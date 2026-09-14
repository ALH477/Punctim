# alh477/dcf-python — DCF mesh node (Python)

The **DCF / Punctim** Python mesh endpoint (`a2a` over `DcfTextNode`). It carries the
certified 17-byte **DeModFrame** as **bare frames batched into 32-byte SuperPacks** over UDP,
with a frequency-channel rendezvous on the frame `dst`. It **meshes with `alh477/dcf-nodejs`**
(the same bare-frame + SuperPack dialect).

Built hermetically with Nix (`dockerTools`). Entrypoint `a2a.py`; default command `recv --follow`.

## Run

```sh
# a persistent listener on a channel
docker run --rm -p 7801:7801/udp alh477/dcf-python recv --follow --channel duet --port 7801

# unicast a message to peers
docker run --rm alh477/dcf-python send "hello" --channel duet --peers HOST:7801
```

## Wire

- **Quantum:** 17-byte `DeModFrame` (version 1), CRC-16/CCITT-FALSE — certified, shared across SDKs.
- **Transport:** bare DeModFrame text frames batched into **SuperPacks** (34→32 bytes/pair) over UDP.
  Meshes with `alh477/dcf-nodejs`. (The Go/Rust/C nodes use a different ProtoMessage envelope.)
- **Adapter:** DCF-Text L2 framing (certified) + SuperPack (certified).

> DCF is encryption-free by design (export compliance); run it inside an encrypted underlay
> (WireGuard/Tailscale) for confidentiality.

Tags: `latest`, `0.3.0`. Ports: `7801/udp`. License: **LGPL-3.0-only**.
Source & full docs: https://github.com/ALH477/Punctim
