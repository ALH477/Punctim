# DCF-Telemetry — Engine Meters/Scope over the DeModFrame Wire

**Version 1 (draft)** · DeMoD LLC · Normative. Companion to
[`DCF_AUDIO_SPEC.md`](DCF_AUDIO_SPEC.md) and [`DCF_CONTROL_SPEC.md`](DCF_CONTROL_SPEC.md).

DCF-Telemetry carries the **engine → GUI readback** (per-slot meters, transport state,
optional scope) over Punctim, so the DeMoD GUI can drive a split engine (VM / wired
offloader). It is an **adapter** over the 17-byte `DeModFrame`, reusing DCF-Audio's L2
framing exactly: one telemetry block per engine tick → a burst of `CTRL` frames. It carries
the same data demod-rt publishes locally in `/dev/shm/demod-rt-meters` (`DemodRtMeters`).

Lossy by design: a dropped block just skips one UI refresh (latest-wins, no retransmit).

## Layered model (reuses DCF-Audio L2)

| Layer | What | Reuses |
|-------|------|--------|
| **L1** frame | 17-byte `DeModFrame`, `CTRL` (type 3). | `codec/demod_frame.h` |
| **L2** framing | DCF-Audio `packetize`/reassemble: block ⇄ `CTRL` frames, `codec_id` in the descriptor. | `codec/demod_audio.h`, `lua/dcf_audio.lua` (certified) |
| **L3** block | the compact meters block (below) or a scope block. | this doc |
| **L4** transport | one frame → `ProtoMessage` (`DCF_MSG_AUDIO=2`) → UDP datagram; lossy. | `dcfnode.c` |

Two `codec_id`s in the DCF-Audio registry (extending it; the L2 vectors are unchanged
because `codec_id` lives in the descriptor):

| codec_id | Name | Rate | Reliability |
|----------|------|------|-------------|
| **16** | Meters | engine tick (30–60 Hz) | lossy, latest-wins |
| **17** | Scope | lower (15–30 Hz) | lossy, latest-wins |

## L2 framing (normative — inherited from DCF-Audio)

One block of `payload_len` bytes → `1 + frag_total` frames (`CTRL`, version 1):

```
 seq (u16)     = packet_id[15:5] (11 bits) | frag_idx[4:0] (5 bits)
 frag_idx == 0 descriptor : payload = [payload_len, frag_total, codec_id, flags]
 frag_idx 1..N data       : payload = bytes[(k-1)*4 .. +4]   (last frame zero-padded)
 frag_total    = ceil(payload_len / 4)
 packet_id     = tick counter (gaps at the receiver = dropped blocks)
 src_id        = engine node id     dst_id = the TELEMETRY (or SCOPE) channel
```

Bound (5-bit frag): **≤ 124 bytes / block**. The meters block is designed to fit one packet.

## L3: Meters block (codec_id 16)

Fixed little-endian header (15 bytes) + `N` slot records (4 bytes each). Values are
**quantised** so a full rig fits one 124-byte block.

```
 off  field           type   encoding
 0    version         u8     = 1
 1    slot_count N    u8     0..27  (N*4 + 15 <= 124  ->  N <= 27)
 2    flags           u8     bit0 = levels are dB-mapped (else linear)
 3    master_level    u8     max active output, quantised (see below)
 4-5  bpm             u16    beats per minute
 6-7  pitch_hz        u16    detected pitch, Hz (0 = none)
 8    beat            u8     0..3
 9    cpu             u8     engine load %, 0..100
 10   xruns           u8     xruns since last block (saturating)
 11-12 mute_mask      u16    bit i set = slot i muted
 13-14 solo_mask      u16    bit i set = slot i soloed
 15.. slot[i] (4B):   [ level_L u8, level_R u8, gain u8, pan i8 ]
```

Quantisation (normative, so C/Lua/Python agree byte-for-byte):

- **level** (0..1 float): linear `u8 = round(clamp(level,0,1) * 255)`; or, when `flags.bit0`,
  dB-mapped `u8 = round((clamp(dbfs,-60,0)/60 + 1) * 255)` for better low-level resolution.
- **gain** (0..1.5 amp): `u8 = round(clamp(gain,0,1.5)/1.5 * 255)`.
- **pan** (-1..1): `i8 = round(clamp(pan,-1,1) * 127)`.

For `N = 16` the block is `15 + 64 = 79` bytes → one descriptor + 20 data frames. Rigs with
`N > 27` slots MUST split into consecutive blocks (rare; the default rig is 12–16 slots).

The GUI decodes this straight into the same fields `orchestrator.lua poll()` reads from the
meters shm today (`levels_l/r`, `slot_gain/pan`, `mute_mask/solo_mask`, `bpm/pitch/beat/cpu/
xruns`), so no consumer above the backend changes.

## L3: Scope block (codec_id 17)

Post-master scope, **downsampled + quantised** to fit one packet:

```
 off  field       type   encoding
 0    version     u8     = 1
 1    n           u8     sample count, <= 120
 2    flags       u8     bit0 = stereo interleaved (else mono)
 3    reserved    u8
 4..  samples     i8[n]  normalized -128..127  (mono: n samples; stereo: n/2 L,R pairs)
```

`n <= 120` keeps the block `<= 124` bytes (one packet). The full 256-sample ring is
decimated to `n` and quantised to `i8`. Sent at a lower rate than meters (the scope is a
visual, not a control). Larger/full-precision scope is out of scope for v1.

## Reliability / loss

Fire-and-forget. The receiver's `Reassembler` (per DCF-Audio) emits each completed block;
**`packet_id` gaps = dropped blocks**, which simply skip a UI frame. No retransmit, no PLC
(unlike audio; a missed meter is invisible). Duplicates ignored.

## Channel / addressing

Engine (node 1) sends to a **telemetry channel** `dst_id` (and a separate **scope channel**)
pre-agreed with the GUI (node 2), same scheme as DCF-Control. One-way (engine → GUI).

## Security

Same posture as DCF-Control: encryption-free wire; plaintext on a trusted VM/wired link,
**WireGuard beneath** on an untrusted one. Telemetry is read-only readback (lower risk than
control), but leaks rig state to an on-path observer — tunnel if that matters.

## Reference implementation targets

| Language | Files |
|----------|-------|
| C (engine bridge: shm → block → frames) | `codec/demod_audio.h` (`dcf_audio_packetize`) + the block encoder in this doc |
| Lua (GUI: frames → block → poll structs) | `lua/dcf_audio.lua` (`Reassembler`) + a block decoder |

## Certification

Extend `gen_audio_vectors.py`: golden vectors for the Meters and Scope block round-trip
(struct ⇄ quantised bytes ⇄ frames ⇄ struct), C/Lua/Python byte-identical. The L2 framing
is already certified by `audio_vectors.json`; only the two new block layouts need vectors.

## Latency / bandwidth (informative)

Meters at 60 Hz × ~21 frames/block × 17 B ≈ **21 KB/s**; scope at 30 Hz × ~31 frames × 17 B
≈ **16 KB/s**. Negligible on a VM/wired link. One-way latency = engine tick → packetize →
UDP (sub-ms) → reassemble → UI, well inside a display frame.
