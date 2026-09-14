# alh477/dcf-cpp — DCF gRPC node (C++)

The **DCF / Punctim** C++ node (`dcfcpp`): a "supercharged" **gRPC** transport for the
certified 17-byte **DeModFrame** and 32-byte **SuperPack**. The headline is a
**bidirectional `MeshStream`** — two nodes exchange frames, SuperPacks, and PING/PONG in
both directions — plus unary `SendFrame`, server-streaming `Subscribe`, and gRPC **health +
reflection**.

Built hermetically with Nix (`dockerTools`). Entrypoint `dcfcpp`; default command `serve`.

## Run

```sh
# a server (health + reflection enabled)
docker run --rm -p 50051:50051 alh477/dcf-cpp serve --port 50051

# a client: ping (RTT) + a frame + a SuperPack over the bidi stream
docker run --rm alh477/dcf-cpp connect    --peer HOST:50051
docker run --rm alh477/dcf-cpp send-frame --peer HOST:50051
docker run --rm alh477/dcf-cpp bench      --peer HOST:50051 --count 100
docker run --rm alh477/dcf-cpp version
```

## Wire

- **Quantum:** 17-byte `DeModFrame` (+ 32-byte SuperPack), validated server-side via the
  certified header-only C++ codec.
- **Transport:** gRPC `DCFService` — unary `SendFrame`, `Subscribe` (server stream),
  `MeshStream` (bidi), `Ping`. Payloads are the raw certified wire bytes (frames / SuperPacks
  / game/audio/text adapter frames).

Tags: `latest`, `0.3.0`. Ports: `50051/tcp`. License: **LGPL-3.0-only**.
Source & full docs: https://github.com/ALH477/Punctim
