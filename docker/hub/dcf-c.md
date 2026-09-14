# alh477/dcf-c — DCF mesh node (C) + Faust-DSP modem

The **DCF / Punctim** C SDK node (`dcfnode`). Two transports for the certified 17-byte
**DeModFrame**:

1. **ProtoMessage / UDP** — byte-identical to the Go/Rust nodes, so it **meshes with
   `alh477/dcf-go` and `alh477/dcf-rs`**.
2. **Faust-DSP modem** — carries a frame across a *modulation medium* (**FSK / OOK / PSK /
   QAM**), the "modulations across quanta mediums" path. The byte↔symbol mapping is certified
   across Python/Rust/C; the waveform is loopback-tested (like DCF-Audio synthesis).

Built hermetically with Nix (`dockerTools`). Entrypoint `dcfnode`; default command `start`.

## Run

```sh
docker run --rm -p 7777:7777/udp alh477/dcf-c start --bind 0.0.0.0:7777
docker run --rm alh477/dcf-c send-text     --peer HOST:7777 --channel lobby --text "hi"
docker run --rm alh477/dcf-c send-game     --peer HOST:7777 --channel-id 1 --hex deadbeef --type 2
docker run --rm alh477/dcf-c send-position --peer HOST:7777 --x 1 --y 2 --z 3

# Faust modem over a file "medium" (a shared volume stands in for the channel)
docker run --rm -v ch:/m alh477/dcf-c send-modem --medium /m/x.dcfm --modulation qam --text "hi"
docker run --rm -v ch:/m alh477/dcf-c recv-modem --medium /m/x.dcfm
```

## Wire

- **Quantum:** 17-byte `DeModFrame` — certified, shared across SDKs.
- **Transports:** ProtoMessage/UDP (mesh with Go/Rust); Faust modem (FSK/OOK/PSK/QAM).
- Spec: [`DCF_MODEM_SPEC.md`](https://github.com/ALH477/Punctim/blob/main/Documentation/DCF_MODEM_SPEC.md).

Tags: `latest`, `0.3.0`. Ports: `7777/udp`. License: **LGPL-3.0-only**.
Source & full docs: https://github.com/ALH477/Punctim
