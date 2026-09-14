# alh477/dcf-rs — DCF mesh node (Rust)

The **DCF / Punctim** Rust SDK node (`dcf`), the reference UDP node. It carries the
certified 17-byte **DeModFrame** over a binary **ProtoMessage / UDP** envelope and
**meshes with `alh477/dcf-go` and `alh477/dcf-c`**. A gRPC management port is also exposed.

Built hermetically with Nix (`dockerTools`). Entrypoint is the `dcf` binary; default command `start`.

## Run

```sh
# a listening node
docker run --rm -p 7777:7777/udp -p 50051:50051 alh477/dcf-rs start

docker run --rm alh477/dcf-rs version
docker run --rm alh477/dcf-rs status
docker run --rm alh477/dcf-rs send-position --peer-id p --x 1 --y 2 --z 3   # see `--help`
docker run --rm alh477/dcf-rs benchmark     --peer-id p --count 100
```

## Wire

- **Quantum:** 17-byte `DeModFrame` (version 1), CRC-16/CCITT-FALSE — byte-identical across SDKs.
- **Transport:** ProtoMessage over UDP (shared with Go/C); gRPC for management on `50051`.
- **Adapters:** DCF-Game and DCF-Audio (L2), certified against the shared golden vectors.

Tags: `latest`, `0.3.0`. Ports: `7777/udp`, `50051/tcp`. License: **LGPL-3.0-only**.
Source & full docs: https://github.com/ALH477/Punctim
