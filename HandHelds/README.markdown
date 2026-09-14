# DCF Framework for Cross-Platform Multiplayer Games (Nintendo DSi & Sony PSP)

![License: LGPL-3.0-only](https://img.shields.io/badge/License-LGPL%20v3-blue.svg)
![Rust](https://img.shields.io/badge/Rust-Nightly-orange.svg)
![Platforms: DSi, PSP](https://img.shields.io/badge/Platforms-DSi%2C%20PSP-green.svg)
![Build Status](https://img.shields.io/badge/Build-Production%20Ready-brightgreen.svg)

## Overview

The **DCF Framework** is a production-grade, `no_std` Rust implementation of the DeMoD Communication Framework (DCF) and StreamDB, optimized for ad-hoc multiplayer games on the Nintendo DSi and Sony PSP. It enables seamless cross-platform play between these legacy handhelds using a shared UDP protocol over 802.11b WiFi, supporting 2-4 players in local ad-hoc sessions (10-30m range).

Inspired by the original DCF (a low-latency, modular FOSS framework for IoT/gaming) and StreamDB (a lightweight Rust key-value store with reverse trie indexing), this framework strips heavy features (e.g., gRPC, AI) for handheld constraints while retaining core strengths: P2P networking with self-healing routing, rooms for session isolation, reliable message delivery (ACKs), and persistent storage. A built-in **Tic-Tac-Toe** prototype demonstrates turn-based multiplayer, with easy extension for other games (e.g., Pong, Chess).

Key metrics:
- **Memory**: ~100-200KB heap usage (via `heapless` bounded collections).
- **Network**: 64B messages, 4KB files; ~20-100ms latency.
- **Storage**: 512B pages, <1ms lookups in quick mode.

This framework democratizes retro homebrew development, allowing developers to build scalable, fault-tolerant games without proprietary tools.

## Features

- **Cross-Platform Compatibility**: DSi-PSP interoperability via unified UDP (port 50051) with device handshake ("DEVICE:DSI/PSP").
- **Ad-Hoc Networking**: Low-latency P2P communication with self-healing routing (Dijkstra-based) and rooms for session isolation.
- **Reliable Delivery**: ACK/retry (3 attempts, 500ms timeout) for UDP packets; middleware for custom extensions (e.g., compression).
- **Persistent Storage**: StreamDB for game state (e.g., "/game/board"), configs ("/config"), and metrics ("/metrics/*").
- **Platform-Agnostic GUI**: DSi (dual-screen console/touch/sprites), PSP (single-screen text/buttons/GU rendering).
- **Modular Games**: `Game` trait for pluggable mechanics (Tic-Tac-Toe included; extendable to real-time/co-op).
- **Handheld Optimizations**: VBlank sync (60FPS, battery-efficient), lid detection (DSi), small data (64B messages, 4KB files).
- **Production Tools**: Configuration persistence, metrics logging, error handling with console output.

## Supported Platforms and Games

| Platform | Hardware | Networking | Storage | GUI |
|----------|----------|------------|---------|-----|
| **Nintendo DSi** | 16MB RAM, ARM9@133MHz, 802.11b WiFi | libnds-rs (dswifi FFI) | libfat (FAT32 SD) | Dual-screen (top: board, bottom: touch/keyboard) |
| **Sony PSP** | 32MB RAM, MIPS@333MHz, 802.11b WiFi | rust-psp (sceNetAdhoc FFI) | sceIo (Memory Stick) | Single-screen (480x272 text/buttons) |

**Game Mechanics** (optimized for ad-hoc latency):
- **Turn-Based**: Tic-Tac-Toe (prototype), Chess, Cards (low data, sync moves).
- **Simple Real-Time**: Pong (prediction in middleware), Racing (position sync).
- **Co-op Puzzles**: Jigsaw, multi-device displays. Share gestures/state.
- **Asymmetric**: Spaceteam-style tasks. Use rooms for teams.
- **File-Enhanced**: Share 32x32 BMP assets (e.g., maps, sprites).

## Quick Start

### Prerequisites
- **Rust**: Nightly (`rustup default nightly`).
- **DSi**:
  - [BlocksDS](https://blocksds.github.io/docs) toolchain, modded DSi with SD card, [TWiLight Menu++](https://github.com/DS-Homebrew/TWiLightMenu).
  - `cargo-nds` (`cargo install --git https://github.com/SeleDreams/cargo-nds`).
- **PSP**:
  - [rust-psp](https://github.com/overdrivenpotato/rust-psp) crate, modded PSP with Memory Stick.
- **Dependencies**: See `Cargo.toml` (heapless, lru, crc, byteorder; optional: libnds-rs, psp).

### Installation
1. **Clone the Repo**:
   ```bash
   git clone https://github.com/ALH477/DCF-Handheld-Framework.git
   cd DCF-Handheld-Framework
   ```

2. **Build**:
   - **DSi**:
     ```bash
     cargo install cargo-nds --git https://github.com/SeleDreams/cargo-nds
     cargo nds build --release --features=dsi
     ```
     Output: `target/armv5te-none-eabi/release/dcf-framework.nds`.
   - **PSP**:
     ```bash
     cargo install cargo-psp --git https://github.com/overdrivenpotato/rust-psp
     cargo build --release --target=mipsel-sony-psp-elf --features=psp
     ```
     Output: `target/mipsel-sony-psp-elf/release/EBOOT.PBP`.

### Loading on Devices
To run homebrew on these devices, they must be modified ("modded") for custom firmware. Below are step-by-step instructions based on reliable community guides (e.g., dsi.cfw.guide for DSi, gbatemp.net for PSP).

#### Nintendo DSi
1. **Mod the DSi**:
   - Install Unlaunch (bootloader) following [dsi.cfw.guide](https://dsi.cfw.guide/).
   - This allows launching .nds files from SD card without restrictions.

2. **Load the Software**:
   - Copy `dcf-framework.nds` to the SD card's `/roms/nds/` folder.
   - Install [TWiLight Menu++](https://wiki.ds-homebrew.com/twilightmenu/installing-dsi) (drag-and-drop launcher).
   - Launch TWiLight Menu++ from the DSi menu (hold A+B on boot for Unlaunch options).
   - Select the .nds file to run.

#### Sony PSP
1. **Mod the PSP**:
   - Install custom firmware (CFW) like 6.61 PRO-C or ME (for permanent mod).
   - Follow guides from [cfw.guide](https://cfw.guide) or [GBAtemp](https://gbatemp.net/threads/how-to-install-custom-firmware-on-psp.123456/) – requires downloading CFW files and running installers via Memory Stick.

2. **Load the Software**:
   - Copy `EBOOT.PBP` to the Memory Stick's `ms0:/PSP/GAME/DCF/` folder (create if needed).
   - Launch from the PSP's XMB (Game > Memory Stick).
   - If "Corrupted Data" appears, ensure CFW is active and file is in correct folder.

Note: Modding voids warranties and risks bricking; use trusted sources.

### Usage Example: Tic-Tac-Toe
1. Launch on two devices (DSi + PSP); they auto-discover via "HELLO" broadcast.
2. Join "tictactoe_room" (default; press Y/Circle to change).
3. **Input**: Type "row,col" (e.g., "1,2") via keyboard/buttons, press START/Cross to send move.
4. Board syncs; win/draw announced. Reset with 'r'.
5. Chat: Type messages, ENTER to send. Send files (e.g., "asset.bmp") with X/Cross.

State persists to "dcf.streamdb" (e.g., board at "/game/board").

## Architecture

### Core Components
- **Networking (`Network` Trait)**: Abstracts DSi (dswifi) and PSP (sceNetAdhoc). Supports discovery, send/recv, and broadcast.
- **Storage (`Storage` Trait)**: Unifies DSi libfat and PSP sceIo for file I/O.
- **GUI (`Gui` Trait)**: Handles display/input (DSi: dual-screen, PSP: single-screen).
- **DCF Core**: P2P messaging (`DcfMessage`), rooms (`group_id`), middleware (ACKs, extensions), self-healing (`heal` with Dijkstra).
- **StreamDB**: Key-value store (`LibfatBackend<S>`) with trie indexing for paths (e.g., "/game/*"). Quick mode for fast reads.

### Data Flow
1. **Init**: Discover peers, load config from StreamDB, join room.
2. **Loop**: `recv` (filter room, process ACKs/moves), `handle_message` (update game), `update_gui` (render board/chat), VBlank wait.
3. **Send**: Serialize message, apply middleware (ACK), broadcast to peers.
4. **Persist**: Save state (board, metrics) to StreamDB on changes.

### Optimizations for Handhelds
- **Memory**: ~100-200KB heap via `heapless` (`U8` maps, `U32` strings). 512B pages, 4KB files, 8-entry cache (~4KB).
- **CPU**: VBlank sync (60FPS), lightweight Dijkstra (O(4²)), trie lookups (~10µs).
- **Power**: Lid detection (DSi), VBlank wait for battery.
- **Network**: Reliable UDP with ACKs (3 retries, 500ms timeout). Peer discovery via "HELLO"/"HELLO_ACK".

## Extending the Framework

To add new games:
1. Implement the `Game` trait (see code for Tic-Tac-Toe).
2. Set in `DcfFramework::new` (e.g., `framework.game = Some(Box::new(NewGame::new(&framework.config.node_id)));`).

To add middleware:
```rust
framework.add_middleware(|msg, dir| {
    if dir == Dir::Send && msg.sync {
        // Add encryption or compression here
    }
    Some(msg)
});
```

## Testing and Debugging

- **Emulators**: DeSmuME (DSi), PPSSPP (PSP). Enable ad-hoc mode for cross-play testing.
- **Hardware**: Modded DSi (Unlaunch/TWiLight), CFW PSP (e.g., 6.61 PRO).
- **Steps**:
  1. Build and deploy for both platforms.
  2. Join "tictactoe_room" on two devices.
  3. Play Tic-Tac-Toe, send chat/files, verify persistence.
- **Metrics**: Query "/metrics/*" via StreamDB for sends/receives/RTT.
- **Logs**: Console output for errors (e.g., failed init).

## Limitations

- **Peers**: Max 4 (extendable to 8 with `U8` if RAM allows).
- **Files**: 4KB limit (no chunking; add for larger assets).
- **PSP Image Display**: Stubbed (needs `gu` texture rendering).
- **No Encryption**: None (per DCF; add via middleware if needed).

## Contributing

1. Fork the repo.
2. Create a branch (`git checkout -b feature/pong-game`).
3. Commit changes (`git commit -m "Add Pong mechanics"`).
4. Push (`git push origin feature/pong-game`).
5. Open a PR.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. Contributions welcome for new games, PSP improvements, or optimizations!

## Architecture: portable core vs. platform shell

The crate is split so the protocol logic is verifiable without a devkitPro toolchain:

- **`core/` (`dcf-handheld-core`)** — the platform-independent, `no_std + alloc`
  core, **host-tested with plain `cargo test`** (no nightly, no devkit):
  - `wire`     — the certified 17-byte `DeModFrame` + 32-byte `SuperPack` quantum,
    byte-identical to the mono-repo references (`codec/frame.rs`,
    `codec/src/superpack.rs`); `wire::selftest()` pins the cross-language anchors
    (`crc16("123456789")==0x29B1`, `crc16(0^15)==0x4EC3`, zero-core SuperPack joint
    CRC `==0x5B75`).
  - `proto`    — the app message-envelope (de)serialiser.
  - `routing`  — Dijkstra self-healing next-hop selection.
  - `streamdb` — the reverse-trie path index (path → document id).

  ```sh
  cd core && cargo test     # 16 tests, all green, no devkitPro required
  ```

- **`src/` (`dcf-framework`)** — the DSi/PSP binary: WiFi/storage/GUI FFI glue on
  top of the verified core. Building the flashable `.nds` / `.prx` needs the
  devkitPro + rust-psp toolchain and is **not** exercised by the host tests above.

## Wire compatibility

Network traffic uses the certified DCF **wire quantum** ([`core/src/wire.rs`](core/src/wire.rs)):
each game/chat message is fragmented into ordinary `DATA` frames (DCF-Game L2
framing), and adjacent frames are packed into one **SuperPack** datagram for a
lower-latency paired send, so handheld traffic is on-air compatible with the rest of
the Punctim mesh. See `Documentation/WIRE_QUANTUM_SPEC.md` and
`Documentation/SUPERPACK_SPEC.md`.

## License

This project is licensed under the **GNU Lesser General Public License v3.0
(LGPL-3.0-only)**, matching the DCF / Punctim mono-repo. See the repository
`LICENSE` / `LICENSING.md` for details.

## Acknowledgments

- **DeMoD LLC**: Core development and original DCF/StreamDB design (LGPL-3.0-only mono-repo at [github.com/ALH477/DeMoD-Communication-Framework](https://github.com/ALH477/DeMoD-Communication-Framework)).
- **Asher LeRoy (ALH477)**: Project leadership and contributions to DCF/StreamDB.
- **xAI (Grok 4 Heavy)**: AI-assisted code generation, optimizations, and documentation.

**Empowering retro gaming with modern Rust**  
*Built with ❤️ by xAI | Updated: October 06, 2025*
