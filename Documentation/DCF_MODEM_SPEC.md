# DCF Modem — modulations across quanta mediums

Status: the **byte↔symbol mapping** is certified across Python/Rust/C
(`Documentation/modulation_vectors.json`); the **Faust waveform** synthesis/recovery
is loopback-tested, not byte-certified (same policy as DCF-Audio's Opus/PM synthesis).
Shipped in the C SDK node (`dcfnode send-modem` / `recv-modem`). HydraModem
(`hydramodem/`, the `hydra:` medium) is a separate modem whose **symbol stream** is
certified on its own (`hydra_symbols`, see [Certification](#certification)).

## Motivation

The 17-byte `DeModFrame` (see `WIRE_QUANTUM_SPEC.md`) is the one wire invariant. The
modem is a **transport/carrier** over it — not a new wire format — that puts frames
(and SuperPacks) onto a *modulated signal* so a node can mesh across a physical
**medium** (acoustic over speaker↔mic, RF, wire) rather than only over IP. It is the
C SDK's networking story alongside the conventional ProtoMessage/UDP transport.

## Two layers (one certified, one not)

1. **Byte↔symbol mapping — CERTIFIED.** A pure-integer, lossless map from frame bytes
   to Gray-coded symbol indices and back. Identical in `python/MCP/modulationlab_core.py`,
   `codec/demod_modulation.h`, and `codec/src/modulation.rs`.
2. **Waveform synthesis/recovery — NOT byte-certified.** Rendering a symbol onto a
   carrier (FSK tones, OOK, PSK phase, QAM I/Q) and recovering it is analog. The C
   reference (`C_SDK/node/dcf_modem.h`) renders sample snippets and recovers them by
   matched filtering — **exact over an ideal (loopback/file) medium**, robust under
   mild noise on a real one. The normative audio-band signal design is
   `codec/faust/dcf_modem.dsp` (the `DCF_MODEM_AUDIO` live path).

This mirrors DCF-Audio exactly: the L2/mapping layer is the contract; the synthesised
signal is not certified across languages.

## Modulation registry

| id | scheme | bits/symbol | carrier design (dcf_modem.h / dcf_modem.dsp) |
|----|--------|-------------|-----------------------------------------------|
| 0 | FSK | 1 | Bell-202-style mark/space tones (f1/f0) |
| 1 | OOK | 1 | on/off keying of a single carrier |
| 2 | PSK | 2 | QPSK, carrier phase {0,90,180,270}° (Gray) |
| 3 | QAM | 4 | 16-QAM, I/Q levels {-3,-1,1,3} (Gray) |

## Acoustic medium profiles

The live audio path (`python/modem/`) defines three medium profiles optimized for different physical channels. All use FSK (modulation id 0) with Bell-202-style mark/space tones, but differ in baud rate, frequency placement, and preamble length:

| profile | mark (Hz) | space (Hz) | baud | preamble | use case |
|---------|-----------|------------|------|----------|----------|
| standard | 1200 | 2200 | 300 | 80 bits | acoustic (speaker↔mic), Bell-202 AFSK |
| handheld | 1200 | 1800 | 300 | 240 bits | walkie-talkie radio, mid-band tones, long AGC keyup |
| aux-cable | 1000 | 1500 | 1200 | 16 bits | wired line-level (3.5mm/TRS), 4× faster |

The `aux-cable` profile is optimized for **wired connections** where the channel has flat frequency response, no AGC settling, and minimal noise. The higher baud rate (1200 vs 300) and shorter preamble (16 vs 80 bits) cut control-op latency: the 136 frame bits alone take ~113 ms over aux cable vs ~453 ms at 300 baud, and the complete on-air frame (preamble + `0x7E` sync + frame + CRC-8 + 16-bit postamble) is 184 bits = ~153 ms over `aux-cable` vs 248 bits = ~827 ms over `standard` (exact bit counts are pinned by the `afsk_bits` family of `Documentation/medium_vectors.json`).

**HydraModem's aux profile is a different medium — it does not interoperate with the Python `aux-cable` profile.** `hydramodem/src/hydra_profile.c` also ships an aux-cable profile, `hydra_profile_aux_cable()` (exposed as `frame_tx`/`frame_rx --profile aux`; before that flag it had no caller), but only the baud rate and preamble length coincide:

| | Python `aux-cable` (`python/modem/acoustic_frame.py`) | HydraModem `aux` (`hydra_profile_aux_cable`) |
|---|---|---|
| tones | 1000 / 1500 Hz (mark / space) | 1200 / 2400 Hz (tone 0 / tone 1, orthogonal) |
| baud · preamble | 1200 · 16 alternating bits | 1200 · 16 alternating symbols |
| sync | `0x7E` (8 bits) | `0x2DD4` (16 bits) |
| integrity / FEC | CRC-8 (default) or RS 2t=16 (`fec=1`) | CRC-16/CCITT-FALSE + K=7 r=1/2 conv (soft Viterbi) + interleaver |
| postamble | 16 alternating bits | none |

A frame sent by one cannot be decoded by the other, even over the same cable. They are therefore two separately named media in the DCF-Medium layer (`DCF_MEDIUM_SPEC.md`) — **`afsk:`** (`afsk:profile=aux-cable`) and **`hydra:`** (`hydra:profile=aux`) — each certified to its own stream in `Documentation/medium_vectors.json`: `afsk_bits` (the on-air bit string of `acoustic_frame.encode_bits`) and `hydra_symbols` (the tone-index string of `hydra_frame_build`). Both carry the 17-byte `DeModFrame` opaquely.

## Mapping rule (the certified law)

```
modulate(mod, data):
  bits   = MSB-first bits of every byte
  pad    bits with 0 to a multiple of bits_per_symbol
  symbol = Gray(value) for each bits_per_symbol-bit group  ->  symbol stream
demodulate(mod, symbols, nbytes):
  bits   = inverse-Gray each symbol, MSB-first within the group
  bytes  = pack 8 bits/byte, take the first nbytes
```

Law: `demodulate(modulate(x), len(x)) == x` for every modulation.
Symbol count: `ceil(8*len(x) / bits_per_symbol)`.
Anchors: Gray(0..15) = `[0,1,3,2,6,7,5,4,12,13,15,14,10,11,9,8]`;
`ungray∘gray = id` on 0..255.

## The medium

The modem transport reads/writes a sample stream from a **medium**:
- **loopback / file** (default, deterministic) — `dcfnode send-modem --medium PATH`
  writes a self-describing capture (`"DCFM" | mod | nbytes | nsamples | fec | f64[]`);
  `recv-modem --medium PATH` demodulates it. A frame pair crosses the "channel"
  byte-exact. Used by the interop test (per modulation).
- **live audio** (`DCF_MODEM_AUDIO`, default OFF) — a PortAudio/ALSA backend rendering
  `dcf_modem.dsp` over speaker↔mic, the open-air path of `python/modem/`.

The 14-byte medium header is `"DCFM" | mod u8 | nbytes u32 BE | nsamples u32 BE |
fec u8`, followed by `nsamples` native `f64` samples. The trailing byte is a **0/1 FEC
flag, not a parity count** (`C_SDK/node/dcfnode.c` `cmd_send_modem` / `cmd_recv_modem`,
`hdr[13]`): `fec=0` = the symbols carry the raw payload; `fec=1` = they carry a DCF-FEC
multi-codeword blob and `nbytes` is the blob length. The RS parity count (`--parity N`,
default 16) is not in the medium header — the blob's own self-protecting header
(`[len u32 | nparity u8]`, `DCF_FEC_SPEC.md`) carries it, so `recv-modem` needs no flag.

## Forward error correction (`--fec`)

A real channel corrupts symbols. `dcfnode send-modem --fec [--parity N]` wraps the
payload in the certified Reed-Solomon code (`DCF_FEC_SPEC.md`, default 2t=16 →
corrects 8 byte-errors) before modulation; `recv-modem` RS-decodes after demod and
reports `RS-FEC ok` (recovered) or `RS-FEC uncorrectable` (never silent garbage).
This turns the detect-only modem into a **correcting** link — what makes it usable
over lossy RF/acoustic media. Verified per modulation in
`C_SDK/tests/test_modem_fec.c` (corrupt symbols → RS recovers the frame). The
byte↔symbol map and RS code are certified; the waveform is loopback-tested.

## Certification

`Documentation/modulation_vectors.json` (+ identical `python/MCP/` copy) and
`codec/modulation_vectors.gen.h` are generated by `python/MCP/gen_modulation_vectors.py`:
16 cases (4 schemes × 4 inputs) pinning `bytes→symbols` and the round-trip + symbol
count laws. CI (`wire-certify.yml`) regenerates + diffs them and runs the Rust
(`codec/tests/certify_modulation.rs`) and C (`C_SDK/tests/test_modulation_certify.c`)
certs; `C_SDK/tests/test_modem_loopback.c` proves byte-exact recovery for all four
schemes over the ideal medium.

```sh
python3 python/MCP/gen_modulation_vectors.py /tmp/m.json   # regen + verify laws
cd codec && cargo test --test certify_modulation           # Rust
gcc -std=c11 -I codec C_SDK/tests/test_modulation_certify.c -o /tmp/mc && /tmp/mc  # C
gcc -std=c11 -I codec -I C_SDK/node C_SDK/tests/test_modem_loopback.c -lm -o /tmp/ml && /tmp/ml
```

### HydraModem symbol stream (`hydra_symbols`)

HydraModem (`hydramodem/`) is now **byte-certified to its symbol stream across
languages**. The `hydra_symbols` family of `Documentation/medium_vectors.json` (+ the
identical `python/MCP/` copy and `codec/medium_vectors.gen.h`, generated by
`python/MCP/gen_medium_vectors.py` from the Python port in `python/MCP/mediumlab_core.py`)
pins, for each basis frame × profile (`default` / `aux`) × FEC (`none` / `rep3` / `conv`)
× interleave (0 / 1), plus 4-FSK cases, the exact tone-index sequence
`[preamble][sync 0x2DD4][interleave(fec(frame ‖ CRC-16))]` (one hex digit per symbol)
together with `coded_bits`, `interleave_stride` and `total_syms`. The per-language
`hydra:` medium codecs certify against it (`DCF_MEDIUM_SPEC.md` lists which).

The ground truth is the upstream C: `hydramodem/dcf-tools/hydra_symbols_certify`
checks every case against the **real** `hydra_frame_build` (symbol-for-symbol), feeds
the vector symbols through the real `hydra_frame_decode_soft`, confirms the profile
tables equal `hydra_profile_default()` / `hydra_profile_aux_cable()`, and runs a
`hydra_modem_tx → hydra_modem_rx_ex` loopback per case. CI runs it in the
`certify-hydramodem` job. The **waveform** (tone synthesis, acquisition, timing
recovery, soft metrics) stays **loopback-tested, not certified** — the same line as
Opus/PM synthesis — so certification stops at the symbols.

```sh
cd hydramodem/dcf-tools && ./build.sh && build/hydra_symbols_certify   # vectors vs real C
build/frame_tx <34-hex> aux.wav --profile aux --none --interleave 0     # profile knobs:
build/frame_rx aux.wav --profile aux --none --interleave 0              #   --profile/--interleave/--preamble
```
