# HydraModem

An acoustic **M-FSK physical layer** for transporting **Punctim / DCF** frames
(17 opaque bytes) over sound — 44.1 / 48 kHz audio, real-time on a single
SiFive U74 core of a StarFive JH7110.

The DSP front end is **Faust**; the framing / FEC / synchronization layer is
**C**. That split is the entire design philosophy (see below) and follows
directly from what Faust can and cannot express.

```
            ┌──────────────────────────────────────────────────────────┐
            │  Punctim  (mesh / routing layer — your existing stack)   │
            └───────────────┬───────────────────────────┬──────────────┘
                            │  17-byte DCF frame (opaque)│
            ┌───────────────▼───────────────────────────▼──────────────┐
   C layer  │ framing · CRC-16 · conv+interleave FEC · acquisition ·     │
   (src/)   │ symbol-timing recovery · soft-Viterbi · streaming RX       │
            └───────────────┬───────────────────────────▲──────────────┘
                 freq/sample │                           │ per-tone I/Q
            ┌───────────────▼───────────────────────────┴──────────────┐
  Faust DSP │  CPFSK modulator   ·   quadrature down-conversion bank     │
  (faust/)  │  demod_modem.lib → hydramodem_tx.dsp / hydramodem_rx.dsp   │
            └───────────────┬───────────────────────────▲──────────────┘
                            ▼  acoustic samples          │
                         speaker / DAC  ~~~~air~~~~  mic / ADC
```

## The rule

> **Faust owns continuous, per-sample DSP. C owns anything variable-length,
> data-dependent, or packet-shaped.**

Faust is a synchronous stream language: one output frame per input frame, a
single global sample rate, strict evaluation, statically-bounded memory. That is
ideal for the modulator phase accumulator and the quadrature mixer bank, and
unable to express framing, CRC, FEC, variable-rate symbol emission, acquisition,
or timing recovery. So the modem carries **data as audio streams** across the
Faust boundary (TX: an instantaneous-frequency signal in; RX: per-tone I/Q out)
and does every byte-/packet-level operation in C.

## Physical layer

Continuous-phase **M-FSK** (binary by default), detected **non-coherently** via
per-tone **quadrature integrate-and-dump** — the matched filter for a
rectangular-envelope tone, and the reason the RX is a clean fit for Faust (no
carrier recovery).

Default profile (`hydra_profile_default`, matches `faust/hydramodem_rx.dsp`):

| parameter     | value                                                          |
|---------------|----------------------------------------------------------------|
| sample rate   | 48 kHz                                                         |
| baud          | 1000 sym/s (48 samples/symbol)                                 |
| tones         | 2 (binary), 2000 / 3000 Hz                                     |
| orthogonality | tones are integer multiples of the baud → exactly orthogonal over one symbol |
| preamble      | 24 symbols, alternating tone 0 / tone N−1                      |
| sync word     | 0x2DD4 (16 bits)                                               |
| CRC           | CRC-16/CCITT-FALSE over the 17 payload bytes                   |
| FEC           | **conv** (K=7, r=1/2, soft Viterbi) + block interleaver — *default*; or rep3; or none |

Frame on the wire (symbols): `[preamble][sync][ interleave( conv( payload + CRC16 ) ) ]`.
The 17-byte DCF payload is transported **opaquely** — the modem never parses it.

**M-FSK (4 / 8 / 16-ary) is validated**, not just nominally supported — see the
channel section and `tests/test_channel.c`. Modulation order, baud, tones,
preamble length and sync word are all profile fields; set `n_tones` to a power
of two (and keep the same value in the `.dsp` `N` constant). One hard
requirement the profile **enforces**: `base_freq` and `tone_spacing` must be
integer multiples of `baud`, so every tone is an exact integer number of cycles
per symbol — that orthogonality is what makes the non-coherent integrate-and-dump
detector exact and cancels the 2·f image. Non-orthogonal configs are rejected by
`hydra_profile_init` rather than left to mis-decode silently.

## Receiver chain

The hard part of a real acoustic link is that the two devices do **not** share a
sample clock and the signal arrives buried in noise. The receiver is built for
both (see `docs/RECEIVER.md` for the full rationale):

