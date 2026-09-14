//=============================== hydramodem_tx.dsp ============================
// Punctim acoustic modem -- TRANSMITTER top level.
//
//   Input  0 : instantaneous frequency (Hz), driven sample-by-sample by the C
//              layer (one tone frequency held per symbol period).
//   Output 0 : modulated acoustic sample.
//
// The C layer (src/) sequences preamble, sync word, payload (17-byte DCF frame),
// CRC and optional FEC by writing the right frequency to input 0 each sample.
// Phase continuity across symbol boundaries is automatic (CPFSK).
//
// Compile (deployment, StarFive JH7110 / RV64GC scalar):
//   faust -lang c -os -double -ftz 2 -cn hydratx \
//         -I faust faust/hydramodem_tx.dsp -o build/hydratx.c
//   $(CC) -O3 -march=rv64gc -mtune=sifive-u74 ...
//
// Block processing is fine here because timing is carried in the input data
// stream, not in control updates -- so -os is optional (use it only for lowest
// latency). See BUILD.md.
//=============================================================================
declare name    "hydramodem_tx";
declare version "0.1.0";
declare author  "DeMoD LLC";

import("stdfaust.lib");
m = library("demod_modem.lib");

// Output level (leave DAC headroom). Exposed as a control so it can be trimmed
// without recompiling; smoothed + DC-blocked inside m.txout.
gain = hslider("[1]TX Gain", 0.9, 0.0, 1.0, 0.001);

process = m.cpfsk : m.txout(gain);
