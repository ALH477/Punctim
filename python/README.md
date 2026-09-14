# demod-dcf — DeMoD Communications Framework (DCF / Punctim), Python SDK

A handshakeless, **encryption-free**, export-control-compliant mesh protocol built on
one tiny invariant: the **17-byte `DeModFrame` wire quantum**, certified byte-identical
across 13 languages. This package is the Python SDK — a stdlib self-healing **UDP mesh
node** that interoperates with the Go/C/Rust nodes, plus a **Reed-Solomon-FEC SDR
modem** so the *same frame* meshes over UDP **or real radio**.

```bash
pip install demod-dcf            # core: mesh node + SDR modem (numpy)
pip install "demod-dcf[grpc]"    # + the gRPC client/master
```

## Mesh over radio (no internet, error-corrected)

```bash
# frame -> RS-FEC -> GFSK IQ -> .cf32 file -> recovered frame  (no hardware needed)
dcf-sdr tx --text "DCF!" --mod gfsk --iq /tmp/d.cf32
dcf-sdr rx --iq /tmp/d.cf32 --mod gfsk          # -> recovers "DCF!", CRC valid

# real radio (RX is license-free; TX needs a license / ISM band):
dcf-sdr tx --text "DCF!" --soapy driver=hackrf --freq 433.9M --rate 2M
dcf-sdr rx              --soapy driver=rtlsdr --freq 433.9M --rate 2M --secs 3
```

Modulations: **GFSK / QPSK / 16-QAM / OOK·AM / AFSK-over-FM**. SoapySDR is a soft
import — `.cf32` files (interop with `rtl_sdr` / `hackrf_transfer` / GNU Radio) and the
software loopback always work without hardware. The **RS-FEC bytes are certified
byte-for-byte in all 13 wire-codec languages**; the IQ waveform is loopback-tested.

## Stdlib UDP mesh node

```bash
dcf-node start --mode auto --node-id 2 --master 1@master.host:9100 \
               --peer 3@peer3.host:9100
```

No third-party dependencies for the node itself — a peer-health FSM, an AUTO/master
election loop, and decentralized failover, speaking the same binary `ProtoMessage`
envelope as the Go/C/Rust nodes.

## Library

```python
from dcf import DcfNode, MeshRuntime, ProtoMessage      # stdlib UDP node + mesh runtime
from dcf.MCP import wirelab_core as wire                # the certified wire codec
frame = wire.encode(3, seq=1, src=0x00A1, dst=0xFFFF, payload=b"hi\x00\x00", ts=0)
assert wire.decode(frame)["src"] == 0x00A1
```

## Security note

The DCF wire is **plaintext by design** (EAR/ITAR export compliance). On UDP, deploy it
beneath WireGuard; **on RF there is no WireGuard** — treat an over-the-air link as a
public broadcast and apply operator-supplied, export-compliant crypto *above* the frame
if you need confidentiality.

- Source & full docs: https://github.com/ALH477/Punctim
- License: **LGPL-3.0-only**
