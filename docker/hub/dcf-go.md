# alh477/dcf-go — DCF mesh node (Go)

A real **DCF / Punctim** node built on the stdlib-only Go SDK
([`go/`](https://github.com/ALH477/Punctim/tree/main/go)). It carries the certified
17-byte **DeModFrame** wire quantum over a binary **ProtoMessage / UDP** envelope, so it
**meshes with `alh477/dcf-rs` and `alh477/dcf-c`**.

Built hermetically with Nix (`dockerTools`) — no apt/apk. Image entrypoint is the `dcfnode`
CLI; the default command is `start`.

## Run

```sh
# a listening node (receiver + ping + ARQ)
docker run --rm -p 7777:7777/udp alh477/dcf-go start --bind 0.0.0.0:7777

# send the certified adapters to a peer
docker run --rm alh477/dcf-go send-text     --peer HOST:7777 --channel 1 --text "hi"
docker run --rm alh477/dcf-go send-game     --peer HOST:7777 --channel 1 --hex deadbeef --type 2
docker run --rm alh477/dcf-go send-audio    --peer HOST:7777 --channel 1 --hex 00112233 --codec 1
docker run --rm alh477/dcf-go send-position --peer HOST:7777 --x 1 --y 2 --z 3
docker run --rm alh477/dcf-go benchmark     --peer HOST:7777 --count 100
docker run --rm alh477/dcf-go version
```

## Wire

- **Quantum:** 17-byte `DeModFrame` (version 1), CRC-16/CCITT-FALSE, byte-identical across all SDKs.
- **Transport:** ProtoMessage (`type|seq|ts|len|payload`, big-endian) over UDP — shared with Rust/C.
- **Adapters:** DCF-Game, DCF-Audio (L2), DCF-Text, all certified against the shared golden vectors.

Tags: `latest`, `0.3.0`. Ports: `7777/udp`. License: **LGPL-3.0-only**.
Source & full docs: https://github.com/ALH477/Punctim
