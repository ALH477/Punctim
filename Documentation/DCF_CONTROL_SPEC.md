# DCF-Control — Engine Control Ops over the DeModFrame Wire

**Version 1 (draft)** · DeMoD LLC · Normative. Companion to
[`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) and [`DCF_AUDIO_SPEC.md`](DCF_AUDIO_SPEC.md).

DCF-Control carries **engine control operations** (the messages a GUI sends to a real-time
audio engine: load an effect, set a parameter, trigger a note) over Punctim. It
introduces **no new wire format**: a control op is an **adapter** over the 17-byte
`DeModFrame` quantum, serialised as a **DCF-Text** message (a burst of `DATA` frames). It
exists so the DeMoD stack (orchestrator + demod-rt + demod-ui) can run **split across a
link**: engine on a VM or wired offloader, GUI on the host.

The paired return path (meters/scope) is [`DCF_TELEMETRY_SPEC.md`](DCF_TELEMETRY_SPEC.md).

## Layered model

| Layer | What | Reuses |
|-------|------|--------|
| **L1** frame | 17-byte `DeModFrame`, `DATA` (type 0). | `codec/demod_frame.h` |
| **L2** framing | DCF-Text `packetize`/reassemble: op bytes ⇄ `DATA` frames. | `codec/demod_text.h` (certified) |
| **L3** op payload | v1 = the engine's existing JSON-lines op, verbatim. | none (byte-transparent relay) |
| **L4** reliability | ACK + retransmit for state-changing ops; fire-and-forget for latest-wins. | this doc |
| **L5** transport | one frame → one `ProtoMessage` (`DCF_MSG_TEXT_DCF=10`) → one UDP datagram. | `C_SDK/node/dcfnode.c`, `dcf_proto.h` |

The key decision: **the op payload is opaque to L1–L2**, so the control vocabulary can grow
without touching the wire certificate. v1 keeps it trivial by shipping the exact bytes the
engine already accepts on its local Unix control socket.

## L2 framing (normative — inherited from DCF-Text)

A control op of `payload_len` bytes → one **DCF-Text message** = `1 + frag_total` frames,
each a valid `DeModFrame` (version 1, type `DATA` = 0):

```
 seq (u16)      = packet_id[15:10] (6 bits) | frag_idx[9:0] (10 bits)
 frag_idx == 0  descriptor : payload = [len_hi, len_lo, flags, 0]
 frag_idx 1..N  data       : payload = bytes[(k-1)*4 .. +4]   (last frame zero-padded)
 frag_total     = ceil(payload_len / 4)
 src_id         = sender node id      dst_id = the CONTROL channel
```

Bounds (from the 10-bit fragment field): **≤ 1023 data fragments → ≤ 4092 bytes / op**.
Every current DeMoD op is far smaller (the largest, `load_fx` with an absolute `.so` path,
is < 300 bytes). Ops that would exceed 4092 bytes MUST be app-chunked (not expected in v1).

Reference: `dcf_text_packetize` / `dcf_text_reasm_init` / `dcf_text_reasm_push`
(`codec/demod_text.h`, C) and the Lua/Rust equivalents.

## L3 op payload (v1 = verbatim JSON)

v1 carries the engine's **existing control-socket protocol byte-for-byte**: a single
JSON-lines op object. The engine-side bridge reassembles the message and `write()`s it to
the local `$DEMOD_CONTROL_SOCK` unchanged, so the orchestrator sees an ordinary local
client and **runs unmodified**.

The op set (from the demod-rt control socket):

| Op | Fields | Class |
|----|--------|-------|
| `load_fx` | `slot`, `path` | state-changing (reliable) |
| `unload_fx` | `slot` | state-changing |
| `set_param` | `slot`, `idx`, `value` | latest-wins |
| `bypass_fx` | `slot`, `on` | latest-wins |
| `set_gain` / `set_bpm` | `gain` / `bpm` | latest-wins |
| `set_slot_gain`/`pan`/`mute`/`solo` | `slot`, value | latest-wins |
| `synth.load` | `slot`, `path` | state-changing |
| `synth.note_on`/`note_off` | `slot`, `note`, `velocity` | event (reliable-ish) |
| `synth.all_notes_off` | `slot` | event |
| `preset_load`/`preset_save` | `name` | state-changing |

Each op carries a monotonic `id` (already present as `"id":"ui-N"` in the envelope); the
bridge echoes it in the ACK.

**v2 (optional, informative):** a compact binary op-codec keyed like DCF-Audio's `codec_id`
— e.g. `set_param` as `[op=0x01, slot u8, idx u8, value f32]` = 7 bytes = one descriptor +
2 data frames. Reduces per-op frames ~4×. Not required; the JSON relay is the certified v1.

## L4 reliability (normative)

The UDP transport is fire-and-forget. Control ops split by class:

- **latest-wins** (`set_param`, `set_slot_*`, `bypass_fx`, `set_gain`, `set_bpm`): sent
  once, **no ACK**. A dropped frame is corrected by the next value (a swept knob emits a
  stream). The receiver deduplicates by `(op, slot, idx)` keeping the highest `packet_id`.
- **state-changing** (`load_fx`, `unload_fx`, `synth.load`, `preset_*`): the sender MUST
  receive an **ACK** and retransmit (exponential backoff, cap ~5 tries, ~250 ms window)
  until acked or failed-to-UI. The engine bridge replies with a `DCF_MSG_TEXT_DCF` op
  `{"op":"ack","id":<echoed>,"ok":<bool>}` (or a bare `ACK`-type frame carrying the id).
  Ops are idempotent where possible (re-`load_fx` of the same slot/path is a no-op).
- **event** (`synth.note_on`/`off`): sent + one fast retransmit inside a short window
  (loss ≈ 0 on a VM/wired link, so a dropped note is rare and non-fatal). `all_notes_off`
  is a reliable safety op (treat as state-changing).

Ordering: `packet_id` (11-bit, wraps) gives per-channel order; the bridge applies ops in
`packet_id` order and drops duplicates.

## Channel / addressing

Handshakeless. Sender and engine pre-agree a **control channel** = the frame `dst_id`
(a `u16`, or `channel_from_passphrase("demod-control")`). Node ids: GUI = 2, engine = 1.
Peers wire as `id@host:port`; each binds `INADDR_ANY` and `sendto`s the peer. Liveness is a
`DCF_MSG_PING`/`PONG` heartbeat on the same link.

## Security

The wire is **encryption-free by design** (see `DCF_SECURITY_EXPOSURE.md`) and control ops
are **forgeable** by any on-path node. Therefore:

- **Trusted link** (same-host VM, wired offloader): plaintext (intended mode).
- **Untrusted link**: run the UDP inside a **WireGuard** tunnel (crypto stays outside DCF).
- **Optional pairing-auth ABOVE the wire**: the engine bridge MAY require a signed
  `hello` (Ed25519, reusing the DeMoD stack's `dm.crypto`) before accepting control ops
  from a GUI node, so a rogue node on a shared segment cannot forge control. This lives in
  L3/L4, never in the L1 codec.

## Reference implementation targets

| Language | Files |
|----------|-------|
| C (engine bridge, GUI `dm.dcf`) | `codec/demod_frame.h` + `codec/demod_text.h` + lifted `C_SDK/node/dcfnode.c` glue |
| Lua (GUI fallback) | `lua/dcf_audio.lua` (frame codec) + a small text-L2 port + luasocket UDP |

## Certification

Mirror DCF-Text: a Python reference emits golden vectors (op bytes → frame burst →
reassembled bytes); C and Lua diff byte-for-byte. Add `control_vectors.json` alongside
`audio_vectors.json`. L3 (JSON payload) is opaque and needs no vectors; only the L2 framing
is certified (it already is, via DCF-Text).

## Latency budget (informative)

knob turn → packetize → 1 UDP datagram (sub-ms on VM/wired) → reassemble → local socket
write → engine applies. **~1–2 ms one-way** added control latency, imperceptible; and the
**audio path never crosses the link**, so audio latency is unchanged.
