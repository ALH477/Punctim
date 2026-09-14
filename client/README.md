# Punctim — communications client

A dedicated, cross-platform **comms client** for the Punctim / DCF mesh, built on the
repo's own Rust crates: `dcf-rust-sdk` (`rust/` — node, transport, peers, mesh) and the
certified `dcf-wire-codec` (`codec/` — the 17-byte DeModFrame wire + DCF-Audio), with
**cpal** for real-time audio. Tauri 2 (Rust core + a lean Vue 3 UI).

**License:** LGPL-3.0-only, © 2026 DeMoD LLC (matches the repo).

## What it does

| Tab | |
|-----|--|
| **Connect** | configure node id / bind / UDP port; set the **rendezvous channel** (a numeric frequency or a shared passphrase). |
| **Peers** | add mesh peers (id / host / port). |
| **Messages** | reliable text/data over DCF, gated to the active channel. |
| **Jam** | real-time audio (Opus / PCM-diag / Faust-PM) over the mesh to peers on the same channel, with **multitrack recording**. |
| **Wire** | decode any DeModFrame hex — a built-in protocol inspector. |

### Recording (sample-synced, ffmpeg on the host)

Each participant (self + every peer) is recorded as a **bit-exact Opus track** (`-c:a copy`,
no re-encode) placed on a **shared, sample-accurate timeline**: within a source the spacing
is exact (`packet_id`×960 samples); across sources each track is anchored on the session t0
and gap/loss is filled with synthetic Opus silence so all tracks stay continuous and aligned.
On stop, host **ffmpeg** muxes a **multitrack master** (`master.mka`, one Opus track per
participant) and a **mixdown** (`mix.flac`, `amix`). The timeline math lives in `sync.rs`
(unit-tested) and the muxing in `recorder.rs`. Per-peer RTT (Peers tab) feeds the cross-source
one-way-delay anchor.

### Frequency rendezvous (handshakeless)

DCF has no connection handshake: peers **pre-agree on a channel** (the frame `dst`).
Tune a numeric channel, or derive one from a shared passphrase
(`channel_from_passphrase` = the certified CRC-16, identical to `lua/dcf_audio.lua`).
A node hears a frame iff `dst == its channel` or `dst == 0xFFFF` (broadcast). Both audio
and messages use this channel, so "same frequency → connected".

## Architecture

```
src-tauri/            Rust core (the comms engine)
  lib.rs              AppState + #[tauri::command]s (connect/send/jam/decode_frame/…) + run()
  engine.rs           UiHandler: impl MessageHandler -> emits `message` / `audio-level` events
  audio.rs            cpal capture/playback + jitter buffer + Opus (feature `audio`)
  channel.rs          frequency: numeric + passphrase (crc16) + accept filter
src/                  Vue 3 UI (App.vue, ipc.ts)
```

The receive path: SDK `run_udp_receiver` → `UiHandler::handle_audio` (channel-filtered) →
`reassemble_audio_payload` → decode → playback; `handle_game_event` → `message` event.
Send: mic → Opus `encode` → `DcfNode::send_audio_dcf(codec, bytes, pid, ts, channel)`.

## Build & run

```sh
nix develop .#comms          # toolchain: rust + node + webkit + alsa + opus + cargo-tauri
cd client
npm install
cargo tauri dev              # dev (hot-reload UI)
cargo tauri build            # bundle (appimage/deb; dmg/msi on mac/win)
```

Two instances on one machine (different UDP ports), same channel/passphrase → they jam
and chat; a third on a different channel hears nothing. (The same rendezvous is proven
headlessly in `rust/tests/comms_channel.rs`.)

### Headless / CI build

The audio stack is optional so the mesh + messaging + wire client builds without audio
system libs:

```sh
cargo check --no-default-features    # no cpal / libopus; Jam tab disabled
```

## Build & test

```sh
nix develop .#comms
cd client/src-tauri
cargo check --features audio          # full client (cpal + opus + ogg + recorder)
cargo test  --features audio          # sync timeline, channel, recorder (+ ffprobe multitrack)
cargo check --no-default-features     # headless (mesh/messaging/wire, no audio libs)
```

Verified in this tree: the sync/timeline math (6 tests), channel rendezvous (2), the
recorder — Ogg-Opus round-trip **and** an ffmpeg/ffprobe check that `master.mka` carries one
audio track per source (2 tests), the headless mesh+messaging path, and the SDK
`comms_channel` integration test. The cpal capture/playback (real mic/speaker) compiles and
is conventional but needs on-device validation; the Vue UI builds with `npm`.

## Status / refinements

Desktop-first (Linux/macOS/Windows); `rust-toolchain.toml` carries Android/iOS targets for a
future mobile build. Documented refinements: arbitrary-device sample-rate **resampling**
(v1 assumes a 48 kHz device), a proper per-source **jitter buffer** (v1 mixes on arrival),
long-session **clock-drift** correction, transports beyond UDP, and AEC/AGC.
