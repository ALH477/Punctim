# HydraModem receiver design

This note explains *why* the receiver is built the way it is. The two problems
that dominate a real acoustic link — and that a clean-loopback prototype hides —
are (1) the transmitter and receiver run on **independent sample clocks**, and
(2) the signal arrives **buried in noise**, often with louder junk before it.
Everything below follows from handling those honestly.

The receive chain is:

```
samples → quadrature down-conversion → integrate-and-dump → acquisition
        → symbol-timing recovery → soft metrics → deinterleave → Viterbi → CRC
```

## Quadrature down-conversion + integrate-and-dump

For each tone `k` the DSP front end produces baseband in-phase/quadrature
streams `I_k, Q_k` (multiply by `cos`/`sin` at the tone frequency). The matched
filter for a rectangular-envelope tone of length `L` samples is a plain sum over
the symbol; the non-coherent decision statistic is the energy

```
E_k(a) = (Σ_{n=a}^{a+L} I_k[n])² + (Σ Q_k[n])²
```

Because the tones are integer multiples of the baud, the `2·f_c` image term
integrates to zero over a symbol, so the sum is the optimal non-coherent
detector. Carrier phase only rotates between I and Q, so taking the magnitude
makes the detector phase-blind — no carrier recovery, which is exactly what lets
the front end live in Faust.

**Prefix sums.** We precompute `PI_k[n] = Σ_{j<n} I_k[j]` (and `PQ_k`) once per
buffer, so any `E_k(a)` is two subtractions — O(1). That turns an otherwise
expensive full-buffer acquisition search into a cheap loop, and it is what makes
the timing search affordable on a JH7110 core.

## Acquisition: match the whole known prefix, then centre, then peak

A 16-bit sync word alone is too weak: at ~11k candidate origins, a 14/16 chance
match happens by accident dozens of times, and an onset detector keyed to energy
fires on leading noise and never reaches the true frame. Both failure modes
showed up in testing as a total AWGN collapse.

The fix is to correlate the **entire known prefix** — 24 preamble symbols plus
the 16 sync symbols = **40 known symbols** — at every origin. The probability of
40 symbols matching by chance is negligible, so acquisition locks onto the real
frame even when the buffer opens with signal-level noise.

Two refinements matter:

- **Centre the plateau.** The match score is flat across every origin within
  ±L/2 of the truth (they all detect the preamble correctly). Taking the first
  such origin biases the sampling phase half a symbol early; **every** data
  symbol is then sampled off-centre. We take the centre of the best-scoring run.
- **Refine to the energy peak.** Centring still leaves up to a sample of static
  phase error. The *energy* of the known prefix (unlike the match *count*) has a
  sharp peak, so a ±L/2 search for maximum known-tone energy pins the phase. The
  timing loop tracks drift, not a static offset, so starting aligned is
  essential.

## Symbol-timing recovery: the part that is genuinely hard

With independent clocks the symbol period at the receiver is not exactly `L`;
over a 356-symbol frame even 500 ppm drifts the sampling grid several samples and
open-loop decoding fails. So the grid must be tracked.

The non-obvious difficulty: **integrate-and-dump gives a flat energy envelope
within a symbol.** A classic early-late gate needs a pulse with a peak to lock
to; on a rectangular FSK symbol it has no stable lock point, and a single-tone
energy gradient *flips sign* when the window drifts onto a neighbouring symbol
and detection picks the wrong tone. Several plausible detectors were tried and
rejected (see the dead-ends list below).

What works is a **total-energy discriminator**, gated to transitions:

- **Total energy across all tones** `Σ_k E_k(a)` for a window straddling two
  *different* symbols is `A²·(d² + (L−d)²)`, which is **maximized at symbol
  alignment** (`d = 0`) and is symmetric and smooth. Crucially it does **not**
  depend on which tone wins, so it never flips sign — the bug that made earlier
  single-tone schemes track positive clock offsets but diverge on negative ones.
- **Gate on transitions.** During a *same-tone* run the signal is a continuous
  tone, so `Σ_k E_k` is flat across all offsets and the position-argmax rails to
  the search-range edge — injecting a constant bias that walks the grid away.
  Only a symbol where the profile is actually peaked (`max − min > 15 %`) carries
  timing information, so the loop updates only there and coasts otherwise.
