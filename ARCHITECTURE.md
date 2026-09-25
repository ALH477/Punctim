# Punctim / DCF — Architecture Map

A one-page orientation for newcomers. For frank, module-by-module status (what's
solid vs. drifted), read [`Documentation/DCF_CODE_REVIEW.md`](Documentation/DCF_CODE_REVIEW.md).

## The shape of the repo

Punctim is a polyglot monorepo. Its center of gravity is **narrow**: one wire
format, certified identical across languages, with everything richer layered on top
as adapters.

```
                ┌───────────────────────────────────────────────┐
                │  DeModFrame — the 17-byte wire quantum          │  ← the one invariant
                │  sync | flags | seq | src | dst | payload(4B)   │
                │  | ts24 | crc16   (version nibble = 1)          │
                └───────────────────────────────────────────────┘
                         ▲                          ▲
          adapters over it (no new wire format), byte-certified:
                         │                          │
            ┌────────────┴───────┐      ┌───────────┴────────────┐
            │  DCF-Audio (CTRL)  │      │  other adapters …       │
            │  20 ms codec block │      │  (transports, game, …)  │
            └────────────────────┘      └─────────────────────────┘
                         ▲
        per-language SDKs / bindings  (C, C++, Rust, Go, Python, Perl,
        Java/Kotlin, Swift, Node, Haskell, Lisp)
                         ▲
        end-user app: the Tauri comms client (`client/`)
```

## Layers

1. **The wire quantum** — `Documentation/WIRE_QUANTUM_SPEC.md`. A 17-byte
   `DeModFrame`, valid iff sync byte + version nibble + CRC-16/CCITT-FALSE over
   bytes `[0..14]`. Reference codecs (must stay byte-identical):
   `codec/demod_frame.h` (C), `codec/frame.rs` (Rust), `python/MCP/wirelab_core.py`
   (Python), plus Lua/Haskell/Lisp.

2. **Adapters** — anything richer is serialised into bursts of ordinary
   `DeModFrame`s, never a new wire format. **DCF-Audio**
   (`Documentation/DCF_AUDIO_SPEC.md`) is the worked example: a 20 ms codec block →
   `1 + ceil(len/4)` `CTRL` frames, with `codec_id` in the descriptor so adding
   codecs never changes the vectors. New adapters follow this exact pattern.

3. **SDKs** — each top-level language dir is an independent binding/SDK. The Rust
   SDK (`rust/`) carries the node/mesh/transport runtime used by the client.

4. **Client** — `client/` is a Tauri 2 app (Rust core + Vue UI): Connect, Peers,
   Messages, Jam (audio), Wire inspector, over the frequency-channel rendezvous.

## The medium layer (transports, beneath the quantum)

Transports sit *beneath* the quantum and carry the 17 bytes opaquely. **DCF-Medium**
(`Documentation/DCF_MEDIUM_SPEC.md`, normative) turns "a transport" into a contract, which
is what makes the protocol **deterministic across mediums**: any frame stream can be
moved between any two media, and every implementation writes the same bytes.

- **Codec model.** Each medium is a pair of total functions
  `encode : [frame] → representation` and `decode : representation → ([frame], diagnostics)`,
  `diagnostics = {skipped_bytes, bad_lines, invalid_frames}`. Classes: **stream**
  (`file:` `.dcf`, `stdio:` — concatenated frames, byte-wise resync scan, no COBS/SLIP),
  **text** (`hex:` — 34 hex digits per line), **datagram** (`udp:dialect=proto` — one
  34-byte ProtoMessage of type 12 per frame; `udp:dialect=bare` — SuperPack pairs;
  `l2eth:` — `[n u16][SuperPack…]` plus a zero filler; `loop:` — identity), **analog**
  (`hydra:` — the HydraModem M-FSK symbol stream; `afsk:` — the AFSK bit stream).
