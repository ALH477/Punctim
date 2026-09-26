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