- **Smoothed grid.** The per-symbol best offset feeds an EMA (`drift`) that
  drives the running position. Noise averages to ≈0 — the grid does not random-
  walk under AWGN, which preserves the soft-Viterbi coding gain — while a real
  clock offset biases it consistently and is tracked. On the default profile it
  decodes **±3000 ppm** (the vendored impaired set cited in `README.md`;
  `tests/test_loopback.c` itself runs only ±2000 ppm and prints rather than asserts the result);
  real audio crystals are within ±100 ppm.

The reported `clock_ppm` is derived from the mean grid advance over the data
field. Treat it as coarse: `tests/test_loopback.c`'s table prints, for applied
offsets of +500, +2000 and −2000 ppm, estimates of −1053, −1860 and +1633 (the
opposite sign, by that test's resampler convention, and not proportional). It is
diagnostic output, not a calibrated measurement.

### Dead ends (recorded so they are not re-tried)

- *Early-late gate* on symbol-half energies — flat envelope, no lock point.
- *Single-tone energy gradient* `(E(a+1) − E(a−1))/E(a)` — sign flips when the
  window drifts onto a neighbour tone; tracks one clock-offset polarity only.
- *Joint (origin, rate) search maximizing raw prefix energy* — degenerate: with
  origin free, a 40-symbol span cannot resolve rate, and the search rails to the
  range edge.
- *Per-symbol least-squares peak fit* — parabolic interpolation on a flat-topped
  energy function is ill-conditioned and noise-sensitive; wrecked AWGN.
- *Confidence gate `E₀ > 2·E₁`* — backwards for timing: it admits only same-tone
  neighbours (no timing information) and rejects the transitions that carry it,
  so the loop never corrects and decode succeeds only by luck.

## Soft-decision decode

Per coded bit we form a max-log soft metric `(max₁ − max₀)/(max₁ + max₀)` from
the tone energies (sign = hard decision, magnitude = confidence), deinterleave
(scatter the soft values back through the coprime-stride permutation), run the
soft-input Viterbi decoder for the K=7 code, reassemble the bytes and check the
CRC-16. The convolutional code buys roughly **6 dB** over uncoded at the cliff;
the interleaver additionally spreads burst fades (equal to plain conv under pure
AWGN, better under bursts).

## Channel reach: AWGN, bursts, and reverberation

The receiver is optimal for AWGN (matched filter + soft Viterbi) and, with the
interleaver, robust to bursts. Reverberation is the genuine limit: the
integrate-and-dump window assumes one tone occupies one symbol, but a room's
delay spread bleeds each symbol's energy into the next (inter-symbol
interference). At the default 1 ms symbol that fails once the reverb tail passes
a few ms, so the default profile is a near-field / low-reverb / cabled link. The
standard fix is to lengthen the symbol — drop the baud so the delay spread is a
small fraction of a symbol — which `tests/test_channel.c` confirms on a
synthetic exponential random-tap reverb, not a measured room (at RT60 = 50 ms,
1000 baud decodes ~3 %, 125 baud ~75 %). A cyclic-prefix / guard
interval per symbol would buy more, at a throughput cost; it is the natural next
step if heavy-reverb operation becomes a requirement.

## Streaming

`hydra_rx_push` runs an energy segmenter: an EMA noise floor sets adaptive
on/off thresholds; crossing the on-threshold starts collecting a burst into a
bounded buffer (one frame plus margin). A burst is decoded when either a full
frame-plus-margin is collected (back-to-back frames) **or** a sustained quiet
period follows at least a whole frame body (a lone frame trailed only by its own
guard) — the latter case is why the quiet branch decodes rather than only
abandoning. Shorter bursts are dropped as false triggers. Memory is bounded
regardless of stream length.

## False frames

A false frame is a CRC-valid 17-byte payload that the receiver outputs when no
intact frame of its profile was sent, or one whose bytes differ from every frame
that was sent. For a mesh a manufactured frame is worse than a lost one, so the
question is how often it happens and what stands between it and Punctim.

**Guards on the receive path.** There are two:

1. **Acquisition.** At least `nknown - 3` of the known prefix symbols
   (preamble + sync) must match at one origin. For a uniformly random symbol
   stream the per-origin chance is (binomial, *estimated*): default 40/2-ary
   9.7e-9, aux 32/2-ary 1.3e-6, bass and chime 20/4-ary 3.0e-8, melody 18/8-ary
   1.6e-11. Aux is the weakest, because its preamble is 16 symbols, not 24.
2. **Viterbi + CRC-16.** The soft Viterbi decoder turns *any* soft input into
   some 152-bit word, so after acquisition the HydraModem CRC-16/CCITT-FALSE is
   the only check. The expected pass rate is 2^-16 = 1.53e-5.

The modem itself never checks that the payload is a DeModFrame (sync `0xD3`,
version nibble 1, CRC-16 over bytes 0..14 in 15..16). `frame_rx`, `poly_rx`,
`hydra_modem_rx*` and the `hydra_rx` callback return any HydraModem-CRC-valid
payload. The DeModFrame gate is applied later by `punctim io` in all five
CLIs (`python/dcf/medium.py` `run_io` → `mediumlab_core.gate`, and
`dcf_medium_gate` in `C_SDK/node/punctim.c`, plus the Rust, Go and Node ports).
It is on by default and `--no-validate` turns it off. Python's
`Transport._deliver` does **not** apply the gate, and neither does
`dcf.bridge.Bridge`. The bridge forwards a gate-failing frame to every other
transport (`wire.decode` raises, so `dst` falls back to broadcast). This was
checked with a loopback bridge and the false payload
`41ee14814dfec5e9d589fcfe339e08af79`, which was relayed. On the
`dcf-bridge` path the HydraModem CRC is therefore the only guard.

**The two CRCs are not independent in the obvious way.** CRC-16/CCITT-FALSE has
no final XOR, so a message followed by its own big-endian CRC has a CRC of
`0x0000`. The HydraModem CRC field of *every* valid DeModFrame is therefore
`0x0000` (the harness self-test checks this on 1e5 frames). A HydraModem-valid
payload passes the DeModFrame gate only if that field is 0 (2^-16), byte 0 is
`0xD3` (2^-8) and the version nibble is 1 (2^-4). That is about 2^-28 = 3.7e-9
per false HydraModem frame (*estimated*, assuming uniform decoder output), and
about 2^-44 per decode that reaches the CRC.

### Measurement

`dcf-tools/false_frame.c` compiles `src/hydra_modem.c` into itself and renames
two external calls to counting wrappers. It does not change the library.
`hydra_rx_dsp_process` runs once per `decode_window` (**attempts**) and
`hydra_frame_decode_soft` runs once per window that passed acquisition
(**sync**). The one-shot path is `hydra_modem_rx` on one WAV-sized window (lead
+ frame + tail, as `frame_rx` decodes a file). The streaming path pushes the
same audio continuously through `hydra_rx_push` and counts callbacks.
Structured trials are followed by 0.25 s of silence. Every row has a fixed seed,
so rerunning gives the same TSV. The inputs are synthetic 48 kHz float, clipped
to ±1:

- **background:** digital silence; white Gaussian noise at −40, −20 and
  −6 dBFS RMS; pink noise at −20 dBFS; off-grid "music" (1–3 voices of
  12-TET ±30-cent notes with 4 harmonics, fundamentals kept off the baud grid,
  in phrases with rests); on-grid music (notes drawn from the profile's *own*
  tones, 1–4 whole symbols each, phase-continuous); an on-grid trill (each
  phrase opens with the preamble's own tonic/top-tone alternation, 4..preamble+4
  symbols, then random own tones); clicks (Poisson 5/s, impulses or
  0.2–3 ms bursts, over a −70 dBFS floor).
- **structured:** a real frame cut at 10–99 % of its body; two frames overlapped
  at 1/3 symbol and at 2–90 % of a body; a frame of another profile (aux↔default,
  melody/chime/bass/nocturne into each other); a frame with 1–8 sync bits
  flipped; a frame with 2–64 coded bits flipped; a preamble alone, and a preamble
  plus sync with nothing after it.
