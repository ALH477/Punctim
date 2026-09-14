# DCF-Audio — Collaborative Audio over the DeModFrame Wire

**Version 1** · DeMoD LLC · This document is normative. Companion to
[`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md).

DCF-Audio carries real-time, collaborative audio (jamming, talkback) over the
Punctim mesh. It introduces **no new wire format**: a 20 ms codec block is an
**adapter** over the 17-byte `DeModFrame` quantum, serialised into a short burst of
ordinary `CTRL` (type 3) frames. The framing is **codec-agnostic and
byte-deterministic across C, Rust, and Python**, and is pinned by a finite
certificate exactly like the wire quantum itself.

## Layered model

| Layer | What | Certified? |
|-------|------|-----------|
| **L1** codec registry | Pluggable codecs keyed by `codec_id` (Opus / PCM-diag / Faust-PM). Output is opaque to L2. | PCM-diag bytes & PM param layout: **yes**. Opus & PM *audio*: no (float/version-dependent). |
| **L2** framing | `packetize` / reassemble: codec bytes ⇄ `DeModFrame` CTRL frames. | **Yes** — `audio_vectors.json` |
| **L3** jitter buffer + PLC | Reorder/dejitter on `packet_id`/`timestamp_us`; conceal lost packets. | Runtime (unit-tested) |
| **L4** transport + demo | Send frames over the mesh; 2-peer loopback jam. | Demo |

The key invariant: **`codec_id` lives in the L2 descriptor, so adding codecs never
changes the L2 vectors.**

## L2 framing (normative)

One 20 ms block → one *audio packet* → `1 + frag_total` frames. Every frame is a
fully valid `DeModFrame` (version = 1, type = `CTRL` = 3) and still passes the
246-vector wire certificate.

```
 seq (u16)      = packet_id[15:5] (11 bits) | frag_idx[4:0] (5 bits)
 frag_idx == 0  descriptor : payload = [payload_len, frag_total, codec_id, flags]
 frag_idx 1..N  data       : payload = bytes[(k-1)*4 .. +4]   (last frame zero-padded)
 frag_total     = ceil(payload_len / 4)
 timestamp_us   = 24-bit block capture time, identical across a packet's frames
 src_id/dst_id  = node ids (0xFFFF = broadcast for a jam)
```

Bounds (from the 5-bit fragment field):

| Quantity | Value |
|----------|-------|
| Max data fragments | 31 |
| **Max codec payload** | **124 bytes / 20 ms** (≈ 49.6 kbps) |
| Max `packet_id` | 2047 (wraps ≈ 41 s at 20 ms) |
| Frames per packet | `1 + ceil(payload_len/4)` (1–32) |

Descriptor flags: bit 0 = end-of-talkspurt, bit 1 = PM voice profile.

**Reassembly.** Buffer by modular `packet_id`; gather the descriptor and all data
fragments; concatenate and truncate to `payload_len`; emit
`(packet_id, timestamp_us, codec_id, flags, payload)`. Duplicates are ignored. Any
missing fragment ⇒ the packet is **lost** ⇒ L3 runs the codec's PLC.

## Codec registry

| id | Name | Profile (20 ms blocks) | ≤124 B? | Byte-deterministic? |
|----|------|------------------------|---------|---------------------|
| 0 | Opus | 48 kHz mono, ~24 kbps (≈60 B) | yes | No (libopus-version dependent) |
| 1 | PCM-diag | 6 kHz, 8-bit linear, mono = 120 B | yes | **Yes** (round-trip pinned) |
| 2 | Faust-PM | phase-mod synthesis, **8-byte param block** | yes | Param layout: **yes**; synthesis audio: no |
| 3 | *reserved* | (declined low-bitrate speech codec) | — | — |

Because a full-band PCM block cannot fit in 124 B, the diagnostic PCM codec runs at
6 kHz / 8-bit (one byte per sample, 120 B). Opus fragments ≈ 16 frames; PM is just
3 frames, so PM and Opus tolerate frame loss far better than PCM-diag.

### Faust phase-mod codec (id 2) — 8-byte parameter block

The wire carries compact synthesis parameters; the decoder resynthesises timbre via
a ratio-locked phase-modulation oscillator (`codec/faust/dcf_pm_codec.dsp`, committed
as Faust→C in `dcf_pm_codec.gen.c`). The **layout is certified**; the synthesised
audio is not.

```
 byte:  0       1      2     3          4          5        6      7
 field: f0_hi   f0_lo  amp   mod_index  mod_ratio  bright   env    flags
        u16 (cents-mapped fundamental)  u8 each (carrier:mod is u8 fixed-point)
```

## Certification

Mirrors the wire quantum: a Python reference emits golden vectors; C and Rust diff
against them byte-for-byte.

```sh
# regenerate + verify the laws, then diff against the committed vectors
python3 python/MCP/gen_audio_vectors.py /tmp/audio_vectors.json
diff /tmp/audio_vectors.json    Documentation/audio_vectors.json
diff /tmp/pm_param_vectors.json Documentation/pm_param_vectors.json
diff /tmp/audio_vectors.gen.h   codec/audio_vectors.gen.h

# C  (L2-only; no libopus / Faust needed)
gcc -std=c11 -Wall -Wextra -I codec C_SDK/tests/test_audio_certify.c -lm -o /tmp/ac && /tmp/ac

# Rust
cd codec && cargo test --test certify_audio
```

| Artifact | Role |
|----------|------|
| `Documentation/audio_vectors.json` (= `python/MCP/`) | framing + reassembly + PCM round-trip vectors |
| `Documentation/pm_param_vectors.json` (= `python/MCP/`) | PM 8-byte param round-trip |
| `codec/audio_vectors.gen.h` | same vectors as a C header (dependency-free C test) |
| `python/MCP/audiolab_core.py` | Python L2 reference (imports the certified wire codec) |
| `python/MCP/gen_audio_vectors.py` | executable laws + vector generator |

## Reference implementations

| Language | File | Entry points |
|----------|------|--------------|
| C | `codec/demod_audio.h` | `dcf_audio_packetize`, `dcf_audio_reasm_*`, `dcf_codec_get`, `dcf_pm_pack/unpack` |
| Rust | `codec/src/audio.rs` | `packetize`, `AudioReassembler`, `codec_for`, `pm_pack/unpack` |
| Python | `python/MCP/audiolab_core.py` | `packetize`, `AudioReassembler`, `pm_pack/unpack` |
| Lua | `lua/dcf_audio.lua` | self-certifying L2 codec (LGPL-3.0-only, dual-licensed — see `lua/LICENSING.md`) |

## Latency budget (informative)

~5 ms capture · 5–10 ms encode (Opus/PM) · 20 ms one-way network · 20–40 ms jitter
buffer (1–2 packets) · 5–10 ms decode · 5 ms playout → **< 100 ms** one-way for
collaborative jamming. The jitter window and PLC aggressiveness are tunable.

## Transport & demo

- **Rust SDK:** `DcfNode::send_audio_dcf(codec_id, encoded, packet_id, ts_us)`
  packetizes a block and ships each 17-byte frame as an unreliable `AUDIO` message;
  the receiver feeds payloads to `reassemble_audio_payload`.
- **2-peer jam:** `cd codec && cargo run --example jam_loopback -- --codec pcm`
  (add `--loss 0.05` to exercise PLC; `--features opus|pm` + `--codec opus|pm`).

## TERMINUS integration (informative)

`unified-UI` (DeMoD TERMINUS) is a separate repo. The intended hookup feeds the
orchestrator's real-time audio through L1–L4 and surfaces peers via the existing
device-bridge `/v1/mesh` endpoint — a future `demod-jam` app, not built here.
