# Musical profiles — M-FSK that plays in tune

HydraModem's linear profiles (`default`, `aux`) sound like a fax machine: two
tones swapping 1000 times a second. The musical profiles (`melody`, `chime`,
`nocturne`, or any `hydra_profile_music()` call) send the same 17-byte frame
through the same receiver, FEC, interleaver and CRC. Only the tone table and the
tempo change, so the data comes out as a melody over a drone.

They are an **aesthetic mode**. You trade airtime for sound, and you get a lot of
noise margin as a side effect of the slower tempo. They do not interoperate with
the linear profiles.

## The one idea: the detector already wants the harmonic series

The receiver is a non-coherent integrate-and-dump correlator. It is exact only
when every tone completes a whole number of cycles per symbol, which means
`f = h · baud` for an integer `h`. `hydra_profile_init()` has always enforced this.

The set `{h · baud}` is the **harmonic series of the baud**. Ratios of small
integers between harmonics *are* just intonation. So a consonant scale costs the
detector nothing: choose the harmonic numbers whose ratios spell the scale.

| scale (`hydra_scale`) | tones | harmonic numbers | ratios to the tonic |
|---|---|---|---|
| `HYDRA_SCALE_FIFTH` | 2 | 2 3 | 1 · 3/2 |
| `HYDRA_SCALE_TRIAD` | 4 | 4 5 6 8 | 1 · 5/4 · 3/2 · 2 |
| `HYDRA_SCALE_MAJOR_PENT` | 8 | 24 27 30 36 40 48 54 60 | do re mi so la do′ re′ mi′ |
| `HYDRA_SCALE_MINOR_PENT` | 8 | 30 36 40 45 54 60 72 80 | la do re mi so la′ do′ re′ |
| `HYDRA_SCALE_MAJOR_PENT16` | 16 | 24 … 192 | major pentatonic, three octaves |

A side note: the `default` profile (2000/3000 Hz on 1000 baud) is already a just
perfect fifth, 2:3. It doesn't sound like music because a note lasts 1 ms.
**Tempo matters more than pitch.** Notes shorter than about 30 ms are heard as
timbre, not melody.

### Why pentatonic

The data is scrambled by the convolutional code and the interleaver, so the note
sequence is effectively random. The pentatonic scale has no semitones and no
tritone, so no two of its notes clash. That makes it the scale where a random
walk still sounds intentional, the same reason it is the usual improvisation
scale. The triad goes one step further: every note belongs to one chord, so the
result is an arpeggio.

Just intonation vs 12-TET, for reference: 9/8 = 204 ¢, 5/4 = 386 ¢ (−14),
3/2 = 702 ¢ (+2), 5/3 = 884 ¢ (−16), 6/5 = 316 ¢ (+16), 9/5 = 1018 ¢ (+18).

## Music-theory decisions, and what each one costs

| decision | musical effect | engineering effect |
|---|---|---|
| **Tones = harmonics of the baud** | just-intonation scale | none: this is the detector's own orthogonality condition. `music_check()` also requires `sample_rate / baud` to be an integer, so the cycle count is exact per symbol and not just nominally exact. |
| **Gray map**: symbol `gray(d)` plays degree `d` | none directly | two notes a scale step apart differ in one bit, so a pitch-neighbour confusion costs one bit instead of up to `log2 N`. Benefit under frequency error is **[UNTESTED]**. In AWGN, orthogonal-FSK errors are not neighbour-biased. |
| **Preamble = symbols 0 and N−1** (unchanged framing) | with the Gray map this becomes a tremolo on the tonic and its octave (two octaves for 16 tones, a fifth for 2 or 4 tones). The intro states the key. | none: the known acquisition prefix is the same symbols as always, and `test_music` asserts the interval |
| **Preamble 12 symbols** (linear profiles: 24/16) | a ~0.5 s intro at 25 baud | 12 + 6 known 8-ary symbols; acquisition needs ≥ nknown−3 matches |
| **Drone**: tonic an octave down (`h0/2`), plus the fifth above that (`3h0/4`) when those are integers | a pedal under the melody. Every scale note is consonant with it. | each drone partial is also a harmonic of the baud and never a data tone. The cross term between two such tones integrates to zero over one symbol **whatever the window start**, so the drone is invisible to every correlator on the timing grid and in the acquisition scan. Measured leakage is ≤ 5e−14 of a data tone. It costs power: data amplitude drops to `tx_gain − Σ drone_gain` (0.66 of 0.9), which is ~6 % of signal power (≈ 0.3 dB at equal total power). |
| **Raised-cosine attack/release** (`ramp_ms`, 8 ms) | no onset or offset click | rendered **outside** the symbol body: the first tone is pre-rolled and the last post-rolled. No data symbol is attenuated, and the pre-roll is the same tone as preamble symbol 0. `ramp_ms = 0` (all linear profiles) renders exactly the old waveform. |