- **presync_random:** a correct preamble and sync followed by uniformly random
  data symbols. Every trial reaches the CRC, so this is the direct test of it.
- **codec:** soft metrics fed straight into `hydra_frame_decode_soft` (random
  Gaussian or ±1, or a valid codeword with 8–48 coded bits flipped), for volume.

Results, summed over classes (per-class rows in `dcf-tools/false_frame.tsv`):

| profile | path | class family | audio h | attempts | sync | CRC fail | false | false + DeModFrame gate |
|---|---|---|---:|---:|---:|---:|---:|---:|
| default | one-shot | background | 25.5 | 231821 | 9 | 9 | 0 | 0 |
| default | one-shot | structured | 3.1 | 13800 | 9020 | 3585 | 0 | 0 |
| default | one-shot | presync_random | 22.0 | 200000 | 200000 | 199998 | **2** | 0 |
| default | stream | background | 25.5 | 56273 | 46 | 46 | 0 | 0 |
| default | stream | structured | 4.1 | 22500 | 6309 | 1790 | 0 | 0 |
| default | stream | presync_random | 36.3 | 200000 | 200000 | 199999 | **1** | 0 |
| aux | one-shot | background | 17.0 | 185462 | 187 | 187 | 0 | 0 |
| aux | one-shot | structured | 3.6 | 13800 | 9310 | 3846 | 0 | 0 |
| aux | one-shot | presync_random | 9.2 | 100000 | 100000 | 99999 | **1** | 0 |
| aux | stream | background | 17.0 | 45941 | 38 | 38 | 0 | 0 |
| aux | stream | structured | 4.6 | 33300 | 6613 | 2277 | 0 | 0 |
| aux | stream | presync_random | 16.3 | 100000 | 100000 | 99999 | **1** | 0 |
| melody | one-shot | background | 8.5 | 6103 | 1 | 1 | 0 | 0 |
| melody | one-shot | structured | 1.9 | 1250 | 786 | 235 | 0 | 0 |
| melody | one-shot | presync_random | 4.2 | 3000 | 3000 | 2999 | **1** | 0 |
| melody | stream | background | 8.5 | 2079 | 4 | 4 | 0 | 0 |
| melody | stream | structured | 2.1 | 900 | 600 | 133 | 0 | 0 |
| melody | stream | presync_random | 4.7 | 3000 | 3000 | 3000 | 0 | 0 |
| bass | one-shot | background | 8.5 | 4267 | 0 | 0 | 0 | 0 |
| bass | one-shot | structured | 2.4 | 1150 | 791 | 265 | 0 | 0 |
| bass | one-shot | presync_random | 6.0 | 3000 | 3000 | 3000 | 0 | 0 |
| bass | stream | background | 8.5 | 1736 | 10 | 10 | 0 | 0 |
| bass | stream | structured | 2.6 | 699 | 588 | 137 | 0 | 0 |
| bass | stream | presync_random | 6.5 | 3000 | 3000 | 3000 | 0 | 0 |
| chime | one-shot | background | 8.5 | 16635 | 1 | 1 | 0 | 0 |
| chime | one-shot | structured | 0.9 | 1150 | 791 | 293 | 0 | 0 |
| chime | one-shot | presync_random | 5.1 | 10000 | 10000 | 10000 | 0 | 0 |
| chime | stream | background | 8.5 | 4041 | 26 | 26 | 0 | 0 |
| chime | stream | structured | 1.0 | 1200 | 592 | 165 | 0 | 0 |
| chime | stream | presync_random | 6.0 | 10000 | 10000 | 10000 | 0 | 0 |

In the structured family, sync hits that are not CRC failures are
**legitimate** decodes of the frame that was really sent: a frame cut at ≥75 %
of its body still decodes, as do ≤3 flipped sync bits, one voice of an
overlap, and ≤~24 flipped coded bits. A wrong-profile frame never reached the
CRC (0 sync hits in every pairing).

Codec-level (no audio; the codec is profile-independent at 316 coded bits):

