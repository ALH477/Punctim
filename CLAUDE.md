# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A polyglot monorepo for **DCF / Punctim** (DeMoD Communication Framework) — a
handshakeless, encryption-free, export-control-compliant mesh protocol. The same
protocol is implemented as bindings/SDKs across C, C++, Rust, Go, Python, Perl,
Java/Kotlin, Swift, Node.js, Haskell, and Common Lisp, each in its own top-level
directory.

The gravitational center of current work is narrow: the **wire quantum** and its
cross-language certification, now extended with a certified **audio** layer.
Read these first — they are normative:
- `Documentation/WIRE_QUANTUM_SPEC.md` — the 17-byte `DeModFrame` wire format.
- `Documentation/DCF_AUDIO_SPEC.md` — collaborative audio as an adapter over it.
- `Documentation/DCF_GAME_SPEC.md` — multiplayer game state/events as an adapter
  over it (same fragmentation scheme as audio, on `DATA` frames).
- `Documentation/DCF_TEXT_SPEC.md` — chat / agent-to-agent UTF-8 text as an adapter
  over it (also on `DATA` frames, but a 10-bit fragment index).
- `Documentation/DCF_SSTV_SPEC.md` — slow-scan television (still images) as an adapter
  over it (also on `DATA` frames, but an 11-bit fragment index), carried over HydraModem.
- `Documentation/DCF_SNAKE_SPEC.md` — a synchronized studio audio snake over cat5e: a star
  of nodes → one "mixer" hub, with a quanta-coded **record plane** (`CTRL` 5:11) and a
  bidirectional low-latency PCM **cue plane** (`CTRL` 9:7), locked to a `BEACON` media clock.
- `Documentation/DCF_MESH_SPEC.md` — self-healing redundancy (peer-health FSM,
  REPORT/ROLE control, election + failover) as a `MsgMesh` control adapter.
- `Documentation/HYDRAPACK_SPEC.md` — universal serialization above DeModFrame + Pipe:
  a declarative schema model, plane-aware emission (quantum vs Pipe), and byte-certified
  bit-packing across C/Rust/Python. Not a new wire format; the library intermediary.
- `Documentation/DCF_PIPE_MULTI_SPEC.md` — DCF-Pipe Multi-Control: up to 3 steady-state
  DCF-Pipe commands packed into one 4-byte DeModFrame payload for concurrent pipe
  steering. Control adapter; pipe_vectors.json untouched.
- `Documentation/DCF_SECURITY_EXPOSURE.md` — the plaintext wire's exposure and the
  WireGuard / external-crypto deployment rule.
- `Documentation/DCF_SPA_SPEC.md` — single-packet port authorization: an
  **authentication-only** (EAR99) side channel that gates mesh data ports, NOT a
  wire format (the certificate is untouched). Implemented in `spa/` (Rust
  `dcf-spa-authorizer` + `python/dcf/spa/knock.py`).
