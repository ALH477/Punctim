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

## Certification is the contract

The thing that keeps a polyglot codebase honest is a **finite golden-vector
certificate**: matching all vectors ≡ agreeing with the reference on the entire
input space. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the commands. CI
(`.github/workflows/wire-certify.yml`) regenerates and diffs on every push/PR.

- `Documentation/golden_vectors.json` — 246-vector wire certificate.
- `Documentation/audio_vectors.json` (+ `pm_param_vectors.json`) — audio L2 framing.

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
4. `Documentation/DCF_CODE_REVIEW.md` — the honest status of everything else.
