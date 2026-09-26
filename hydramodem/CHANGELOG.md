# Changelog

## 2.0.0 — musical profiles

### Added
- **Musical tone-table profiles** (`hydra_profile_music`, presets
  `hydra_profile_melody` / `_chime` / `_nocturne`; `frame_tx`/`frame_rx
  --profile melody|chime|nocturne`). M-FSK whose tones are a just-intonation
  scale on the harmonic series of the baud: pentatonic or triad, orthogonal by
  construction. Symbols are Gray-mapped onto scale degrees, the preamble becomes a
  tonic/octave tremolo, an optional drone sits on harmonics that are
  orthogonal to every data correlator, and a 10 ms raised-cosine attack/release
  is rendered outside the symbol body. Musical synthesis is exact: integer
  phase on the `k/L` grid and a quarter-wave sine table (no accumulated
  `f/fs` drift), so the WAV is a normative function of the table. See `docs/MUSIC.md`.
- **Bass voice and polyphony**: `hydra_profile_bass` (D2 B2 D3 A3, 75–225 Hz,
  `--profile bass`); `hydra_modem_tx_poly` / `hydra_modem_rx_poly` /
  `hydra_poly_check` put up to 4 frames in one burst, one voice each;
  `hydra_profile_duet` (melody + bass) is the acoustic SuperPack: 2 frames in
  7.18 s vs 10.04 s sequential. `dcf-tools/poly_tx` / `poly_rx`; Python
  `HydraDuet`.
- `tests/test_music.c` (in `make check`).

### Fixed
- **Streaming RX lost frames sent back to back** on the musical profiles:
  `hydra_rx_push`'s window (body + 50 ms + 4 symbols) is longer than a melody
  burst plus the 60 ms between bursts, so it held the next burst's start and then
  discarded the whole window. 1 of 4 melody frames came through at `frame_tx`'s
  gap. Now only the samples through the decoded frame (its release included) are
  consumed and the rest is replayed through the segmenter: 4 of 4
  (`tests/test_music.c` [6], which fails at 1 of 4 on the old code). The default
  profile was not affected (8 of 8 before and after).
- **Acquisition's plateau is the first run** of best-scoring origins (within one
  symbol of the first), so a window holding two bursts' prefixes centres on the
  first, not between them. For a single burst the plateau is never a symbol wide,
  so one-shot verdicts are unchanged: the 96-input impaired comparison gives the
  same verdicts before and after.

### Changed (ABI)
- `hydra_profile` gains `tone_mult[16]`, `drone_mult[2]`, `drone_gain` and
  `ramp_ms`, appended after the derived fields. The struct grew, so the soname
  is now `libhydramodem.so.2`. `hydra_profile_default` / `_aux_cable` now zero
  the whole struct first. Linear profiles render exactly the 1.0.0 waveform.
- The library is built with `-ffp-contract=off` (no FMA contraction).
- The Faust RX backend refuses a musical tone table (its tone bank is compiled
  in); use the C reference RX.

## 1.0.0 — production

First production release. The proof-of-concept transported a 17-byte frame over
a clean loopback; this release makes the link work between independent devices,
over noise, and as a streaming receiver, with a full test suite.

### Added
- **Soft-decision convolutional FEC** (`hydra_conv`): rate-1/2, constraint
  length K=7, soft-input Viterbi. Now the default mode; ≈6 dB coding gain over
  uncoded at the cliff (100 % frame success to −6 dB broadband SNR).
- **Block interleaver** (`hydra_interleave`): coprime-stride permutation to
  spread burst fades ahead of the convolutional decoder.
- **Symbol-timing recovery**: decision-directed loop on a total-energy
  discriminator, gated to transition symbols. Tolerates ≥ ±3000 ppm TX/RX clock
  offset (open-loop decoding failed beyond a few hundred ppm). `hydra_rx_diag`
  reports the estimated `clock_ppm`.
- **Robust acquisition**: full known-prefix (40-symbol) correlation over the
  whole buffer, plateau-centred and energy-peak-refined. Fixes the AWGN collapse
  caused by sync-only search triggering on leading noise.
- **Quadrature integrate-and-dump receiver**: per-tone I/Q matched filter via
  prefix sums (O(1) symbol energy), replacing the prior energy-only detector.
- **Streaming API**: `hydra_rx_create` / `hydra_rx_push` / `hydra_rx_reset` /
  `hydra_rx_destroy` with an energy segmenter and bounded per-burst memory.
- **Test suite**: `test_unit`, `test_fuzz`, `test_stream` (plus the existing
  `test_loopback`); `make check` runs all, `make asan` runs them under
  AddressSanitizer + UBSan (clean).
- **Packaging**: umbrella header `<hydramodem/hydramodem.h>` with version
  macros, static + versioned shared library, `pkg-config` file, and
  `make install` (PREFIX / DESTDIR aware).
- **M-FSK validated end to end** (2/4/8/16-ary), including the zero-pad path when
  bits/symbol does not divide the 16-bit sync or the coded payload. Fixes an
  out-of-bounds read in acquisition and a sync-tiling constraint that previously
  rejected 8-FSK while accepting 16-FSK.
- **Orthogonality guard**: `hydra_profile_init` now rejects tones that are not
  integer multiples of the baud (the integrate-and-dump detector requires it),
  rather than mis-decoding silently.
- **Channel regression test** (`tests/test_channel.c`): proves M-FSK decodes,
  the interleaver rides out long bursts (40-symbol burst: conv-only ~0 % vs
  conv+interleave ~100 %), and characterizes reverb/ISI with the baud tradeoff
  (RT60 = 50 ms: 1000 baud ~3 % vs 125 baud ~75 %).
- **Random-profile fuzzing**: `test_fuzz` now roundtrips hundreds of random valid
  profiles across every modulation order, baud, sync word and FEC combination.
- **Docs**: `docs/RECEIVER.md` documents the acquisition and timing-recovery
  design, including rejected approaches; the README gains a channel
  characteristics / tuning section.

### Changed
- FEC selection is now `hydra_profile.fec_mode` (`HYDRA_FEC_NONE` / `_REP3` /
  `_CONV`) plus an `interleave` flag, replacing the boolean `use_fec`.
- `hydra_frame` exposes `hydra_frame_decode_soft` (soft metrics in) in place of
  the old hard-decision payload decoder; frame layout now interleaves the coded
  payload+CRC region.
- `faust/hydramodem_rx.dsp` and `demod_modem.lib` emit per-tone **I/Q** (2N
  outputs) via `tonebank_iq`; the RX adapter and reference DSP updated to match.
- Examples take `--none` / `--rep3` / `--conv` instead of `--fec`.

### Notes
- The library version is single-sourced from `HYDRAMODEM_VERSION` in
  `hydramodem.h`; the Makefile derives the soname and pkg-config version from it.
- Repetition-3 FEC (`hydra_fec`) is retained as the `HYDRA_FEC_REP3` mode.
- The default 1000-baud profile is a near-field / low-reverb link; lower the baud
  for reverberant rooms (see the README channel section).
- The Faust and portable-C DSP backends remain decode-identical.
- All code is scalar `double`/`float`: no SIMD or RISC-V vector dependency
  (targets SiFive U74 / RV64GC).