| input | decodes | false | rate | DeModFrame gate |
|---|---:|---:|---:|---:|
| soft ~ N(0,1) | 4,000,000 | 58 | 1.45e-5 | 0 |
| random ±1 | 2,000,000 | 29 | 1.45e-5 | 0 |
| codeword, 8 / 16 / 20 / 24 bits flipped | 100k / 200k / 500k / 500k | 0 | < 3.0e-5 / 1.5e-5 / 6.0e-6 / 6.0e-6 (95 %) | 0 |
| codeword, 28 / 32 / 48 bits flipped | 500k / 500k / 100k | 1 / 4 / 3 | ≈ 1e-5 per wrong Viterbi output | 0 |

### What this says

- **After acquisition the CRC performs as designed.** Across the codec rows,
  87 false frames in 6.0e6 random decodes gives 1.45e-5, against 2^-16 =
  1.53e-5. With audio, presync_random gave 6 false frames in 632,000 decodes
  (9.5e-6; the Poisson 95 % interval covers 1.53e-5). When the Viterbi decoder
  settles on a *wrong* codeword (28–48 flipped bits), the CRC let through
  8 of about 786,000, again about 2^-16. Once a decode reaches the CRC, about
  1 in 65,000 comes out as a false HydraModem frame.
- **No false frame was seen outside the classes built to reach the CRC.**
  Background audio covered 136 h in total with 0 false frames (95 % bound
  < 3/h per cell, which is weak). The useful figure is the **sync-hit rate**,
  because false frames/h ≈ sync hits/h × 1.5e-5. The worst measured sync-hit
  rates were aux one-shot on noise or clicks (≈ 21–24 /h, from 41–48 in 2 h),
  chime streaming the on-grid trill (26 /h), and default streaming off-grid
  music or the trill (7–8 /h). That gives about 3–4e-4 false HydraModem
  frames/h, roughly one per 2,500–3,300 h of such input (*estimated*, not
  observed). Default one-shot on noise reached the CRC 1 time in 3 h.
- **The DeModFrame gate stopped every false frame.** None of the 101 false
  HydraModem frames (6 audio, 95 codec) passed it, and 2 of them had byte 0 =
  `0xD3`. The analytic factor of about 3.7e-9 puts the gated rate near
  1e-12/h. At these volumes that cannot be measured; it is *estimated* only.
- **The streaming receiver decodes far more often than it needs to on stationary
  noise.** Its noise EMA adapts only while searching and starts at 0, so any
  input louder than 0.02 re-triggers it at once. It decoded about 10–420 windows/h on
  noise, and about 250–7,700 windows/h on the phrase-and-rest music (more for the faster profiles). Nearly all of them
  failed acquisition. This costs CPU, but it raises the sync-hit exposure only
  through more attempts. It is behaviour, not a bug.

No crash and no manufactured frame appeared in any class not designed to reach
the CRC. No library bug was found.

**Not measured.** The inputs are synthetic. The tests did not use real
acoustic recordings, real room or background audio, real speech, real
instruments, sound-card clipping or AGC, or clock offset combined with the
adversarial classes. Coverage is 136 h of synthetic background and 116 h of
presync_random audio, far short of the hundreds to thousands of hours needed
to *observe* a false frame on background input. The musical profiles got about
8.5 h of background each, and only 3,000 (melody, bass) or 10,000 (chime)
presync_random decodes. So their CRC pass rate rests on the codec rows, which
use the same decoder. Nocturne was tested only as a wrong-profile source. The
Faust DSP backend was not tested; everything ran on the default C reference.

Reproduce (about 35 min on 4 cores; the counts are deterministic):

```sh
cd hydramodem && make && dcf-tools/build.sh
B=dcf-tools/build/false_frame
$B --selftest
$B --profile default --path oneshot,stream --hours 3 --trials 3 --presync 200000 > A.tsv &
$B --profile aux --path oneshot,stream --hours 2 --trials 3 --presync 100000 > B1.tsv &
$B --profile melody --path oneshot,stream --hours 1 --trials 1 --presync 3000 > C.tsv &
( $B --profile bass --path oneshot,stream --hours 1 --trials 1 --presync 3000 > D1.tsv
  $B --profile chime --path oneshot,stream --hours 1 --trials 1 --presync 10000 > D2.tsv ) &
wait; $B --path codec > B2.tsv     # false payloads are printed on stderr
```