1. **Quadrature down-conversion** (Faust/ref DSP) → per-tone I/Q streams.
2. **Prefix-sum integrate-and-dump** — a symbol's energy is `(ΣI)²+(ΣQ)²`,
   evaluated in O(1) from prefix sums so a full-buffer search is cheap.
3. **Acquisition** — match the entire known prefix (24 preamble + 16 sync = 40
   known symbols) over the whole buffer. 40 known symbols make false alarm
   negligible, so it locks even when leading noise has signal-level energy. The
   match score is a plateau, so the **centre** of the best run is taken and then
   refined to the energy peak — otherwise sampling is biased half a symbol off.
4. **Symbol-timing recovery** — a decision-directed loop tracks the TX/RX clock
   offset using the **total-energy** timing discriminator (peaked at symbol
   alignment, independent of which tone wins), **gated to transition symbols**
   where that discriminator actually carries information. Tolerates **≥ ±3000
   ppm** (real audio crystals are ±100 ppm).
5. **Soft-decision decode** — per-bit max-log soft metrics → deinterleave →
   soft Viterbi → CRC.

## Channel characteristics & tuning

The end-to-end behaviour on the impairments that matter for an acoustic link
(`tests/test_channel.c` asserts all of this):

- **Modulation order.** 2/4/8/16-FSK all decode at 100 % clean and ≥ 95 % at
  0 dB AWGN (8-FSK ≥ 80 %). Higher orders carry more bits/symbol (shorter
  frames) at the cost of bandwidth and per-tone noise margin; binary and 4-FSK
  are the usual sweet spots.
- **Burst errors.** The interleaver is not cosmetic. Under a long error burst —
  a moving reflector, a passing occlusion — a convolutional code alone collapses
  (a 40-symbol burst drops conv-only to ~0 %), while **conv+interleave rides it
  out at ~100 %** by scattering the burst into isolated, correctable errors.
  Keep `interleave = 1` (the default) on any real channel.
- **Reverberation / multipath.** This is the one to design around. At the
  default 1000 baud (1 ms symbols), inter-symbol interference dominates once the
  reverb tail exceeds a few ms, so the default profile is a **near-field /
  low-reverb / cabled** link. For a live room, **lower the baud** so the delay
  spread is a fraction of a symbol: at RT60 = 50 ms, dropping 1000 → 125 baud
  takes decode from ~3 % to ~75 %; 125 baud holds ≥ 80 % at RT60 = 20 ms. The
  tradeoff is throughput, and the tones must stay integer-cycle at the new baud:

  ```c
  hydra_profile p; hydra_profile_default(&p);
  p.baud = 125; p.base_freq = 1000; p.tone_spacing = 500;  /* reverberant room */
  hydra_profile_init(&p);
  ```

- **Clock offset / Doppler.** Timing recovery tolerates ≥ ±3000 ppm; acoustic
  Doppler at walking speed (~2900 ppm) and any real crystal (±100 ppm) are well
  inside that. A sample-clock offset is equivalent to the pitch shift a
  mismatched playback rate produces, so both are covered by the same loop.

## API

```c
#include <hydramodem/hydramodem.h>

hydra_profile p; hydra_profile_default(&p); hydra_profile_init(&p);

/* transmit: 17 bytes -> mono float PCM at p.sample_rate */
float *audio; size_t n;
hydra_modem_tx(&p, payload17, &audio, &n);

/* receive, one-shot over a captured buffer */
uint8_t out[17]; hydra_rx_diag d;
if (hydra_modem_rx_ex(&p, audio, n, out, &d) == HYDRA_OK) { /* d.clock_ppm, ... */ }

/* receive, streaming from a live audio callback (bounded memory) */
hydra_rx *rx = hydra_rx_create(&p, on_frame, user);
hydra_rx_push(rx, block, block_len);     /* call per audio block; fires on_frame */
hydra_rx_destroy(rx);
```

All buffers are caller-managed; the library allocates only transient working
memory bounded by one frame. Everything is scalar `double`/`float` — no SIMD or
RISC-V vector-extension dependency.

## Two DSP backends, one interface

`src/hydra_dsp.h` is the per-sample boundary. Two implementations satisfy it:

- **`hydra_dsp_ref.c`** — portable C, mirrors `demod_modem.lib` 1:1. Zero
  external dependencies, the default `make`. Develop and validate against it.
