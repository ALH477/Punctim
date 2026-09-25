<!-- SPDX-License-Identifier: LGPL-3.0-only -->
# DCF-Steam — Steam-compatible multiplayer transport

DCF-Steam makes the DeMoD mesh speak **Valve's multiplayer networking**: Steam
**P2P** for clients and **dedicated servers** that are the Docker containers / node
runtime. It is a **transport** under the certified DCF wire — the 17-byte
`DeModFrame` quantum and every adapter (game/audio/text/superpack) are unchanged;
DCF-Steam only carries those bytes between peers.

This is implemented in the C++ node (`cpp/`, binary `dcfcpp`).

## One API, two backends

The transport is written **once** against Valve's `ISteamNetworkingSockets` API and
the backend is chosen at build time. The open and proprietary libraries share the
**same API and wire protocol**, so the same source recompiles against either:

| Backend | CMake | Library | What you get | Hermetic? |
|---------|-------|---------|--------------|-----------|
| **GNS** (default) | `-DDCF_CPP_GNS=ON` | GameNetworkingSockets (BSD-3, nixpkgs) | Dedicated server (`CreateListenSocketIP`), client (`ConnectByIPAddress`), hub forwarding, LAN/direct P2P, custom-signaling P2P | ✅ yes — built and loopback-tested locally (`cmake … -DDCF_CPP_GNS=ON`, `ctest -R gns_loopback`); `nix build .#dcf-cpp-gns` / `.#docker-dcf-gns` build it hermetically (build only, no test run); **not in hosted CI** |
| **Steamworks** (priority) | `-DDCF_CPP_STEAM=ON -DDCF_STEAMWORKS_SDK=…` | Proprietary Steamworks SDK | + **SDR relay** (NAT-punch, IP hiding), **lobbies**, **server browser**, Steam auth | ❌ developer-supplied |

Reference: `cpp/include/dcf/{transport,net_steam}.hpp`, `cpp/src/net_steam.cpp`
(shared), `cpp/src/steam_{gameserver,lobby}.cpp` (`#ifdef DCF_CPP_STEAM`).

The two backends differ in **~6 calls**, isolated behind `#if DCF_CPP_STEAM /
#else`: init, relay-init, listen (`CreateListenSocketP2P` vs `…IP`), connect
(`ConnectP2P` vs `ConnectByIPAddress`). The send/receive/poll loop, the hub
forwarding, and the DCF framing are identical — so the open GNS build **fully
exercises** the wire and reliability logic that the Steam build also runs.

## Roles

- **Dedicated server (the Docker container / runtime).** `dcfcpp serve-gns`
  (or `serve-steam`) opens a listen socket + poll group, accepts clients, and
  **forwards each DCF frame to the other clients** preserving the sender's
  reliability — a hub. Channel filtering stays the receiver's job (the DCF
  dst-channel rendezvous), exactly as elsewhere in DCF. Under Steamworks the server
  also registers with the **master server** (server browser) and may host a lobby.
- **Client / P2P.** `dcfcpp connect-gns --peer host:port` (direct/LAN/dedicated) or,
  under Steamworks, `connect-steam --peer <identity>` for **SDR-relayed P2P** (NAT
  traversal, IP hidden). With open GNS, NAT-punched internet P2P needs either a
  WebRTC-ICE GNS build (the nixpkgs build ships **without** WebRTC) or the Steam SDR
  backend; LAN/direct and hub-relayed paths work out of the box.

## Transport datagram

GNS does not report, on receive, whether a message arrived reliably — so a
forwarding hub could not preserve intent. Each transport message is therefore:

```
[ flags : u8 ][ DCF payload : N bytes ]      flags bit0 = RELIABLE
```

The payload is the **unmodified** DCF wire (a 17-byte frame, a 32-byte SuperPack, or
an adapter burst). The hub reads `flags`, forwards with the matching reliability
(`k_nSteamNetworkingSend_Reliable` / `_Unreliable`), and leaves the payload intact —
so the certified wire certificate is untouched.

## Mappings (DCF ↔ Steam)

| DCF | Steam | Notes |
|-----|-------|-------|
| DCF-Game `FLAG_RELIABLE`/`ORDERED` descriptor | `k_nSteamNetworkingSend_Reliable` | else `_Unreliable`; the game spec already says these flags "pick the transport path" |
| `dst` channel (u16 = `crc16(passphrase)`) | **lobby** / virtual port | handshakeless rendezvous ↔ a room/lobby; `0xFFFF` = broadcast/open |
| `src_id` (u16) | per-player identity | a SteamID64 is carried in a `GMSG_JOIN` and mapped to a u16 slot (collision-checked); `SteamNetworkingIdentity::SetGenericString` holds the node id for GNS |
| node / hub | `ISteamGameServer` dedicated server | anonymous or GSLT login; master-server heartbeats → server browser |
| reassembly keyed by `src_id` | per-connection messages | one reassembler per peer, as today |

