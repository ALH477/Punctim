<!-- SPDX-License-Identifier: LGPL-3.0-only -->
# Punctim — browser WASM comms client

The DCF protocol compiled to WebAssembly, driving the same redesigned comms UI as
the desktop client (`client/src/App.vue`, shared via the `@ipc` alias), delivered
as **one self-contained `index.html`** (the wasm is base64-inlined). Browsers
can't open raw UDP, so it talks to the mesh through `dcf-ws-bridge` — a stateless
WebSocket↔UDP relay that only shuttles datagrams; the codec runs in the browser.

See `../Documentation/DCF_WASM_SPEC.md` for the full design, dialect, and routing.

## Build (one HTML file)

```sh
nix develop .#wasm           # rust+wasm32, wasm-pack, binaryen, node
cd web
npm install
npm run build                # → web/dist/index.html  (no other assets)
```

`npm run build` runs `wasm-pack` (→ `src/wasm/`), inlines the wasm as base64
(`scripts/inline-wasm.mjs`), then `vite build` folds everything into `dist/index.html`.

## Run

```sh
# 1) start the relay (deploy behind WireGuard — the DCF wire is plaintext)
cargo run --manifest-path bridge/Cargo.toml -- --listen 127.0.0.1:7000
# 2) serve the file (Jam needs a secure context for the mic; Messages/Arena/Wire
#    work from file:// too)
python3 -m http.server -d dist 8080      # open http://127.0.0.1:8080/
```

Point the client at a different relay with `?bridge=ws://host:port` (or
`localStorage.dcf_bridge`). In the app: set your node id, **Connect**, tune a
channel, **+ peer** a mesh node, and Messages/Arena go live. It interoperates with
`node ../JS/nodejs/src/node.js` on the same channel.

## Verify

```sh
npm run certify          # WASM byte-identical to Documentation/*_vectors.json (88 checks)
npm run test:loopback    # WASM + bridge move frames both ways over real UDP/WS (Node ≥ 22)
npm run typecheck        # vue-tsc over the shared App.vue + wasm-ipc.ts
```

## Capabilities

`api.capabilities = { recording:false, radio:false, opus:false }` in the browser.
Messages (DCF-Text), Arena (DCF-Game on `channel ^ 1`), and the Wire inspector are
fully live; **Jam** runs PCM-diag in WASM (needs a served secure context for mic
access). Multitrack recording, Radio (HLS), and Opus are host-only and hidden here.

## Files

| Path | Role |
|------|------|
| `../codec-wasm/` | wasm-bindgen surface over the certified `dcf-wire-codec` |
| `src/wasm-ipc.ts` | the `@ipc` transport: WASM codec + WS bridge + Web-Audio Jam |
| `src/wasm/` | wasm-pack output + base64-inlined wasm (gitignored; built by `npm run build`) |
| `bridge/` | `dcf-ws-bridge` — the WS↔UDP relay |
| `certify/` | byte-cert + headless loopback test |
