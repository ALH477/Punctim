# Building HydraModem

## Prerequisites

- A C11 compiler (GCC or Clang). The default build needs nothing else.
- For the Faust-backed build: the **Faust** compiler (`faust`) and its C glue
  header `faust/gui/CInterface.h` (ships with Faust; on Debian/Ubuntu the
  `faust` package installs it under `/usr/include/faust/`).

```bash
# Debian/Ubuntu
sudo apt-get install -y faust        # provides faust + /usr/include/faust/...
faust --version                      # 2.70.x or newer
```

## Default build (portable reference DSP)

Zero external dependencies — uses `hydra_dsp_ref.c`, which mirrors the Faust DSP
1:1. This is the right way to develop and test the framing/protocol layers.

```bash
make            # static + shared lib, tx_demo + rx_demo, all test binaries
make check      # build and run the full suite (unit + fuzz + stream + loopback)
make asan       # rebuild the suite under AddressSanitizer + UBSan and run it
make clean
```

## Faust-backed build (runs the actual compiled Faust DSP)

```bash
make faust          # regenerates build/hydratx.c + build/hydrarx.c, then builds
make faust-check    # build and run the loopback against the real Faust DSP

# if your Faust glue header is elsewhere:
make faust FAUST_INC=/path/to/include      # dir containing faust/gui/CInterface.h
```

What `make faust` runs under the hood:

```bash
faust -lang c -os -double -ftz 2 -cn hydratx -I faust faust/hydramodem_tx.dsp -o build/hydratx.c
faust -lang c -os -double -ftz 2 -cn hydrarx -I faust faust/hydramodem_rx.dsp -o build/hydrarx.c
```

- `-os` — one-sample ("one shot") architecture. The data path is carried in the
  audio in/out streams, so one sample per call is exactly what the C symbol
  clock wants, and there is no block-boundary timing jitter. Note: in `-os` mode
  you must call `control<class>()` to precompute control-rate values before the
  per-sample `compute<class>()` loop — the adapters in `src/hydra_dsp_faust_*.c`
  do this, and they also warm up the TX so `si.smoo` and `fi.dcblocker` settle
  before the frame (otherwise the gain ramp corrupts the preamble).
- `-double -ftz 2` — double precision (the correlators and phase accumulator want
  it) with flush-to-zero denormals. This is the DeMoD SKILL house standard.

### Compile-check the Faust without building any C

```bash
make validate       # faust-only syntax/type check of all three .dsp files
```

## Cross-compiling for StarFive JH7110 (SiFive U74, RV64GC, no vector ext)

The U74 is in-order dual-issue with a scalar double-precision FPU and **no**
RISC-V vector extension, so SIMD autovectorization buys nothing — build scalar,
let the hardware FPU do the floating-point, and tune for the core:

```bash
make CC=riscv64-linux-gnu-gcc \
     ARCHFLAGS="-march=rv64gc -mtune=sifive-u74"

# Faust-backed, cross-compiled:
make faust CC=riscv64-linux-gnu-gcc \
     ARCHFLAGS="-march=rv64gc -mtune=sifive-u74" \
     FAUST_INC=/usr/include          # CInterface.h is host-arch-independent
```

Notes for the JH7110 target:

- `float` is the sensible default for 48 kHz acoustic work; the DSP keeps
  `double` only where it matters (correlator/phase state, ADAA). Do **not** pass
  `-ffast-math` blindly — it can destabilize feedback structures.
- One U74 core comfortably runs a binary-FSK TX+RX at 48 kHz in real time,
  leaving the other three cores for Punctim, FEC, and application logic.
- ArchibaldOS / NixOS deployment: build the static `libhydramodem.a` and link it
  into your service; the modem has no audio-I/O dependency of its own (it
  produces/consumes float buffers — wire it to JACK/ALSA/PipeWire at the app
  layer).

## ⚠️ Profile matching (read this)

The tone count **N** is compiled into `faust/hydramodem_rx.dsp` as the constant
`N`, and the tone frequencies/baud are baked into the correlator coefficients.
The Faust DSP and the C `hydra_profile` **must agree**:

- If you change `n_tones`, `base_freq`, `tone_spacing`, or `baud` in the C
  profile, change the matching constants in `hydramodem_rx.dsp` (and
  `hydramodem_tx.dsp` if relevant) and regenerate with `make faust`.
- The RX emits quadrature, so the adapter asserts
  `getNumOutputs() == 2 * profile.n_tones` (interleaved I/Q per tone) at create
  time and returns NULL on mismatch — a forgotten regenerate fails loudly rather
  than silently mis-decoding.
- The portable reference backend reads everything from `hydra_profile` at
  runtime, so it needs no regeneration — another reason to prototype with it.

## Installing

```bash
sudo make install                       # PREFIX=/usr/local by default
make install PREFIX=/usr DESTDIR=/tmp/stage   # staged / packaged install
```

Installs the static and versioned shared libraries, the public headers under
`<includedir>/hydramodem/`, and a `pkg-config` file, so downstream builds can:

```bash
cc myapp.c $(pkg-config --cflags --libs hydramodem) -o myapp
```

## Linking into your own program

```c
#include <hydramodem/hydramodem.h>      /* or "hydra_modem.h" from the source tree */

hydra_profile p;
hydra_profile_default(&p);          /* or set fields yourself */
hydra_profile_init(&p);

uint8_t dcf[17] = { /* your Punctim DCF frame */ };
float  *audio; size_t n;
hydra_modem_tx(&p, dcf, &audio, &n);    /* -> mono float frame, you free() it  */

uint8_t out[17];
if (hydra_modem_rx(&p, audio, n, out) == HYDRA_OK) {
    /* out holds the recovered, CRC-valid 17-byte DCF frame */
}
free(audio);
```

Link against `build/libhydramodem.a` (reference) or `build/libhydramodem_faust.a`
(Faust DSP) plus `-lm`, or use the installed library via `pkg-config hydramodem`.
For streaming receive, create a `hydra_rx` and feed it audio blocks with
`hydra_rx_push` — see the API example in the README.
