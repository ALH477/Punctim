# alh477/hydramodem — HydraModem acoustic modem toolbox

**HydraModem** carries the certified 17-byte **DeModFrame** of the DCF / Punctim protocol
over **sound** (a WAV/file or line/acoustic medium) — continuous-phase **M-FSK** with
preamble/sync acquisition, symbol-timing recovery (±3000 ppm), and soft-Viterbi convolutional
FEC. It is a *transport beneath the wire quantum*: the frame rides opaquely, so the 246-vector
wire certificate is untouched.

This image is the **toolbox** (not a network daemon): the CLI tools are on `PATH`, and the
default command runs the DCF interop self-test (a real frame through TX→RX, byte-exact).

Built hermetically with Nix (`dockerTools`).

## Tools

| tool | purpose |
|------|---------|
| `frame_tx <34-hex> out.wav [--conv|--rep3|--none] [--base-freq HZ …]` | modulate one frame → WAV |
| `frame_rx in.wav [--conv|--rep3|--none] [--base-freq HZ …]` | demodulate a WAV → frame hex |
| `tx_campaign N out.wav` / `rx_campaign in.wav N` | multi-frame PER campaigns |
| `dcf_loopback` | interop self-test (default cmd) |
| `sense_node <node_id_hex> <sensor_type> <count>` | DCF-Sense reference sensor node |

## Run

```sh
# interop self-test (default)
docker run --rm alh477/hydramodem

# modulate a frame to a WAV, then demodulate it (a shared volume = the "wire")
docker run --rm -v "$PWD:/m" alh477/hydramodem frame_tx d310000100a1ffffdeadbeef0a1b2c6242 /m/f.wav
docker run --rm -v "$PWD:/m" alh477/hydramodem frame_rx /m/f.wav

# bring it up alongside the DCF node backends:
#   docker compose -f docker/docker-compose.yml --profile demo up
```

## Notes

- **Acoustic PHY, not a UDP node** — it produces/consumes WAV; wire it to ALSA/PipeWire/JACK
  (or a file medium) at the app layer. For networked meshing use `alh477/dcf-{go,rs,c,cpp,python,nodejs}`.
- **FDMA** channels via `--base-freq`/`--tone-spacing`; multiple nodes share one line.
- Powers **DCF-Sense** (wired sensor telemetry, e.g. greenhouses) — see the spec.

Specs: [`HydraModem`](https://github.com/ALH477/Punctim/blob/main/hydramodem/README.md) ·
[`DCF_SENSE_SPEC.md`](https://github.com/ALH477/Punctim/blob/main/Documentation/DCF_SENSE_SPEC.md).

Tags: `latest`, `0.3.0` (image); HydraModem component v1.0.0. License: **LGPL-3.0-only**.
Source & full docs: https://github.com/ALH477/Punctim