- `Documentation/DCF_CODE_REVIEW.md` — frank, module-by-module status (consult
  before trusting any module's surface area).

## The wire quantum (the one invariant)

One wire format: the 17-byte `DeModFrame` (version nibble = 1). Layout:
`sync(0xD3) | flags[ver|type] | seq | src | dst | payload(4B) | ts24 | crc16`.
Valid iff sync byte + version nibble + CRC-16/CCITT-FALSE over bytes `[0..14]`.
Everything else on the wire is an **adapter** over this quantum. CRC anchors:
`CRC("123456789")=0x29B1`, `CRC(0^15)=0x4EC3`.

Reference codecs (must stay byte-identical):

| Lang | File | Entry points |
|------|------|--------------|
| C | `codec/demod_frame.h` | `dcf_frame_encode/decode`, `dcf_crc16` |
| Rust | `codec/frame.rs`, `codec/src/lib.rs` | `Frame::encode/decode` |
| Python | `python/MCP/wirelab_core.py` | `encode`/`decode`/`crc16_ccitt`/`syndrome` |
| Lua | `GUI/wirelab.lua` | `encode`/`decode`/`crc16` (self-certs on load) |
| Haskell | `haskell/src/DCF/Transport/FrameSpec.hs` | `encodeFrame`/`decodeFrame` |
| Lisp | `lisp/src/punctim.lisp`, `lisp/src/wire.lisp` | `encode-dcf-frame` |

### Certification is the contract

`Documentation/golden_vectors.json` is a 246-vector certificate (109 encode +
137 syndrome). Matching all 246 ≡ agreeing with the reference on the entire
input space. When you touch any codec, regenerate and diff:

```sh
python3 python/MCP/verify_laws.py /tmp/gv.json     # regenerate + verify laws
python3 python/MCP/certify_sdk.py --selftest
cd codec && cargo test --test certify              # Rust
```

CI: `.github/workflows/wire-certify.yml` runs Python/C/Rust certs on push/PR to
`main` and diffs regenerated vs committed vectors.

> Canonical Python lives in `python/MCP/` (the old `GUI/MCP/*` dangling symlinks
> were removed). `C_SDK/tests/test_wire_certify.c` is fixed and certifies against
> the current `dcf_frame_t` / `dcf_crc16` API.

## DCF-Audio (collaborative audio over the wire)

A 20 ms codec block is an **adapter** over `DeModFrame`, serialised into
`1 + ceil(payload_len/4)` ordinary `CTRL` (type 3) frames. The **L2 framing**
is **codec-agnostic and byte-certified across C/Rust/Python**; `codec_id` lives
in the descriptor, so adding codecs never changes the vectors. Precisely: the L2
framing, the PCM-diag codec bytes, and the PM param layout are byte-certified;
**Opus output and PM synthesis audio are NOT byte-certified**.

- `seq = packet_id[15:5] | frag_idx[4:0]`; `frag_idx 0` = `[len, frag_total, codec_id, flags]`; payload ≤ **124 B/block**.
- Codecs: Opus (id 0), PCM-diag (id 1, byte-certified), Faust phase-mod (id 2).
- L2 references: `codec/demod_audio.h` (C), `codec/src/audio.rs` (Rust),
  `python/MCP/audiolab_core.py` (Python), and **`lua/dcf_audio.lua`** (Lua, the
  self-certifying `LGPL-3.0-only` dual-licensed binding — see `lua/LICENSING.md`; CLI
  `lua/dcf_jam.lua`, panel `GUI/audiolab.lua`, frequency rendezvous via the `dst`
  channel). PM synth: `codec/faust/dcf_pm_codec.dsp` → committed `dcf_pm_codec.gen.c`.
- Vectors: `Documentation/audio_vectors.json`, `pm_param_vectors.json` (+ copies
  in `python/MCP/`), and `codec/audio_vectors.gen.h` (dependency-free C test).

```sh
python3 python/MCP/gen_audio_vectors.py /tmp/av.json          # regen + verify laws
cd codec && cargo test --test certify_audio                   # Rust
gcc -std=c11 -I codec C_SDK/tests/test_audio_certify.c -lm -o /tmp/ac && /tmp/ac  # C
cd codec && cargo run --example jam_loopback -- --codec pcm   # 2-peer jam demo (--loss 0.05 for PLC)
```

Opus is behind `--features opus` / `-DDCF_AUDIO_OPUS` (needs libopus); PM behind
`--features pm` / `-DDCF_AUDIO_PM`. Only **L2 framing, PCM-diag bytes, and the PM
param layout are byte-certified; Opus output and PM synthesis audio are NOT
byte-certified**. Rust SDK hookup: `DcfNode::send_audio_dcf` +
`reassemble_audio_payload` (`rust/src/lib.rs`).

## DCF-Game (multiplayer game state/events over the wire)

A second adapter over `DeModFrame`, structurally identical to DCF-Audio but on
`DATA` (type 0) frames instead of `CTRL`, so the two never collide. One game
message (state snapshot, input frame, or opaque event) is serialised into
`1 + ceil(payload_len/4)` ordinary `DATA` frames. The L2 framing is
**message-type-agnostic and byte-certified across C/Rust/Python**; `msg_type_id`
lives in the descriptor, so adding message types never changes the vectors.

- `seq = packet_id[15:5] | frag_idx[4:0]`; `frag_idx 0` = `[len, frag_total, msg_type_id, flags]`; payload ≤ **124 B/message**.
- Message types: SNAPSHOT (id 0, 14 B certified), INPUT (id 1, 6 B certified),
  EVENT (id 2, opaque), JOIN (id 3, certified). Descriptor `flags`: bit0 RELIABLE,
  bit1 ORDERED, bit2 END_TICK — these pick the transport path, not the L2 bytes.
- L2 references: `codec/demod_game.h` (C), `codec/src/game.rs` (Rust),
  `python/MCP/gamelab_core.py` (Python). Spec: `Documentation/DCF_GAME_SPEC.md`.
- Vectors: `Documentation/game_vectors.json` (+ identical `python/MCP/` copy) and
  `codec/game_vectors.gen.h` (dependency-free C test).
- Topology: direct P2P / LAN first (mDNS + `add_peer`, frequency-channel
  rendezvous via `dst`); multi-hop mesh forwarding is a future, additive extension.

```sh
python3 python/MCP/gen_game_vectors.py /tmp/gv.json           # regen + verify laws
cd codec && cargo test --test certify_game                    # Rust
gcc -std=c11 -I codec C_SDK/tests/test_game_certify.c -lm -o /tmp/gc && /tmp/gc  # C
```

SDK hookup: `DcfNode::send_game_dcf` + `reassemble_game_payload` and
`MessageHandler::handle_game` (`rust/src/lib.rs`). Client: a **Game** tab (a 2-D
dot-arena demo) alongside Jam/Messages in `client/`.

## DCF-Text (chat / agent-to-agent text over the wire)

A third adapter over `DeModFrame`, structurally like DCF-Game but tuned for larger,
lower-rate messages: one UTF-8 message is serialised into `1 + ceil(len/4)` ordinary
`DATA` (type 0) frames. The L2 framing is **content-agnostic and byte-certified
across C/Rust/Python/Go**. Text and game both ride `DATA(0)` but split `seq`
differently (text gives `frag_idx` 10 bits, game 5); there is **no in-band tag**
distinguishing them, so a node routes a channel's frames to the one reassembler it
runs there — never multiplex text and game on the same `dst`.

- `seq = packet_id[15:10] | frag_idx[9:0]`; `frag_idx 0` = `[len_hi, len_lo, flags, 0]`
  (big-endian length); payload ≤ **4092 B/message** (1023 frags), `packet_id` 0..63.
- Descriptor `flags` (opaque to L2): bit0 AGENT, bit1 MORE, bit2 RELIABLE.
- L2 references: `codec/demod_text.h` (C), `codec/src/text.rs` (Rust),
  `python/MCP/textlab_core.py` (Python, canonical), `go/text/text.go` (Go); plus an
  interoperable Node port `JS/nodejs/src/text.js`. Spec: `Documentation/DCF_TEXT_SPEC.md`.
- Vectors: `Documentation/text_vectors.json` (+ identical `python/MCP/` copy) and
  `codec/text_vectors.gen.h` (dependency-free C test).
- Consumers: matrix-bridge **agent-to-agent** (`matrix-bridge/dcf_node.py`,
  `dcf_text.py` re-exports `textlab_core`) and the Node/Python `DcfTextNode`.

```sh
python3 python/MCP/gen_text_vectors.py /tmp/tv.json           # regen + verify laws
cd codec && cargo test --test certify_text                    # Rust
gcc -std=c11 -I codec C_SDK/tests/test_text_certify.c -lm -o /tmp/tc && /tmp/tc  # C
cd go && go test ./text/                                       # Go
```

## DCF-SSTV (slow-scan television / still images over the wire)

A fourth adapter over `DeModFrame`, structurally like DCF-Text but tuned for the
*largest* messages: one still image (opaque bytes) is serialised into `1 + ceil(len/4)`
ordinary `DATA` (type 0) frames. The L2 framing is **content-agnostic and byte-certified
across C/Rust/Python/Go/Node**; a `format_id` (JPEG/PNG/raw hint) lives in the descriptor,
so adding image formats never changes the vectors. It is the digital, DCF-native
reimagining of ham SSTV — a picture trickling across the narrow **HydraModem** acoustic
link (~8–12 app-bytes/s), rendered top-down as fragments arrive.

- `seq = image_id[15:11] | frag_idx[10:0]`; `frag_idx 0` = `[len_hi, len_lo, format_id, flags]`
  (big-endian length); payload ≤ **8188 B/image** (2047 frags), `image_id` 0..31. Descriptor
  `flags` (opaque to L2): bit0 MORE, bit1 KEYFRAME, bit2 RELIABLE. Text/game/sstv all ride
  `DATA(0)` with **different** seq splits (text 6:10, game 11:5, sstv 5:11) and are separated
  by deployment (one reassembler per `dst`) — never multiplex them on one `dst`.
- L2 references: `codec/demod_sstv.h` (C), `codec/src/sstv.rs` (Rust),
  `python/MCP/sstvlab_core.py` (Python, canonical), `go/sstv/sstv.go` (Go),
  `JS/nodejs/src/sstv.js` (Node). Spec: `Documentation/DCF_SSTV_SPEC.md`.
- Vectors: `Documentation/sstv_vectors.json` (+ identical `python/MCP/` copy) and
  `codec/sstv_vectors.gen.h` (dependency-free C test).
- HydraModem carriage: `hydramodem/dcf-tools/{sstv_send,sstv_recv}.c` (image ⇄ M-FSK WAV,
  byte-exact) and the in-process demo `python/dcf/sstv_demo.py` (progressive scan render
  over `LoopbackTransport`/`HydraTransport`). Images > 8188 B chain across `image_id`s (MORE).

```sh
python3 python/MCP/gen_sstv_vectors.py /tmp/sstv.json           # regen + verify laws
cd codec && cargo test --test certify_sstv                      # Rust
gcc -std=c11 -I codec C_SDK/tests/test_sstv_certify.c -lm -o /tmp/sc && /tmp/sc  # C
cd go && go test ./sstv/                                         # Go
node JS/nodejs/test/certify_sstv.js                             # Node
cd hydramodem && dcf-tools/build.sh && \
  dcf-tools/build/sstv_send img.jpg out.wav --conv && dcf-tools/build/sstv_recv out.wav got.jpg  # acoustic
```

## DCF-Snake (synchronized studio audio snake over cat5e)

A **two-plane** software audio snake: a *star* of source ("spoke") nodes streaming to one
central **"mixer"** hub over dedicated **cat5e**, for studio use, with **two wires per node**.
Both planes are adapters over `DeModFrame` (the 246-vector certificate is untouched), share one
**BEACON(2) grandmaster media clock** from the mixer, and ride a **raw-L2-Ethernet transport**
(a custom EtherType per plane, SuperPack-batched, no IP/UDP). Spec: `Documentation/DCF_SNAKE_SPEC.md`.

- **Record plane (DCF-Snake):** compressed, sample-accurate multitrack *capture* carrying the
  DeMoD **quanta** codec (QSS streaming; `/home/asher/Documents/DeMoD/quanta`). One QSS commit-hop
  packet → one message on `CTRL(3)` frames, `seq = stream_id[15:11] | frag_idx[10:0]` (5:11, ≤8188
  B/message); descriptor `[len_hi, len_lo, mode_id, flags]` (`mode_id` = live/near/relaxed hint).
  quanta's ≥64 ms encode floor is unfit for live tracking but ideal for recording. The mixer is
  the live analog of `client/src-tauri/src/recorder.rs`.
- **Cue plane (DCF-Cue):** a *bidirectional* low-latency uncompressed-PCM monitor loop on the
  second wire — sources send ~1 ms PCM blocks up, the mixer sums a per-node cue mix and returns
  each performer their feed (~4–6 ms RT). `CTRL(3)`, `seq = block_seq[15:7] | frag_idx[6:0]` (9:7,
  ≤508 B/block); descriptor `[block_samples, format, channels, flags]` (length self-describing).
- **Clock = hybrid:** the mixer is grandmaster (16-byte `[gm_sample_count u64 | nominal_rate_mHz
  u32 | tx_seq u16 | epoch u16]` in `BEACON(2)` frames, same 5:11 fragmenter); spokes run a PI
  servo, the mixer runs per-source ASRC fallback. `unwrap_pid` is ported verbatim + certified from
  `sync.rs`. **Byte-certified:** both L2 framings, the clock payload, `unwrap_pid`. **NOT
  certified** (float DSP, like Opus/PM): quanta QSS audio, the mixer ASRC/PLC/cue-mix.
- **quanta = shell out** to `quanta-stream` / `quanta-stream-decode` subprocesses (like
  `JanusTransport`→janus-c), `$QUANTA_STREAM`/`$QUANTA_STREAM_DECODE`; kept out of the LGPL closure
  (GPL-3.0-only OR DeMoD-Commercial), a standalone `nix build .#quanta`.
- **References:** `codec/demod_snake.h` / `demod_monitor.h` (C), `codec/src/{snake,monitor}.rs`
  (Rust), `python/MCP/{snakelab_core,monitorlab_core}.py` (Python). Nodes:
  `hydramodem/dcf-tools/snake_{source,mixer}.c` (+ `snake_l2.c` raw-L2 transport). Vectors:
  `Documentation/{snake,monitor}_vectors.json` (+ `python/MCP/` copies) and
  `codec/{snake,monitor}_vectors.gen.h`. The wire is plaintext — deploy behind WireGuard
  (`Documentation/DCF_SECURITY_EXPOSURE.md`).

```sh
python3 python/MCP/gen_snake_vectors.py   /tmp/sv.json                            # record: regen + verify laws
python3 python/MCP/gen_monitor_vectors.py /tmp/mv.json                            # cue: regen + verify laws
cd codec && cargo test --test certify_snake --test certify_monitor               # Rust
gcc -std=c11 -I codec C_SDK/tests/test_snake_certify.c   -lm -o /tmp/sc && /tmp/sc  # C (record)
gcc -std=c11 -I codec C_SDK/tests/test_monitor_certify.c -lm -o /tmp/mc && /tmp/mc  # C (cue)
```

## DCF-QKD (ETSI GS QKD 014 key-ID beacon over the wire)

A control adapter over `DeModFrame` that fills the one gap **ETSI GS QKD 014
deliberately leaves out of scope**: the transport of a `key_ID` from the master SAE
to the slave SAE. Every deployment has to invent a carrier, and the arithmetic makes
the quantum an unusually good fit — `key_ID = UUID = 128 bits = 16 bytes = exactly
4 x 4-byte payloads`. One key_ID is serialised into exactly **4** ordinary `CTRL`
(type 3) frames, byte-certified across **C/Rust/Python**. Spec:
`Documentation/DCF_QKD_SPEC.md`.

> **Nothing here is quantum.** The "wire quantum" is quantum as in **quanta** — the
> indivisible 17-byte frame. A `key_ID` is an opaque identifier minted by external
> KME hardware; keys come from that hardware (`os.urandom` in the mock). Never
> describe this as quantum communication.

- `seq = epoch[15:2] | frag_idx[1:0]` (14:2, unique among the CTRL adapters: audio
  11:5, cue 9:7, snake 5:11). **No descriptor fragment** — a key_ID is always 16 B,
  so length and frag_total are known a priori and all four fragments are data. This
  is normative; emitting a descriptor is non-conforming. There are consequently no
  descriptor flags.
- Reassembly is keyed by `(src, dst, epoch)` (two SAEs may beacon one epoch at
  once), order-independent, dup-suppressed, and **bounded** (oldest evicted past
  `max_pending`, default 64).
- L2 references: `codec/demod_qkd.h` (C), `codec/src/qkd.rs` (Rust),
  `python/MCP/qkdlab_core.py` (Python, canonical). Vectors:
  `Documentation/qkd_vectors.json` (+ identical `python/MCP/` copy) and
  `codec/qkd_vectors.gen.h`. CI: `certify-qkd` + `qkd-bridge`.
- **The bridge runtime** is `python/dcf/qkd/` (**stdlib only**): `etsi014.py` (the
  ETSI 014 client — status/enc_keys/dec_keys, optional mTLS), `mock_kme.py` (a mock
  KME pair over one shared keystore = the simulated QKD link), `beacon.py` (key_ID
  over any `dcf.transport.Transport`), `sae.py` (`MasterSae`/`SlaveSae`), `demo.py`.
  It works unchanged against QuKayDee or vendor hardware via `--external`.

**Export posture — read `DCF_QKD_SPEC.md` before touching this.** The module
implements no cryptographic algorithm, but it *does* hold delivered key material in
memory, which is a different posture from the rest of the tree. The normative rule:
**key material MUST NOT be placed in a `DeModFrame` payload** — the wire carries the
key_ID (a non-secret identifier) and nothing else. It is enforced by
`TestExportInvariant` in `python/tests/test_qkd_bridge.py`, which taps the wire and
fails on any 4-byte window of a delivered key. **Never wire a delivered key into a
cipher at the DCF layer**; that collapses the whole project's export posture, not
just this module's. SPA's ECCN 5A002 Note (g) authentication carve-out does **not**
apply here.

```sh
python3 python/MCP/gen_qkd_vectors.py /tmp/qkd.json             # regen + verify laws
cd codec && cargo test --test certify_qkd && cargo test --lib qkd  # Rust
gcc -std=c11 -I codec C_SDK/tests/test_qkd_certify.c -lm -o /tmp/qc && /tmp/qc  # C
python3 python/dcf/qkd/demo.py --count 3                        # end-to-end, loopback
nix run .#qkd-demo -- --count 3                                 # same, hermetic
cd python && python3 -m unittest tests.test_qkd_beacon tests.test_qkd_bridge -v
```

## DCF SuperPack (the paired-frame container)

An **opt-in container adapter**, not a new wire format: SuperPack packs **two**
17-byte `DeModFrame`s into **one 32-byte** message (`sync | sflags=0x15 | coreA[14]
| coreB[14] | joint-crc16`), dropping the 6 redundant bytes (2 inner syncs + 2
inner CRCs) recoverable from context. Net `34 → 32` bytes **and** a single joint
CRC over both quanta. `unpack` rebuilds each inner frame **bit-exact**, so the
246-vector wire certificate is untouched — single frames stay the default.

It is the **lower-latency option for paired sends**: one datagram instead of two
(one IP/UDP header, one syscall, one packet on the wire), so a frame pair crosses
the network with strictly less per-pair overhead/latency than two separate frames.

- Spec: `Documentation/SUPERPACK_SPEC.md`. Contract: `Documentation/superpack_vectors.json`
  (+ identical `python/MCP/` copy) and `codec/superpack_vectors.gen.h`.
- Implemented and **certified in every wire-codec language** (Python/Rust/C/Go/C++/
  Perl/Node/Java/Kotlin/Swift/Haskell/Lua/Lisp); `pack`/`unpack`/`is_superpack`
  live next to each language's frame codec (e.g. `codec/src/superpack.rs`,
  `codec/demod_superpack.h`, `go/dcf/superpack.go`). Anchor: zero-core joint CRC = `0x5B75`.