- **`hydra_dsp_faust_{tx,rx}.c`** — thin adapters over the `faust -lang c -os`
  output: the deployment path, runs the actual compiled Faust DSP. `make faust`.

Both produce identical loopback results, so you can develop against the
reference and ship the Faust build.

## Quick start

```bash
make                       # static + shared lib, examples, test binaries
make check                 # run the full suite: unit + fuzz + stream + loopback
make asan                  # rebuild the suite under AddressSanitizer + UBSan

./build/tx_demo "hello hydra" msg.wav         # 17-byte payload -> WAV (conv FEC)
./build/rx_demo msg.wav                        # WAV -> payload (CRC-checked)
./build/tx_demo "no coding" raw.wav --none     # FEC off
./build/rx_demo raw.wav --none

make faust-check                               # run the real compiled Faust DSP
sudo make install                              # lib + headers + pkg-config
```

Pipe through the air or a cable with anything that plays/records a WAV;
resample or transcode with FFmpeg as needed (the modem is just audio).

## Validation

`make check` runs four programs; all must pass (and `make asan` runs them clean
under AddressSanitizer + UBSan — no leaks, no UB):

- **test_unit** — CRC known-answer (0x29B1), convolutional error correction,
  interleaver bijection, bit/byte/symbol packing inverses, frame build↔decode
  for every FEC mode, WAV I/O.
- **test_loopback** — clean roundtrip, AWGN sweep across FEC modes, clock-offset
  table.
- **test_fuzz** — one-shot and streaming RX on random/degenerate buffers (never
  crash), random-payload roundtrips, and random *profile* roundtrips (every
  M-FSK order, baud, sync, FEC combination).
- **test_stream** — multi-frame chunked stream + reset, per-frame payload check.
- **test_channel** — M-FSK orders, burst-error resistance (interleaver), and
  reverb/ISI behaviour with the baud tradeoff.

Representative results (reference backend; SNR is broadband / full-Nyquist, so
effective Eb/N0 in the ~baud-wide detector is much higher — these are
conservative):

```
clean loopback : OK   sync 16/16   payload recovered exactly
clock offset   : decodes ±3000 ppm (timing recovery); est_ppm reported in diag
AWGN, uncoded  : ~100% down to ~0 dB SNR, cliff below
AWGN, conv     : ~100% down to −6 dB SNR  (≈6 dB soft-Viterbi coding gain)
```

## Files

```
faust/
  demod_modem.lib       phase01, cpfsk, tone_iq, tonebank_iq, agc, tanhpade
  hydramodem_tx.dsp     TX: freq-signal in  -> acoustic sample out      (1->1)
  hydramodem_rx.dsp     RX: acoustic in     -> 2N tone I/Q out          (1->2N)
  hydramodem_test.dsp   pure-Faust offline self-test
src/
  hydramodem.h          umbrella public header + version macros
  hydra_profile.[ch]    profile + derived params (FEC sizing) + validation
  hydra_crc.[ch]        CRC-16/CCITT-FALSE
  hydra_conv.[ch]       K=7 r=1/2 convolutional code + soft Viterbi
  hydra_interleave.[ch] coprime-stride block interleaver
  hydra_fec.[ch]        repetition-3 FEC (legacy mode)
  hydra_frame.[ch]      bit/byte/symbol packing + frame assembly/teardown
  hydra_dsp.h           the per-sample DSP interface (one boundary)
  hydra_dsp_ref.c       portable C reference DSP (default backend)
  hydra_dsp_faust_*.c   Faust-generated DSP adapters (deploy backend)
  hydra_modem.[ch]      end-to-end TX/RX: one-shot + streaming, timing, sync
  wav.[ch]              minimal mono 16-bit WAV I/O for demos
examples/  tx_demo.c, rx_demo.c
tests/     test_unit.c, test_loopback.c, test_fuzz.c, test_stream.c, test_channel.c
docs/      RECEIVER.md  (acquisition + timing-recovery design notes)
```

See **BUILD.md** for the Faust compile commands, JH7110 cross-compilation, and
the profile-matching requirement; **docs/RECEIVER.md** for the receiver design.

## License

LGPL-3.0-only (consistent with the Punctim / DCF tree; DeMoD LLC, the sole
copyright holder, relicensed HydraModem from Apache-2.0 on integration). See
LICENSE and NOTICE. The Faust standard libraries used at build time are under
their own (permissive) licenses.
