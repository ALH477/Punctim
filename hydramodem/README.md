# HydraModem

An acoustic **M-FSK physical layer** for transporting **Punctim / DCF** frames
(17 opaque bytes) over sound — 48 kHz audio (every shipped profile, test and
measurement is at 48 kHz; see [Sample rates](#sample-rates)), real-time on a
single SiFive U74 core of a StarFive JH7110.

The DSP front end is **Faust** (or its C reference port); the framing / FEC /
synchronization layer is **C**. (Exception: the musical profiles' transmitter
synthesizes in C and uses neither DSP backend — `docs/MUSIC.md`.) That split is the entire design philosophy (see below) and follows
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

**M-FSK (4 / 8 / 16-ary) is loopback-tested in simulation**, not just nominally
supported — see the channel section and `tests/test_channel.c` (50 random frames
per point; 16-FSK is exercised clean only). No order other than 2-FSK has been
certified or run over a real link. Modulation order, baud, tones,
preamble length and sync word are all profile fields; set `n_tones` to a power
of two (and keep the same value in the `.dsp` `N` constant). One hard
requirement the profile **enforces**: `base_freq` and `tone_spacing` must be
integer multiples of `baud` (`hydra_profile_init`, `src/hydra_profile.c`), so
that — *when `sample_rate / baud` is an integer* — every tone is an exact integer
number of cycles per symbol. That orthogonality is what makes the non-coherent
integrate-and-dump detector exact and cancels the 2·f image. Configs that break
the multiple-of-baud rule are rejected rather than left to mis-decode silently.
The integer `sample_rate / baud` half is **not** checked for linear profiles
(only the musical path checks it): `samples_per_symbol` is rounded, so e.g.
44.1 kHz at 1000 baud is accepted with 44 samples/symbol and 1.9955 cycles of
the 2000 Hz tone per symbol — not orthogonal. Keep `sample_rate / baud` integral
yourself (see [Sample rates](#sample-rates)).

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
   where that discriminator actually carries information. Decodes **±3000
   ppm** on the default profile (evidence: [below](#what-is-established-and-at-which-level);
   real audio crystals are ±100 ppm).
5. **Soft-decision decode** — per-bit max-log soft metrics → deinterleave →
   soft Viterbi → CRC.

## Channel characteristics & tuning

The end-to-end behaviour on the impairments that matter for an acoustic link
(`tests/test_channel.c` asserts all of this):

- **Modulation order.** 2/4/8/16-FSK all decode at 100 % clean; 2- and 4-FSK
  are asserted ≥ 95 % and 8-FSK ≥ 80 % at 0 dB AWGN (16-FSK is not run under
  noise). Higher orders carry more bits/symbol (shorter
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
  takes decode from ~3 % to ~75 %; 125 baud holds ≥ 80 % at RT60 = 20 ms. These
  figures are from a **synthetic** reverb (an exponentially decaying random-tap
  impulse response, `tests/test_channel.c`, 60 frames a point), not a measured
  room. The tradeoff is throughput, and the tones must stay integer-cycle at the new baud:

  ```c
  hydra_profile p; hydra_profile_default(&p);
  p.baud = 125; p.base_freq = 1000; p.tone_spacing = 500;  /* reverberant room */
  hydra_profile_init(&p);
  ```

- **Clock offset / Doppler.** Timing recovery decodes ±3000 ppm; acoustic
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
- **test_loopback** — clean roundtrip, AWGN sweep across FEC modes (+24 to
  −3 dB), clock-offset table (±2000 ppm). Only the clean roundtrip is asserted;
  the sweep and the table are printed, not checked.
- **test_fuzz** — one-shot and streaming RX on random/degenerate buffers (never
  crash), random-payload roundtrips, and random *profile* roundtrips (every
  M-FSK order, baud, sync, FEC combination).
- **test_stream** — multi-frame chunked stream + reset, per-frame payload check.
- **test_channel** — M-FSK orders, burst-error resistance (interleaver), and
  reverb/ISI behaviour with the baud tradeoff.

Representative results (reference backend, default profile; SNR is broadband /
full-Nyquist, so effective Eb/N0 in the ~baud-wide detector is much higher —
these are conservative):

```
clean loopback : OK   sync 16/16   payload recovered exactly
clock offset   : decodes ±2000 ppm in test_loopback; ±3000 ppm in the vendored
                 impaired set below
AWGN, uncoded  : ~100% down to ~0 dB SNR, cliff below
AWGN, conv     : 100% down to 0 dB, 149/150 at −3 dB in test_loopback;
                 −6 dB decodes in the vendored impaired set below
```

The ±3000 ppm and −6 dB rows are not asserted by `make check`. Their evidence is
an external, vendored set: 70 impaired WAVs of the default profile (AWGN +12 to
−12 dB, clock ±500 to ±3000 ppm, frequency ±50 to ±300 Hz) with `frame_rx`'s
verdict on each, recorded against this tree at `fce2813` in the Exsecutor
repository (`vendor/hydramodem-rx/PROVENANCE.md`, `verdicta.tsv`,
`docs/design/receptor.md` §13): the reference decodes all 24 clock vectors
(±3000 ppm included), all six −6 dB AWGN vectors, and every frequency offset up
to ±250 Hz; it refuses −12 dB and ±300 Hz.

## Sample rates

Every shipped profile (`default`, `aux`, the musical ones), every test in
`make check`, every certificate and every hardware run is at **48 kHz**. Nothing
is tested, certified or field-validated at 44.1 kHz. The library does not refuse
44.1 kHz: a profile whose `sample_rate / baud` is an integer (e.g. 44.1 kHz at
900 or 1050 baud, with tones on multiples of that baud) is orthogonal and should
work, but is untested. 44.1 kHz at the default 1000 baud is *accepted* by
`hydra_profile_init` and is **not** orthogonal (44 samples/symbol, non-integer
tone cycles; see [Physical layer](#physical-layer)). A 44.1 kHz recording of a
48 kHz frame is not rescaled either: `hydra_wav_read` returns the file's rate and
`frame_rx` does not compare it with the profile's. Resample to 48 kHz first.

## What is established, and at which level

Four different claims, kept apart. "Certified" here means only a byte-for-byte
comparison against committed vectors or reference output; a loopback test in
simulation, a structural argument, and a hardware run are each named as what
they are.

| profile | capability (code + simulated loopback) | certified in Punctim | Exsecutor port (separate repository) | field-validated |
|---|---|---|---|---|
| `default` — 2-FSK, 48 kHz, 1000 baud, 2000/3000 Hz, conv + interleave | `make check` | **symbol stream** only: `hydra_symbols` in `Documentation/medium_vectors.json` (36 default-profile cases over conv/rep3/none × interleave on/off), proved against `hydra_frame_build` by `dcf-tools/hydra_symbols_certify`. The waveform is loopback-tested, not vectored | TX: **byte-identical** WAVs for 3 frames and the symbol stream for a 137-word basis; the extension to all 2¹³⁶ inputs is **argued from structure** (the stream is affine over GF(2)), not tested. RX: decodes the 3 WAVs, 140 round trips, and all 62 of 70 vendored impaired WAVs that `frame_rx` decodes, never writing a wrong frame (`examples/hydramodem/`, `docs/design/{modem,receptor}.md`) | **T2 cabled, two USB interfaces, 48 kHz**: 200/200 frames each way (reference DSP), 200/200 A→B (Faust DSP); full duplex 300/300 and ~287/300 (`docs/WHITEPAPER.md` §4, `docs/FAUST_MODERNIZATION.md`). Reported with setup and commands; the raw logs are not committed. Not tested over air |
| `aux` — 2-FSK, 1200 baud, 1200/2400 Hz | `hydra_profile_aux_cable` | symbol stream: 36 `hydra_symbols` cases | reference renders vendored; no Exsecutor program | none recorded |
| 4/8/16-FSK on the default tones | `tests/test_channel.c` (16-FSK clean only) | symbol stream: two 4-FSK `hydra_symbols` cases; 8- and 16-FSK none | 4- and 8-FSK reference renders vendored; no Exsecutor program | none |
| 125 baud (reverberant rooms) | `tests/test_channel.c`, synthetic reverb | none | reference render vendored; no Exsecutor program | none |
| musical `melody`, `bass`, `duet` | `tests/test_music.c`; the five `punctim` CLIs agree byte-for-byte (`tests/io_matrix.py`, `hydra-melody` leg) | none — loopback-tested, not byte-certified (`docs/MUSIC.md`) | TX **byte-identical** to `frame_tx --profile melody\|bass` and `poly_tx` (WAVs plus a 137-word symbol basis for melody and bass); receiver matches the reference's verdicts on the renders and on a few impaired copies (`docs/design/melos.md`) | none |
| musical `chime`, `nocturne` | `tests/test_music.c` | none | none | none |
| anything at 44.1 kHz | untested (see [Sample rates](#sample-rates)) | none | refused by design | none |

The Exsecutor certificates compare against this tree's own output at pinned
commits (`fce2813` for the default profile; `5c6a4e1`, `3aeff9d` and `f86f5d5`
for the melody, bass/duet and musical-receiver sets — each tree's
`PROVENANCE.md`). They certify that port against this reference, not this
reference against anything else.

## Security: the CRC is not authentication

HydraModem's CRC-16 detects accidental corruption; it is not a MAC, not a
signature, and anyone can compute it. Nothing in HydraModem — or in the
`DeModFrame` it carries — provides confidentiality, authentication or replay
protection: any receiver in earshot (or on the cable) can decode every frame,
and anyone with a speaker can transmit a well-formed one. That is DCF's
deliberate encryption-free posture, and on an acoustic or RF link there is no
WireGuard beneath the socket to fall back on. Read
[`Documentation/DCF_SECURITY_EXPOSURE.md`](../Documentation/DCF_SECURITY_EXPOSURE.md)
before deploying.

## Names

| name | what it is | role |
|---|---|---|
| **DeMoD** (DeMoD LLC) | the company that holds the copyright | — |
| **DCF** — DeMoD Communication Framework | the protocol | — |
| **Punctim** | the monorepo that implements DCF, and its `punctim` CLI | the protocol's implementations |
| **HydraMesh** | the earlier name of the Punctim repository (Exsecutor's documents still cite `ALH477/HydraMesh`) | — |
| **`DeModFrame`** | the 17-byte frame, pinned by the 246-vector certificate | the wire *quantum* |
| DCF-Audio, -Game, -Text, … | payload formats serialised onto `DeModFrame`s | *adapters* over the quantum |
| **HydraModem** | this library: framing, CRC, FEC, interleave, sync, timing recovery, M-FSK | a *medium* / PHY beneath the quantum; never parses the frame |
| **DSP backend** | `hydra_dsp_ref.c` (C reference) or the compiled Faust `.dsp` | per-sample modulation and down-conversion inside HydraModem |
| **Exsecutor** | a separate freestanding language whose `examples/hydramodem/` re-implements HydraModem's TX and RX | an independent implementation checked against this one |

Nothing in this stack is "secure" in the cryptographic sense; see above.

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
tests/     test_unit.c, test_loopback.c, test_fuzz.c, test_stream.c, test_channel.c,
           test_music.c
docs/      RECEIVER.md  (acquisition + timing-recovery design notes)
           MUSIC.md     (musical profiles: just-intonation M-FSK, drone, envelope)
```

See **BUILD.md** for the Faust compile commands, JH7110 cross-compilation, and
the profile-matching requirement; **docs/RECEIVER.md** for the receiver design;
**docs/MUSIC.md** for the musical profiles (`--profile melody|chime|nocturne|bass`)
and the two-frame `duet` (`dcf-tools/poly_tx`/`poly_rx`).

## License

LGPL-3.0-only (consistent with the Punctim / DCF tree; DeMoD LLC, the sole
copyright holder, relicensed HydraModem from Apache-2.0 on integration). See
LICENSE and NOTICE. The Faust standard libraries used at build time are under
their own (permissive) licenses.