```sh
python3 python/MCP/gen_superpack_vectors.py /tmp/sp.json   # regen + verify laws
cd codec && cargo test --test certify_superpack            # Rust
gcc -std=c11 -I codec C_SDK/tests/test_superpack_certify.c -lm -o /tmp/sc && /tmp/sc  # C
```

## DCF-Pipe (lossless bulk transfer: DCF as the control plane)

The wire quantum as a **control layer** steering a dumb, fast data lane — the adapter
family's missing high-throughput path. Control messages (**OPEN/CREDIT/SACK/NACK/DONE/
ABORT**) ride ordinary frames and are byte-certified across Python/C/Rust; the data lane is
a separate stateless datagram stream (`[session_id u16 | chunk_seq u32 | payload]`), a
transport *beneath* the quantum like UDP/JANUS/HydraModem. The 246-vector certificate is
untouched. Spec: `Documentation/DCF_PIPE_SPEC.md`.

- **The invariant — Φ = N − |R| (the deficit).** The transfer-level analogue of the CRC
  syndrome: one well-founded scalar that is simultaneously a safety invariant (Φ monotone,
  R always correct ⇒ |R|=N means byte-exact) and a termination variant (Φ→0 for any
  drop < 1). `DONE ⟺ Φ = 0 ⟺ object byte-exact`. Clean links finish in exactly **⌈N/W⌉
  rounds** with zero retransmits — *Φ's slope is the throughput*. Implemented as a checked
  monitor in `python/dcf/pipe/invariant.py`; `python/dcf/pipe/laws.py` sweeps it plus five
  derived laws (INTEGRITY / LOSSLESS / FEC-FORWARD / FLOW-BOUND / IDEMPOTENT).