## Presets

| preset | scale | baud (note length) | tonic | drone | airtime / 17-B frame | raw bit rate |
|---|---|---|---|---|---|---|
| `melody` | major pentatonic | 25 (40 ms) | 600 Hz (≈ D5 +37 ¢) | 300 + 450 Hz | 4.96 s | 75 b/s |
| `chime` | triad arpeggio | 100 (10 ms) | 400 Hz (≈ G4 +35 ¢) | 200 + 300 Hz | 1.78 s | 200 b/s |
| `nocturne` | minor pentatonic | 25 (40 ms) | 750 Hz (≈ F♯5 +23 ¢) | 375 Hz | 4.96 s | 75 b/s |
| `default` (reference) | — (a 2:3 fifth) | 1000 (1 ms) | — | — | 0.356 s | 1000 b/s |

The tonic is `h0 · m · baud` for the integer `m` nearest the requested `root_hz`.
The scale moves in whole harmonics of the baud, so a tonic lands a few cents off
concert pitch. `--baud` (CLI) or `p->baud` retunes: pitch and tempo scale
together and orthogonality is preserved. Example: `--profile melody --baud 50`
gives 20 ms notes an octave up.

## Measured in this change

Measured with the C reference DSP on 48 kHz loopback. AWGN SNR is **wideband
per-sample** SNR over the non-silent samples, drone included, so it is
comparable across profiles but is not Eb/N0. Each cell is the frame success rate
over 20 random payloads (40 for `default`).

| SNR (dB) | −2 | −6 | −8 | −10 | −16 | −18 | −20 | −22 | −24 |
|---|---|---|---|---|---|---|---|---|---|
| `default` | 100 | 97 | 65 | 2 | — | — | — | — | — |
| `chime`, drone off / on | | | | | 100 / 100 | 75 / 55 | 0 / 0 | | |
| `melody`, drone off / on | | | | | 100 / 100 | 100 / 100 | 100 / 100 | 95 / 95 | 10 / 5 |
| `nocturne`, drone off / on | | | | | 100 / 100 | 100 / 100 | 100 / 100 | 100 / 100 | 5 / 5 |

The 25-baud presets have about 14 dB more margin than `default`, bought with 14×
the airtime. That is the processing gain of 40 ms symbols. Clock offset (10
frames per point, linear-interpolation resampler): `melody` and `nocturne` decode
at ±5000 ppm and fail at ±8000; `chime` decodes at ±3000 and fails at ±5000.
`tests/test_music.c` asserts all of this with margin: −18 dB, ±3000 ppm, clean
loopback of every scale, and the streaming receiver.

## Known limits

- **Nonlinear speakers [UNTESTED]:** intermodulation products of harmonics of
  the baud are also harmonics of the baud. They can therefore land on a data
  tone (for example drone 300 Hz + data 600 Hz → 900 Hz, a `melody` data tone).
  Linear channels (AWGN, clock offset, multipath of a steady tone) keep the drone
  orthogonal. A hard-driven speaker may not. If a real acoustic link
  misbehaves, first try `drone_mult[] = 0`.
- **Faust backend:** the compiled RX bank hard-codes a linear tone plan, so
  `hydra_rx_dsp_create()` refuses a tone table. Use the default `make` (C
  reference RX). The Faust TX is frequency-driven and works unchanged. The drone
  is synthesised in C, not in Faust.
- **Certification:** the `hydra_symbols` vectors certify the symbol stream,
  which does not depend on the profile. The musical profiles use 8 or 4 tones
  and 12-symbol preambles, which are not among the certified cases. Like every
  HydraModem waveform they are **loopback-tested, not byte-certified**.
- **Where it is wired:** the C library, `frame_tx`/`frame_rx --profile`, and
  Python (`hydra:profile=melody|chime|nocturne`, both `impl=tool` and
  `impl=cffi`). The C/Rust/Go/Node `punctim` CLIs still accept only
  `default|aux`.
- **ABI:** the tone map is appended to `hydra_profile`, which grows the struct.
  An old binary's stack profile is too small for the new constructors, so this
  is HydraModem **2.0.0** (soname `libhydramodem.so.2`).

## Try it

```sh
cd hydramodem && make check                      # includes test_music
dcf-tools/build.sh
dcf-tools/build/frame_tx d3110001000200034d555343123456e1d9 song.wav --profile melody
dcf-tools/build/frame_rx song.wav --profile melody # -> d3110001000200034d555343123456e1d9
```

```c
hydra_profile p;
hydra_profile_music(&p, HYDRA_SCALE_MINOR_PENT, 25.0, 440.0, /*drone=*/1);
hydra_profile_init(&p);   /* then hydra_modem_tx / hydra_modem_rx as usual */
```