- **Laws** (every language's cert checks them): lossless; order-preserving; resync for
  the self-delimiting media; one **frame gate** (`0xD3` + version nibble 1 +
  CRC-16/CCITT-FALSE) — the only validity test; and **media never parse the frame**, so
  the 246-vector wire certificate and every adapter certificate are untouched.
- **Tiers.** *Byte-certified*: `stream`, `hex`, `udp_proto`, `udp_bare`, `l2eth`,
  `hydra_symbols`, `afsk_bits` — `Documentation/medium_vectors.json` (162 cases).
  *Loopback-tested*: HydraModem/AFSK WAV, SDR `.cf32`, JANUS WAV, the C DCFM waveform
  (certification stops at the symbol/bit stream, as for Opus). *Deferred to v0.2*: the
  `sdr_bytes` family, live tcp/serial/ws media, a conforming live-audio port, and
  `libhydramodem` linked into the C `punctim`.
- **`punctim io`** composes a single-threaded, ordered pipeline **reader → gate →
  writer**. Finite inputs (`stdio:`, `hex:`/`file:` without `follow`) are read to EOF and
  flushed, never dropping a frame; infinite inputs (UDP, L2, loop, WAV/IQ spool dirs)
  cross a bounded FIFO (`--queue`, default 256, oldest shed and counted) until
  `--count`/`--seconds`/`--expect`/SIGINT. A frame that fails the gate is counted in
  `invalid_frames` and not written. Media are named by URIs `SCHEME[:k=v,...]`; the
  grammar is single-sourced in `python/dcf/medium.py` (`parse_uri`) and tabulated in the
  spec. Normative rule: for finite inputs, `punctim io` in any language produces
  byte-identical output for identical input and URI.

| Lang | Medium codec | Cert | `punctim` |
|------|--------------|------|-----------|
| Python (canonical) | `python/MCP/mediumlab_core.py` (runtime `python/dcf/medium.py`) | `python/tests/test_medium.py` | `python/punctim.py` |
| C | `codec/demod_medium.h` | `C_SDK/tests/test_medium_certify.c` | `C_SDK/node/punctim.c` |
| Rust | `codec/src/medium.rs` | `codec/tests/certify_medium.rs` | `codec/src/bin/punctim.rs` |
| Go | `go/medium/medium.go` | `go/medium/medium_certify_test.go` | `go/cmd/punctim/` |
| Node.js | `JS/nodejs/src/medium.js` | `JS/nodejs/test/certify_medium.js` | `JS/nodejs/bin/punctim.js` |
| C++ / Java / Perl (5 digital families) | `cpp/include/dcf/medium.hpp`, `java/com/demod/dcf/Medium.java`, `perl/lib/DCF/Medium.pm` | `cpp/tests/certify_medium.cpp`, `java/com/demod/dcf/MediumCertify.java`, `perl/t/medium.t` | — |
| HydraModem (ground truth) | `hydramodem/src/hydra_frame.c` | `hydramodem/dcf-tools/hydra_symbols_certify.c` | `frame_tx` / `frame_rx` |

Cross-language interop is `make io-matrix` (`tests/io_matrix.py`: every writer × reader
pair of the five CLIs over file, stdio, UDP proto/bare and HydraModem WAV).

## Certification is the contract

The thing that keeps a polyglot codebase honest is a **finite golden-vector
certificate**: matching all vectors ≡ agreeing with the reference on the entire
input space. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the commands. CI
(`.github/workflows/wire-certify.yml`) is written to regenerate and diff on every
push/PR, but hosted Actions has never executed a job on this repository (an account
billing lock); until that clears, `make ci-local` and the dated attestations in
`.github/LOCAL_CI_RESULTS.md` are the certification path of record.

- `Documentation/golden_vectors.json` — 246-vector wire certificate.
- `Documentation/audio_vectors.json` (+ `pm_param_vectors.json`) — audio L2 framing.
- `Documentation/medium_vectors.json` — 162-case medium certificate (DCF-Medium).

## What ships vs. what's quarantined

Be careful: **public headers and READMEs sometimes describe more than the build
delivers.** Treat `Documentation/DCF_CODE_REVIEW.md` as the source of truth.

- **C SDK (`C_SDK/`)** ships four modules: `dcf_platform`, `dcf_error`,
  `dcf_ringbuf`, `dcf_connpool` (`DCF_SOURCES` in `C_SDK/CMakeLists.txt`).
  `include/experimental/`, `plugins/experimental/`, `tests/legacy/` are
  **quarantined** and do not build.
- **StreamDB** (`lisp/streamdb/`, Rust via CFFI) is Lisp-SDK-only.
- The canonical Python reference lives in `python/MCP/` (the wire/audio cert tools).

## Where to start reading

1. `Documentation/WIRE_QUANTUM_SPEC.md` — the frame.
2. `codec/demod_frame.h` + `codec/frame.rs` — the reference codec in two languages.
3. `Documentation/DCF_AUDIO_SPEC.md` + `codec/src/audio.rs` — how an adapter is built.
4. `Documentation/DCF_MEDIUM_SPEC.md` + `python/MCP/mediumlab_core.py` — how a medium is
   built, and the `punctim` contract.
5. `Documentation/DCF_CODE_REVIEW.md` — the honest status of everything else.