- **Loss recovery is two-tier:** in-budget bit corruption heals *forward* via the certified
  DCF-FEC layer (no round trip); a wholly dropped chunk is NACKed and retransmitted. The
  receiver advances an **authorization horizon** by one window per round, so "in flight" vs
  "dropped" is decided by *round, not position* — a lost final chunk is still recovered, and
  the invariant is timing-agnostic (a 2 ms LAN round == a 2 min acoustic round).
- **Flow control is receiver-driven credit** (not sender congestion windows): simpler and
  naturally rate-limiting for a fleet of known devices.
- **Link profiles** (`pipe.profile(name)`): `lan` (1400 B, nparity 16, W 16) vs
  **`hydramodem` (256 B, nparity 0, W 2)** — on the ~8–12 B/s acoustic link airtime is the
  only scarce resource, so the modem's own conv-FEC+interleaver does the correcting and
  DCF-Pipe adds no second parity layer (measured on-air overhead < 3%, vs ~58% if
  double-FEC'd). Losslessness is unchanged: a chunk the PHY can't recover just never
  arrives, which is exactly Φ's NACK path.
- **References:** `python/MCP/pipelab_core.py` (canonical), `codec/demod_pipe.h` (C),
  `codec/src/pipe.rs` (Rust); runtime `python/dcf/pipe/`. Vectors:
  `Documentation/pipe_vectors.json` (+ `python/MCP/` copy) and `codec/pipe_vectors.gen.h`.

```sh
python3 python/MCP/gen_pipe_vectors.py /tmp/pv.json            # regen + verify laws
cd codec && cargo test --test certify_pipe                     # Rust
gcc -std=c11 -I codec C_SDK/tests/test_pipe_certify.c -o /tmp/pc && /tmp/pc  # C
python3 python/dcf/pipe/laws.py                                # transfer laws + Φ sweep
cd python && python3 -m unittest tests.test_pipe -v            # loopback/lossy/hydramodem
```

## HydraPack (universal serialization above DeModFrame + Pipe)

The single serialization layer that sits above both Punctim data planes. An
application value goes in; either a sequence of 4-byte quanta (for the wire
quantum / adapter path) or a contiguous byte buffer (for the DCF-Pipe data plane)
comes out. The choice is driven by size and schema policy. HydraPack never
invents a new wire format; it only decides *how* application values are turned
into the representations already defined by the quantum and by Pipe.

- **Declarative schema model:** a `Schema` = ordered list of `Field`s
  (`uN`/`iN`/`bool`/`enum`/`bits`/`struct` in v0.1; `blob`/`optional`/`fixed[N]`
  deferred to v0.2). A generic `BitReader`/`BitWriter` drives pack/unpack — no
  per-schema hand-written code. `packed_size` is a pure function of (schema,
  value); in v0.1 (fixed-width types only) it's schema-only.
- **Plane-aware emission:** `if packed_size <= threshold (default 120 B) → quantum;
  else → pipe`. The decision is pure and deterministic.
- **Quantum descriptor (4 bytes, byte-aligned, big-endian):**
  `[schema_id_hi, schema_id_lo, (version<<4)|flags, payload_byte_len]`.
  Single-quantum messages (packed <= 4 B) carry no descriptor — schema is implied
  by context.
- **OpenPipe (17 bytes):** 14-byte OPEN (from the certified Pipe codec) + 3 bytes
  `[schema_id_hi, schema_id_lo, (version<<4)|flags]`. A plain-Pipe receiver sees
  a valid 14-byte OPEN; a HydraPack receiver reads the schema extension. The
  `pipe_vectors.json` certificate is untouched.
- **Byte-certified across C/Rust/Python.** Two vector families: quantum vectors
  (value → list of 4-byte quanta) and pipe vectors (value → buffer + FNV-1a
  checksum). The 246-vector wire certificate is untouched — HydraPack feeds
  payload bytes to adapters and buffers to Pipe; it never touches DeModFrame.
- **References:** `python/MCP/hydrapack_core.py` (canonical), `codec/demod_hydrapack.h`
  (C), `codec/src/hydrapack.rs` (Rust). Vectors: `Documentation/hydrapack_vectors.json`
  (+ identical `python/MCP/` copy) and `codec/hydrapack_vectors.gen.h`. Spec:
  `Documentation/HYDRAPACK_SPEC.md`.

```sh
python3 python/MCP/gen_hydrapack_vectors.py /tmp/hp.json     # regen + verify laws
cd codec && cargo test --test certify_hydrapack              # Rust
gcc -std=c11 -I codec C_SDK/tests/test_hydrapack_certify.c -o /tmp/hp && /tmp/hp  # C
```

## DCF-Pipe Multi-Control (concurrent pipe steering)

A **control adapter** over the 17-byte DeModFrame: up to **3** steady-state DCF-Pipe
commands packed into **one 4-byte** payload, so a node can steer up to 3 concurrent
pipes per quantum on bandwidth-scarce links. It is NOT a new wire format and does NOT
replace classic Pipe control messages — OPEN, large NACK/SACK, ABORT/DONE still ride the
original single-session formats. `pipe_vectors.json` is untouched.

- **Byte-aligned layout (exactly 4 bytes):** `byte0 = [magic:2|count:2|flags:4] =
  0xC0 | (count<<4) | flags`; `bytes 1-3 = cmd slots` (one byte each, bits 7-2 used,
  1-0 zero-pad). Max count = 3 (the cost of strict byte alignment). Magic = `11b` →
  byte0 >= 0xC0, a clean discriminator from classic Pipe (byte0 0..5) and audio CTRL
  descriptors (byte0 <= 124).
- **Command byte:** `(local_idx<<6) | (opcode<<3) | (param_lsb<<2)`. Opcodes: NOP,
  CREDIT_DELTA, ACK_CUMUL, ACK_SELECTIVE, NACK_ONE, DONE_HINT, ABORT_HINT (0..6);
  opcode 111 reserved and rejected. The 1-bit `param_lsb` is packed raw; resolving it
  against per-pipe context (last credit, two ack bases, two missing seqs) is runtime
  state, NOT byte-certified.
- **v0.1 scope:** codec-only (pack/unpack the 4-byte message). Active-Set mapping
  (local_idx ↔ session_id) is caller-managed, not byte-certified.
- **Byte-certified across C/Rust/Python.** Three vector families: main (pack→compare
  + round-trip), reject (illegal buffers raise), discriminator (is_multicontrol over
  MC + classic Pipe + audio). The 246-vector wire certificate and pipe_vectors.json
  are untouched.
- **References:** `python/MCP/pipemulti_core.py` (canonical), `codec/demod_pipemulti.h`
  (C), `codec/src/pipemulti.rs` (Rust). Vectors: `Documentation/pipemulti_vectors.json`
  (+ identical `python/MCP/` copy) and `codec/pipemulti_vectors.gen.h`. Spec:
  `Documentation/DCF_PIPE_MULTI_SPEC.md`.

```sh
python3 python/MCP/gen_pipemulti_vectors.py /tmp/mc.json          # regen + verify laws
cd codec && cargo test --test certify_pipemulti                   # Rust
gcc -std=c11 -I codec C_SDK/tests/test_pipemulti_certify.c -o /tmp/mc && /tmp/mc  # C
```

## DCF-Mesh (self-healing redundancy over the wire)

A control adapter (not a new wire format) for self-healing meshes: the payload of a
`ProtoMessage` of type **`MsgMesh = 11`**. Two messages — **REPORT** (node→master:
`node_id + [peer_id, status, rtt]` list) and **ROLE** (master→node: assigned role +
master_id) — are byte-certified across Python/C/Rust/Go, alongside the certified
algorithm primitives (`peer_status`, `group_of`, `dijkstra`, `select_routes`,
`elect`). Spec: `Documentation/DCF_MESH_SPEC.md`.

- The **self-healing runtime** drives those algorithms from live PING/PONG +
  REPORT/ROLE, and runs in the **Go, C, Rust, and Python** nodes: per-peer liveness
  FSM (window 5, fail 3 → Unreachable, ok 2 → recover), an AUTO/master loop (AUTO
  nodes REPORT; the master aggregates topology, runs `elect`, broadcasts ROLE), and
  **decentralized failover** (master Unreachable → local re-election of the lowest-id
  healthy node). Tick ~1s; a periodic `mesh-status` line shows roles/health.
- Algorithm + control references: `codec/demod_mesh.h` (C), `codec/src/mesh.rs`
  (Rust), `go/mesh/mesh.go` (Go), `python/MCP/meshlab_core.py` (Python). Runtimes:
  `go/node/mesh_runtime.go`, `C_SDK/node/dcf_mesh_runtime.h`, `rust/src/mesh_runtime.rs`,
  `python/dcf/mesh_runtime.py` (+ stdlib UDP node `python/dcf/{udp_node,proto}.py`).
- Vectors: `Documentation/mesh_vectors.json` (+ `python/MCP/` copy) and
  `codec/mesh_vectors.gen.h`. The runtime's *timing* is integration-tested, not
  vectored; the algorithms + control bytes are the contract.
- CLI (same flags everywhere): `<node> start --mode p2p|auto|master --node-id N
  [--master PEER] --peer id@host:port` — Go/C `dcfnode start`, Rust `dcf mesh`,
  Python `python3 python/dcf_node.py start`. The nodes interoperate (a Go master
  assigns roles to Rust/Python AUTO nodes). The wire is plaintext — deploy behind
  WireGuard (`Documentation/DCF_SECURITY_EXPOSURE.md`).

```sh
python3 python/MCP/gen_mesh_vectors.py /tmp/mv.json                              # regen + verify laws
cd codec && cargo test --test certify_mesh                                       # Rust
gcc -std=c11 -I codec C_SDK/tests/test_mesh_certify.c -lm -o /tmp/mc && /tmp/mc  # C
cd go && go test ./mesh/                                                         # Go
```

## DCF-FEC (forward error correction for lossy media)

The frame CRC only **detects** corruption; for RF/SDR and acoustic links that drop
bits, DCF-FEC adds **correction**. It is an adapter *around* a frame (the 17-byte
quantum and its 246-vector certificate are untouched): a systematic **Reed-Solomon**
code over **GF(2⁸)** (prim `0x11D`, generator α=2, fcr=0), default `2t=16` parity →
corrects **8 byte-errors** per codeword, plus a **block interleaver** so an RF burst
hits ≤1 byte/codeword. Spec: `Documentation/DCF_FEC_SPEC.md`.

- **Multi-codeword messages** (`encode_message`/`decode_message`): any-length payload
  is split into N equal blocks, RS-coded, and interleaved (corrects bursts up to `N·t`
  bytes) behind a self-protecting fixed-parity header (`[len u32 | nparity u8]`), so
  the receiver needs nothing out-of-band. No single-codeword 239-byte limit.
- **Certified in all 13 wire-codec languages** (Python/C/Rust/Go/Node/C++/Perl/Lua/
  Lisp/Haskell/Java/Kotlin/Swift), byte-identical, like SuperPack. References live
  next to each frame codec — `python/MCP/feclab_core.py`, `codec/demod_fec.h`,
  `codec/src/fec.rs`, `go/dcf/fec.go`, … (full table in the spec). Contract:
  `Documentation/fec_vectors.json` (+ identical `python/MCP/` copy) and
  `codec/fec_vectors.gen.h`. Anchor: RS parity is the byte-certified half; the
  **modem IQ/audio waveform that carries the bytes is analog/loopback-tested**, not
  vectored.
- Consumed by the C node modem: `dcfnode send-modem --fec` / `recv-modem` use the
  multi-codeword layer (`C_SDK/node/dcfnode.c`, `node/dcf_modem.h`), so a frame
  crosses a real FSK/PSK/QAM/AM medium and is *recovered*, not just dropped.

```sh
python3 python/MCP/gen_fec_vectors.py /tmp/fec.json                              # regen + verify laws
cd codec && cargo test --test certify_fec                                        # Rust
gcc -std=c11 -I codec C_SDK/tests/test_fec_certify.c -lm -o /tmp/fc && /tmp/fc   # C
cd go && go test ./dcf/ -run TestFEC                                             # Go
```

## Comms client (`client/`) — Tauri 2 end-user app

A cross-platform communications client (Rust core + Vue UI) on `dcf-rust-sdk` + the
`codec/` crate + cpal: Connect | Peers/Mesh | Messages | Jam (audio) | Wire inspector,
over the frequency-channel rendezvous. Real-time audio is **per-source** (reassembly
keyed by frame `src_id`). It records each participant as a **bit-exact Opus track** on a
sample-accurate timeline (`client/src-tauri/src/sync.rs`, unit-tested) and muxes a
multitrack `master.mka` + `mix.flac` via host **ffmpeg** (`recorder.rs`).

```sh
nix develop .#comms        # toolchain: rust + node + webkit + alsa + opus + cargo-tauri
cd client && npm install && cargo tauri dev
cd client/src-tauri && cargo test --features audio   # sync/channel/recorder (+ ffprobe)
cargo check --no-default-features                     # headless (no audio system libs)
```

The `audio` feature gates cpal/Opus/ogg so the mesh+messaging+wire client builds headless.
SDK additions for the client: `list_peers_detailed`/per-peer RTT, `run_ping_scheduler`,
`send_audio_dcf(.., channel)` (`rust/src/lib.rs`).

## C SDK (`C_SDK/`) — compiled spine vs. quarantine

Only four modules compile and ship in the **library** (the sound part):
`dcf_platform`, `dcf_error`, `dcf_ringbuf`, `dcf_connpool` (`DCF_SOURCES` in
`C_SDK/CMakeLists.txt`). Public headers declare more than the build delivers;
`include/experimental/`, `plugins/experimental/`, `tests/legacy/` are quarantined.

Separately, **`C_SDK/node/` builds `dcfnode`** (`-DDCF_BUILD_NODE=ON`, on by
default) — a real mesh node *independent of* the 4-module spine, on the header-only
certified codec (`../codec/demod_*.h`):
- **ProtoMessage/UDP transport** (`node/dcf_proto.h`, byte-identical to Go/Rust's
  envelope — golden vector in `tests/test_proto_certify.c`): `start`,
  `send-text/game/position`. **Meshes with the Go and Rust nodes.**
- **Faust-DSP modem** (`node/dcf_modem.h` + `codec/faust/dcf_modem.dsp`):
  `send-modem`/`recv-modem` carry a frame across a modulation **medium**
  (FSK/OOK/PSK/QAM). The byte↔symbol mapping is certified
  (`codec/demod_modulation.h`); the waveform is loopback-tested. See
  `Documentation/DCF_MODEM_SPEC.md`.

The **C++ binding** (`cpp/`) is the **supercharged gRPC node** `dcfcpp`
(`cpp/src/dcf_node.cpp`, `cpp/proto/dcf.proto`): unary `SendFrame`, bidirectional
`MeshStream` (frames + SuperPacks + game/audio/text adapter frames), `Ping`/RTT,
health + reflection — over the header-only `cpp/include/dcf/{frame,superpack}.hpp`.

```sh
cd C_SDK && cmake -B build -DDCF_BUILD_NODE=ON -DDCF_BUILD_EXAMPLES=OFF && cmake --build build && ctest --test-dir build
cd cpp && cmake -B build -DDCF_CPP_GRPC=ON && cmake --build build   # needs grpc+protobuf (use `nix build .#dcf-cpp`)
```

Both ship as hermetic Nix node images: `nix build .#docker-dcf-c` /
`.#docker-dcf-cpp` (entrypoints `dcfnode start` / `dcfcpp serve`); pushed via
`docker/build-and-push.sh` and exercised by `docker/mesh-interop-test.sh`.

## DCF-Steam (Steam-compatible multiplayer transport)

A **transport** under the wire (not a new format): `dcfcpp` also speaks Valve's
`ISteamNetworkingSockets` — Steam **P2P** for clients and **dedicated servers** that
are the Docker containers / runtime. Spec: `Documentation/DCF_STEAM_SPEC.md`.

- **One API, two backends** (same source recompiles): **GNS** — the open
  GameNetworkingSockets (BSD-3, nixpkgs `gamenetworkingsockets`), default + hermetic,
  built/tested in CI; **Steamworks** — the proprietary SDK (opt-in, developer-supplied)
  adds SDR relay, lobbies, server browser. Backends differ in ~6 calls behind
  `#if DCF_CPP_STEAM`; the send/recv/hub/framing path is shared, so the GNS build
  exercises the Steam build's wire logic.
- **Roles:** `dcfcpp serve-gns` / `serve-steam` = dedicated-server **hub** (forwards
  each DCF frame to the other clients, preserving reliability); `connect-gns
  --peer host:port` / `connect-steam --peer <identity>` = client/P2P. Transport
  datagram = `[flags u8][DCF payload]`, bit0 RELIABLE → `k_nSteamNetworkingSend_Reliable`.
- **Mappings:** DCF-Game `FLAG_RELIABLE`→reliable send; `dst` channel ↔ lobby;
  `src_id` ↔ SteamID slot (via `GMSG_JOIN`). Transport crypto (GNS/Steam AES) sits
  **beneath** the codec — the DCF payload stays plaintext (`DCF_SECURITY_EXPOSURE.md`).
- **References:** `cpp/include/dcf/{transport,net_steam}.hpp`, `cpp/src/net_steam.cpp`,
  `cpp/src/steam_{gameserver,lobby}.cpp` (`#ifdef DCF_CPP_STEAM`). Image:
  `nix build .#docker-dcf-gns` (entrypoint `dcfcpp serve-gns`, `27015/udp`); Steam
  server image `cpp/Dockerfile.steam` (non-hermetic).
- **Licensing:** GNS is BSD-3 (redistributable). The **Steamworks SDK is proprietary
  and NOT redistributable** — never commit/vendor it; only `redistributable_bin/`
  ships in object form; no `steam_appid.txt` in prod. Enforced by `cpp/.gitignore`
  and a `DCF_CPP_STEAM` CMake warning.

```sh
nix build .#dcf-cpp-gns        # open backend (hermetic)
cd cpp && cmake -B build -DDCF_CPP_GRPC=OFF -DDCF_CPP_GNS=ON \
  -DDCF_GNS_INCLUDE_DIR=$(nix eval --raw nixpkgs#gamenetworkingsockets)/include/GameNetworkingSockets \
  -DDCF_GNS_LIB_DIR=$(nix eval --raw nixpkgs#gamenetworkingsockets)/lib && \
  cmake --build build --target dcfcpp && (cd build && ctest -R gns_loopback)
```

## HydraModem (acoustic M-FSK transport)

A self-contained **C acoustic modem** (`hydramodem/`, LGPL-3.0, DeMoD LLC — relicensed from
Apache-2.0 on integration) that carries the 17-byte `DeModFrame` over **sound** — a transport *beneath* the wire quantum, like
UDP/Steam: it transports the frame **opaquely** (never parses it), so the 246-vector wire
certificate is **untouched**. Its on-air frame is `[preamble][sync 0x2DD4][interleave(conv(
payload + CRC16))]`; the CRC is the same CRC-16/CCITT-FALSE the tree certifies (anchor `0x29B1`).

It is materially more capable than the toy `C_SDK/node/dcf_modem.h` (16 samples/symbol, no
acquisition): continuous-phase **M-FSK** (binary default; 4/8/16-ary validated), preamble+sync
acquisition, **symbol-timing recovery to ±3000 ppm**, soft-Viterbi conv FEC + block interleaver,
one-shot **and** streaming RX. Two DSP backends behind `src/hydra_dsp.h`: a portable C reference
(`hydra_dsp_ref.c`, default `make`, zero deps) and the compiled Faust backend (`make faust`).
The default profile (48 kHz, 1000 baud, tones 2000/3000 Hz) is a **near-field / low-reverb /
cabled** link. Cross-compiles to RISC-V (StarFive JH7110); runs real-time on one U74 core.

- Upstream sources under `hydramodem/{src,faust,examples,tests,docs}` are unmodified except for
  the license relicense (LICENSE/NOTICE/README/`faust` header → LGPL-3.0); repo glue lives in
  `hydramodem/dcf-tools/`: `dcf_loopback.c` (interop — a real
  `DeModFrame` from `codec/demod_frame.h` survives TX→RX byte-exact, wire CRC valid, all FEC
  modes), `tx_campaign.c`/`rx_campaign.c` (streaming-RX PER harness keyed by a per-frame
  counter), `field-test.sh` (cabled-link PER over two ALSA devices — the `DCF_FIELD_USE.md`
  **T2** tier, pass = PER < 1% with FEC), and `link-meter.py` (live per-channel input-level
  meter for tuning an analog path — mixer routing/gain/cabling — until signal arrives). Nix:
  `nix build .#hydramodem`; CI job
  `certify-hydramodem` in `wire-certify.yml`.

The DSP is **authored in Faust** (`faust/*.dsp` + `demod_modem.lib` are normative);
the default `make` ships the byte-identical C reference (`hydra_dsp_ref.c`). The
**compiled-Faust** backend's adapters are **version-tolerant across Faust 2.72–2.85**
(`#if FAUST_REAL_CONTROLS` picks the old `control`+`compute` vs new `frame` `-os`
ABI; the buggy `-ftz 2` flag is dropped) — verified green on 2.72.14 / 2.83.1 /
2.85.5. Build/verify with `nix build .#hydramodem-faust` (hermetic, pinned Faust
2.72.14 for a cached reproducible build) or `nix develop .#hydramodem-faust && cd
hydramodem && make faust-check` (uses whatever Faust is on PATH). Details:
`hydramodem/docs/FAUST_MODERNIZATION.md`.

```sh
cd hydramodem && make check && make asan          # full suite (CRC 0x29B1, FEC, timing, fuzz)
nix build .#hydramodem-faust                       # compiled Faust DSP == reference (pinned Faust 2.72.14)
dcf-tools/build.sh && dcf-tools/build/dcf_loopback # DCF interop (byte-exact, wire CRC valid)
# cabled hardware PER over two interfaces (run per direction):
dcf-tools/field-test.sh --tx-dev plughw:3,0 --rx-dev plughw:4,0 -n 200 --keep OUT
```

## DCF-JANUS (STANAG-4748 acoustic transport)

A `janus:` **transport** (not a new wire format) that carries the 17-byte `DeModFrame` over a
**JANUS (NATO STANAG 4748)** underwater-acoustic link — FH-BFSK + convolutional FEC + a
variable-length cargo field. The frame rides as JANUS **cargo** (hex-encoded), so JANUS sits
*beneath* the wire quantum like UDP/audio/SDR; the 246-vector certificate is untouched. The
value is **interop with a ratified standard** (real JANUS gear), not a novel modem.

- `JanusTransport` (`python/dcf/transport.py`) subclasses `_DirMedium` like `AudioTransport`;
  its codec hooks **shell out to the GPL-3.0 janus-c reference** (`janus-tx`/`janus-rx`) as a
  **separate process** — never linked — so the LGPL library is unaffected (mere aggregation,
  like the `pw-play`/`ffmpeg` calls). The reference encoder/decoder make the waveform
  STANAG-compliant by construction. Factory string: `janus:in=,out=,pset=1,fs=48000`.
- janus-c loads its cargo plugins via `dlopen`, so the transport puts the plugins dir on
  `LD_LIBRARY_PATH` automatically (`../share/janus/plugins`). Default profile = parameter set 1
  (Initial JANUS band, 11520 Hz / 4160 Hz) at 48 kHz. Binaries via `$JANUS_TX`/`$JANUS_RX`/PATH.
- janus-c is an **optional GPL dependency**: a standalone `nix build .#janus-c` (kept out of
  every LGPL closure); the transport raises and the tests `skip` without it, so CI stays green.
  Spec: `Documentation/DCF_JANUS_SPEC.md`; GPL boundary: `LICENSING.md`.

```sh
nix build .#janus-c        # GPL reference (janus-tx/janus-rx + parameter sets + plugins)
JANUS_TX=$PWD/result/bin/janus-tx JANUS_RX=$PWD/result/bin/janus-rx \
  python3 -m unittest python/tests/test_transport -k janus -v   # DCF frame -> JANUS cargo -> frame
```

## DCF-Sense (configurable sensor telemetry)

A telemetry layer for many sensor nodes → one gateway over a wired audio-band link
(HydraModem) — built for greenhouses, medium/platform-agnostic. An **adapter + media-access
layer over the 17-byte frame** (certificate untouched). Python: `python/dcf/sense/`. Spec:
`Documentation/DCF_SENSE_SPEC.md`. **Phase 1 (MVP) shipped:** reading schema, TDMA/dedicated
MAC, node + gateway, loopback demo, tests.

- **Schema** (`schema.py`): one reading = one bare `DeModFrame` — `src_id`=node, payload
  `[sensor_type u8 | value i16 scaled | flags u8]`; `physical = raw/SCALE[type]`. Efficient,
  no adapter overhead. (Bundling via DCF-Game `EVENT` / SuperPack = Phase-2 options.)
- **MAC** (`mac.py`): HydraModem has no media access, so a shared line needs it — configurable
  `tdma` (slot per node, guard times), `dedicated`, `csma` (backoff), and `fdma` (per-node tone
  channels — `base_freq = base0 + k·spacing`, gateway runs one decoder/channel; capacity ×N).
- **node.py / gateway.py / config.py / network.py**: node reads→encodes→sends over any DCF
  `Transport`; gateway RX→`decode_reading`→egress; `build_network(SenseConfig, read, factory)`
  is the config-driven entry point. Runs over real HydraModem via `HydraTransport` (subprocess
  `frame_tx`/`frame_rx`) or in-process `HydraCffiTransport` (`dcf.hydramodem_cffi`, ctypes).
  Mesh = relay readings through `dcf.bridge.Bridge`. Portable C node skeleton:
  `hydramodem/dcf-tools/sense_node.c` (a C node's WAV decodes in the Python gateway).
- **`model.py`**: measured energy/throughput (conv = 396 ms/reading → ~134 nodes/channel @ 60s,
  ~28 mJ/reading) + vs LoRa/RS-485.

```sh
python3 python/dcf/sense/demo.py --nodes 4 --mac tdma --cycles 3 --csv /tmp/sense.csv
cd python && python3 -m unittest tests.test_sense_schema tests.test_sense_mac -v
```

## DCF-WASM (browser comms client)

The certified `codec/` compiled to `wasm32` (`codec-wasm/`, a wasm-bindgen
surface) drives the **same** redesigned comms UI (`client/src/App.vue`, shared via
an `@ipc` alias) in the browser, delivered as **one self-contained `index.html`**
(`web/`, base64-inlined wasm). Browsers can't open UDP, so a stateless WS↔UDP
relay (`web/bridge/`, `dcf-ws-bridge`) carries opaque datagrams to the plaintext
mesh — the codec runs in the browser, not the bridge. It speaks the bare-frame
dialect of `JS/nodejs/src/node.js` (frames batched into SuperPacks, rendezvous on
`dst`): Messages = DCF-Text on the active channel, Arena = DCF-Game on `active^1`,
Jam = DCF-Audio PCM-diag (CTRL). Host-only Recording/Radio/Opus are gated off via
`api.capabilities`. Spec: `Documentation/DCF_WASM_SPEC.md`. Deploy behind
WireGuard (`DCF_SECURITY_EXPOSURE.md`).

```sh
nix develop .#wasm
cd web && npm install && npm run build   # → web/dist/index.html (one file)
npm run certify                          # WASM byte-identical to golden vectors
cargo run --manifest-path bridge/Cargo.toml -- --listen 127.0.0.1:7000
```

The WASM links the same `dcf-wire-codec` as the native certs, so it stays
byte-identical to the 246-vector wire certificate + adapter vectors.

## Per-language build & test

| Dir | Build | Test |
|-----|-------|------|
| `codec/` (Rust wire+audio) | `cargo build` (`--features opus,pm`) | `cargo test --test certify --test certify_audio` |
| `rust/` (gRPC SDK) | `cargo build` | `cargo test` |
| `python/` | `pip install -r python/requirements.txt` | `pytest python/tests/` |
| `python/modem/` (FSK acoustic modem) | — | `python3 main.py --help` (uses `faust_jit.py`) |
| `go/` | `go build ./...` | `go test ./...` |
| `lisp/` | load `lisp/src/punctim.lisp` in SBCL (self-certifies on load) | `sbcl --non-interactive --load lisp/src/wire.lisp` |
| `Documentation/` (Sphinx) | `cd Documentation && pip install -r requirements.txt && make html` | — |

Lisp: `punctim.lisp` self-certifies the wire codec on load — all fixes F1–F9 are
folded into source (`punctim-hotfix.lisp` is now a thin shim), and `:cl-json-schema`
(not in Quicklisp) is already gone. `lisp/src/wire.lisp` is the dependency-free codec
the `certify-lisp` CI loads. StreamDB (`lisp/streamdb/`, a small C lib via CFFI, built by
the `streamdb` Nix package → `libstreamdb.so`) is Lisp-SDK-only.

## Nix / Docker

`flake.nix` exposes per-language packages (`dcf-c`, `dcf-cpp`, `dcf-go`,
`streamdb`, `dcf-python`, `dcf-rust`, `dcf-nodejs`, `dcf-perl`, `dcf-docs`,
`hydramodem`, `janus-c`) and a default devShell (`nix develop`) with all
toolchains. Top-level `Dockerfile`, `install_deps.sh`, and `*-edit-gen.sh`
bootstrap environments.

Node Docker images are hermetic Nix `dockerTools` builds (`nix build .#docker-<name>`,
or `docker/build-and-push.sh`): `docker-dcf-{go,rust,c,cpp,python,nodejs,gns}` (UDP/
gRPC/GNS nodes) and **`docker-hydramodem`** — the acoustic-modem toolbox
(`frame_tx`/`frame_rx`/`tx_campaign`/`rx_campaign`/`dcf_loopback`/`sense_node`/
`sstv_send`/`sstv_recv`/`snake_loopback` on PATH; a WAV/file PHY, default cmd = the
interop self-test). `snake_source`/`snake_mixer` need `snake_ipc.h` from the separate
DeMoD audio-stack repo (`$DEMOD_IPC_INCLUDE`) and are skipped in hermetic builds and **`docker-punctim`** — the
Common Lisp SDK CLI node (`punctim` + StreamDB; versioned `2.2.0`, default cmd = its
FiveAM self-test). The hermetic `docker-punctim` is now canonical for the
`alh477/punctim` image; the traditional `lisp/Dockerfile` remains as a fallback.
**`docker/docker-compose.yml`**
brings the backends up together (`docker compose -f docker/docker-compose.yml up`);
`--profile demo` adds a hydramodem acoustic file-link demo (one container modulates a
frame onto a shared volume, another demodulates it). Interop matrix:
`docker/mesh-interop-test.sh`.

## Conventions when changing the wire/audio path

- A change to any codec is a change to all of them. The certificate is the
  contract: regenerate the golden vectors, run the certs, keep the `Documentation/`
  and `python/MCP/` copies identical. CI fails on drift.
- New transports/codecs are adapters over `DeModFrame`, not new wire formats.
- The protocol is deliberately **encryption-free** for EAR/ITAR export compliance
  (`Documentation/Specs/export_compliance.markdown`). Do not add encryption to the
  core wire path. The flip side — the plaintext wire is fully readable by any on-path
  observer (membership, topology/RTT, roles, message contents) — is documented in
  `Documentation/DCF_SECURITY_EXPOSURE.md`, with a passive wiretap demo
  (`python/dcf_node.py wiretap`, `python/tests/test_eavesdrop_leak.py`). Deploy DCF
  inside WireGuard (or operator-supplied, export-compliant crypto) **beneath** the UDP
  socket — never in the codec.
- License is **LGPL-3.0** for the linkable library; GPL-3.0 is scoped to the DOOM
  example only. (The repo has historical README inconsistencies — see the code
  review.)
