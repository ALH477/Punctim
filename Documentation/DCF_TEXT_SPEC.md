# DCF-Text — Chat / Agent-to-Agent Text over the DeModFrame Wire

**Version 1** · DeMoD LLC · This document is normative. Companion to
[`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) and a sibling of
[`DCF_GAME_SPEC.md`](DCF_GAME_SPEC.md) and [`DCF_AUDIO_SPEC.md`](DCF_AUDIO_SPEC.md).

DCF-Text carries UTF-8 text — human chat and **agent-to-agent** messages — over the
Punctim mesh. It introduces **no new wire format**: one message is an **adapter**
over the 17-byte `DeModFrame` quantum, serialised into a short burst of ordinary
`DATA` (type 0) frames. The framing is **content-agnostic and byte-deterministic
across C, Rust, Python, and Go**, pinned by a finite certificate exactly like the
wire quantum itself.

Audio rides `CTRL` (type 3); **text and game both ride `DATA` (type 0)** but split
the `seq` field differently — text gives the fragment index **10 bits** (messages
are larger and lower-rate than 20 ms audio blocks or per-tick game state), game
gives it 5. See [Relationship to DCF-Game](#relationship-to-dcf-game).

## Layered model

| Layer | What | Certified? |
|-------|------|-----------|
| **L1** message | One UTF-8 string (≤ 4092 B) plus opaque descriptor flags. | n/a (opaque app bytes) |
| **L2** framing | `packetize` / reassemble: message bytes ⇄ `DeModFrame` `DATA` frames. | **Yes** — `text_vectors.json` |
| **L3** delivery | Reassembly with dup-suppression + out-of-order tolerance; optional ARQ via the `RELIABLE` flag. | Runtime (unit-tested) |
| **L4** transport + apps | Frames over the mesh; matrix-bridge agent-to-agent, `DcfTextNode`. | App |

The key invariant: **the descriptor flags are opaque to L2, so they never change
the L2 vectors** — the framing bytes are invariant to the agent/more/reliable
choice.

## L2 framing (normative)

One message → `1 + frag_total` frames. Every frame is a fully valid `DeModFrame`
(version = 1, type = `DATA` = 0) and still passes the 246-vector wire certificate.

```
 seq (u16)      = packet_id[15:10] (6 bits, 0..63) | frag_idx[9:0] (10 bits, 0..1023)
 frag_idx == 0  descriptor : payload = [len_hi, len_lo, flags, 0]   (length big-endian u16)
 frag_idx 1..N  data       : payload = bytes[(k-1)*4 .. +4]         (last frame zero-padded)
 frag_total     = ceil(len / 4)
 timestamp_us   = 24-bit send time, identical across a message's frames
 src_id         = local node id ; dst_id = rendezvous channel (0xFFFF = broadcast)
```

Bounds (from the 10-bit fragment field):

| Quantity | Value |
|----------|-------|
| Max data fragments | 1023 |
| **Max message payload** | **4092 bytes / message** |
| Max `packet_id` | 63 (6-bit; wraps modulo 64) |
| Frames per message | `1 + ceil(len/4)` (1–1024) |

The descriptor carries the **true byte length** (big-endian u16) so the receiver
can strip the zero-padding on the final data fragment. Messages larger than 4092 B
must be split by the application (the `MORE` flag marks continuation chunks); the
adapter never re-fragments beyond the 4092 B quantum.

**Descriptor flags** (`payload[2]` of the descriptor) request delivery behaviour;
they do **not** change the L2 bytes:

| Bit | Name | Meaning |
|-----|------|---------|
| 0 | `AGENT` (0x01) | Message is to/from an LLM agent (agent-to-agent traffic). |
| 1 | `MORE` (0x02) | This message is one chunk of a longer logical message/event. |
| 2 | `RELIABLE` (0x04) | Ask the receiver to ACK (the DCF-Text reliability layer). |

Remaining bits reserved. Because the flags are opaque to L2, an `AGENT|RELIABLE`
message and a bare message of the same text produce **byte-identical** framing —
only the flag byte and the delivery path differ.

**Reassembly.** Buffer by modular `packet_id`; gather the descriptor and every data
fragment `1..frag_total`; concatenate and truncate to `len`; decode UTF-8 and emit
`(packet_id, timestamp_us, src, dst, text, flags)`. Duplicate fragments are ignored
(idempotent — replaying a fragment never double-emits). Out-of-order delivery is
tolerated. A `finalize` call reports every still-incomplete message as **lost**
(ascending `packet_id`) and clears state. Invalid UTF-8 is decoded with replacement
rather than dropped.

## Relationship to DCF-Game

Text and game **share `DATA` (type 0)** and the same descriptor-then-data
fragmentation scheme. There is **no in-band tag** that distinguishes a text `DATA`
frame from a game one — they differ only by `seq` split (text 6+10, game 11+5),
which is not self-describing. They are therefore separated by **deployment, not by
the bytes**: a node feeds a channel's frames to the reassembler for the one service
it runs on that channel (a text node runs a `TextReassembler`; a game node runs a
`GameReassembler`). Do not multiplex text and game on the same `dst` channel with a
single node expecting to demultiplex them in-band. Audio is unambiguous against both
(it rides `CTRL`).

## Addressing & channel rendezvous

Text frames ride the existing DCF transport. Peers rendezvous on a **channel** (the
`DeModFrame` `dst_id`): `dst_id = 0xFFFF` is broadcast; otherwise the channel is
derived from a shared passphrase via the certified CRC-16
(`channel_id(name) = crc16(name)`, the same hash the rest of the repo uses). A node
accepts a frame iff `dst_id == my_channel` or `dst_id == 0xFFFF`, and multiplexes
correspondents by `src_id`.

## Certification

Mirrors the wire quantum and DCF-Game: a Python reference emits golden vectors; the
ports diff against them byte-for-byte.

```sh
# regenerate + verify the laws, then diff against the committed vectors
python3 python/MCP/gen_text_vectors.py /tmp/text_vectors.json
diff /tmp/text_vectors.json   Documentation/text_vectors.json   # must be empty
diff /tmp/text_vectors.json   python/MCP/text_vectors.json       # must be empty

cd codec && cargo test --test certify_text                       # Rust
gcc -std=c11 -I codec C_SDK/tests/test_text_certify.c -lm -o /tmp/tc && /tmp/tc   # C
cd go && go test ./text/                                         # Go
```

`Documentation/text_vectors.json` (with an identical `python/MCP/` copy) holds **9
framing + 4 reassembly** cases (the reassembly cases exercise in-order, reordered,
duplicate, and fragment-drop-lost delivery); `codec/text_vectors.gen.h` is the
dependency-free C vector dump. CI runs the Python/C/Rust certs and diffs regenerated
vs. committed vectors (`.github/workflows/wire-certify.yml`, job `certify-text`);
the Go cert runs under `certify-go`.

**Anchor.** `exampleTextMessage`: `text = "agent ⇄ agent 🚀"` (20 UTF-8 bytes),
`packet_id = 10`, `ts_us = 66051`, `src = 0x00A1`, `dst = 0xEED7`, `flags = 0x05`
→ 1 descriptor + 5 data frames, first frame
`d310280000a1eed7001405000102035490`.

## Reference implementations

| Lang | File | Entry points |
|------|------|--------------|
| C | `codec/demod_text.h` | `dcf_text_packetize` / `dcf_text_reasm_push`, `dcf_text_channel_id` |
| Rust | `codec/src/text.rs` | `packetize` / `TextReassembler`, `channel_id` |
| Python | `python/MCP/textlab_core.py` | `packetize` / `TextReassembler`, `channel_id` |
| Go | `go/text/text.go` | `Packetize` / `TextReassembler`, `ChannelID` |
| Lua | `lua/dcf_text.lua` | `packetize` / `new_reassembler`, `channel_id` — self-certifying (LGPL-3.0-only, dual-licensed; see `lua/LICENSING.md`) |

`python/MCP/textlab_core.py` is the canonical source of truth (the generator
`gen_text_vectors.py` emits the laws + vectors from it). An additional interoperable
Node.js port — `JS/nodejs/src/text.js`, exposed as `DcfTextNode` in
`JS/nodejs/src/node.js` — speaks the same on-air dialect.

## Consumers

- **Agent-to-agent (matrix-bridge).** The `a2a` tooling and `DcfTextNode`
  (`matrix-bridge/dcf_node.py`, with `matrix-bridge/dcf_text.py` re-exporting the
  canonical `textlab_core`) let two MCP-capable agents talk directly over the mesh —
  no Matrix server, no human relay (`matrix-bridge/AGENT_TO_AGENT.md`). The `AGENT`
  flag marks that traffic.
- **Node.js `DcfTextNode`** — a stdlib-`dgram` text node speaking the same dialect
  as the Python node.

## Theorem

L2 framing is a fixed bit-placement adapter over the certified `DeModFrame`.
Matching the framing + reassembly vectors pins the C, Rust, and Go implementations
to the Python reference on the entire input space. The descriptor flags are opaque
to L2, so the framing vectors are invariant to the agent/more/reliable choice —
adding flags or transports never re-opens the certificate, and the 246-vector wire
certificate is untouched.

## Security & export posture

Like the rest of DCF, the text wire is **plaintext** — message contents, `src`/`dst`,
and timing are fully readable by any on-path observer (the protocol is deliberately
encryption-free for export compliance). Deploy behind WireGuard or operator-supplied,
export-compliant crypto **beneath** the UDP socket; never add encryption to the codec.
See [`DCF_SECURITY_EXPOSURE.md`](DCF_SECURITY_EXPOSURE.md).