## Encryption & export control

GameNetworkingSockets and Steam encrypt connections at the **transport** layer
(AES-GCM / Curve25519). That sits **beneath** the DCF codec — exactly the deployment
rule in `Documentation/DCF_SECURITY_EXPOSURE.md` ("deploy DCF inside WireGuard / an
operator-supplied transport crypto beneath the UDP socket — never in the codec").
The DCF application payload stays plaintext, so the encryption-free codec and its
EAR/ITAR posture are preserved; DCF-Steam simply gives you the operator-level
transport crypto that rule already calls for. Do **not** add crypto to the DCF codec.

## Build & run (open GNS — hermetic)

```sh
nix build .#dcf-cpp-gns                 # or: nix build .#docker-dcf-gns
cd cpp && cmake -B build -DDCF_CPP_GRPC=OFF -DDCF_CPP_GNS=ON \
  -DDCF_GNS_INCLUDE_DIR=$(nix eval --raw nixpkgs#gamenetworkingsockets)/include/GameNetworkingSockets \
  -DDCF_GNS_LIB_DIR=$(nix eval --raw nixpkgs#gamenetworkingsockets)/lib
cmake --build build --target dcfcpp && (cd build && ctest -R gns_loopback)

# dedicated server + two clients
dcfcpp serve-gns --port 27015
dcfcpp connect-gns --peer 127.0.0.1:27015 --listen 5                       # receiver
dcfcpp connect-gns --peer 127.0.0.1:27015 --send-hex <FRAME> --reliable    # sender
```

The nixpkgs `gamenetworkingsockets` CMake export has a doubled include path and a
dangling `protobuf::libprotobuf` in its link interface, so the build links the
library directly (`DCF_GNS_INCLUDE_DIR` / `DCF_GNS_LIB_DIR`) instead of relying on
`find_package` — the GNS `.so` resolves protobuf/openssl via rpath.

## Build & run (Steamworks — developer-supplied)

```sh
# 1. Download the Steamworks SDK under YOUR partner account; put it at
#    cpp/steamworks_sdk/ (gitignored). Create an App ID + dedicated-server App ID.
cmake -S cpp -B cpp/build-steam -DDCF_CPP_GRPC=OFF -DDCF_CPP_STEAM=ON \
  -DDCF_STEAMWORKS_SDK=$PWD/cpp/steamworks_sdk
cmake --build cpp/build-steam --target dcfcpp
# 2. dedicated server (anonymous or GSLT via $DCF_STEAM_GSLT); image: cpp/Dockerfile.steam
DCF_STEAM_GSLT=<token> dcfcpp serve-steam --port 16000 --node-id my-server
```

To verify SDR P2P, lobby create/join, and server-browser registration you need
**your** Steamworks SDK, an **App ID**, a `steam_appid.txt` (dev only), and a
**GSLT** (or anonymous login). Those are not present in this repo or CI, so the
Steam backend is **compile-guarded and unverified here**; the shared transport is
verified by the GNS `gns_loopback` test.

## Licensing

**This repository contains no Steamworks SDK material** — no Valve headers, no
libraries, no `steam_appid.txt`. The Steam backend (`cpp/src/steam_*.cpp`,
`net_steam.cpp`'s `#if DCF_CPP_STEAM` branches) is original LGPL-3.0 source that
*links* a developer-supplied SDK; it only references Valve's published API (writing
interoperable code), never copies Valve's declarations, and is excluded from the
default open build.

- **GameNetworkingSockets** — BSD-3-Clause; freely linkable from the LGPL-3.0 node,
  and packaged in nixpkgs. This is the default, redistributable backend.
- **Steamworks SDK** — proprietary, under the **Steamworks SDK Access Agreement**.
  It is **not redistributable**: do **not** commit or vendor the SDK source/headers.
  Only the contents of `redistributable_bin/` (`libsteam_api.so`, `steamclient.so`)
  may ship **in object form** with your built server — which is exactly what
  `cpp/Dockerfile.steam` bundles. Do **not** ship `steam_appid.txt` in a production
  image. `cpp/.gitignore` enforces this (SDK dirs, `steam_appid.txt`,
  `redistributable_bin/` are ignored), and the CMake `DCF_CPP_STEAM` path prints the
  same reminder. Each developer supplies the SDK under their own partner account.
