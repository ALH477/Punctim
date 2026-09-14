# alh477/dcf-nodejs — DCF mesh node (Node.js)

The **DCF / Punctim** Node.js mesh node — a dependency-free stdlib `dgram` UDP node. It
carries the certified 17-byte **DeModFrame** as **bare frames batched into 32-byte
SuperPacks**, the same dialect as the Python node, so it **meshes with `alh477/dcf-python`**
(verified both directions).

Built hermetically with Nix (`dockerTools`). Entrypoint `dcf-node-js`; default command `recv --follow`.

## Run

```sh
# a persistent listener on a channel
docker run --rm -p 7801:7801/udp alh477/dcf-nodejs recv --follow --channel duet --port 7801

# unicast a message to peers
docker run --rm alh477/dcf-nodejs send "hello" --channel duet --peers HOST:7801
docker run --rm alh477/dcf-nodejs version
```

## Wire

- **Quantum:** 17-byte `DeModFrame` (version 1), CRC-16/CCITT-FALSE — certified, shared across SDKs.
- **Transport:** bare DeModFrame text frames batched into **SuperPacks** over UDP; meshes with
  `alh477/dcf-python`. (The Go/Rust/C nodes use a different ProtoMessage envelope.)
- **Adapter:** a faithful JS port of the certified DCF-Text L2 framing + SuperPack.

Tags: `latest`, `0.3.0`. Ports: `7801/udp`. License: **LGPL-3.0-only**.
Source & full docs: https://github.com/ALH477/Punctim
