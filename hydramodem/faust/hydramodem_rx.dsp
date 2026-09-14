//=============================== hydramodem_rx.dsp ============================
// Punctim acoustic modem -- RECEIVER top level.
//
//   Input    0       : acoustic samples (from ADC / capture).
//   Outputs  0..N-1  : per-tone energy (mag^2) from the non-coherent
//                      quadrature correlator bank.
//
// The C layer reads the N energy streams and performs symbol timing, argmax
// tone decisions, sync search, bit<->symbol unpacking, de-FEC, CRC check, and
// recovery of the 17-byte DCF payload.
//
// PROFILE CONSTANTS -- must match the TX and the C config (hydra_profile).
// Defaults: orthogonal binary FSK in the voice band at 48 kHz.
//   tone k frequency = base + k*spacing
//   With base/spacing chosen as integer multiples of the baud rate, each tone
//   sits on an exact correlator bin over one 48-sample symbol => orthogonal.
//=============================================================================
declare name    "hydramodem_rx";
declare version "0.1.0";
declare author  "DeMoD LLC";

import("stdfaust.lib");
m = library("demod_modem.lib");

N       = 2;        // number of FSK tones  (bits/symbol = log2 N)
base    = 2000.0;   // Hz, tone 0
spacing = 1000.0;   // Hz, tone step       -> tone 1 = 3000 Hz

// Optional input AGC ahead of the bank (acoustic level varies with distance).
// DISABLED by default: the production receiver's soft metrics are amplitude-
// normalized in C, so an explicit AGC is unnecessary and would only complicate
// matching the C reference. To enable on real hardware, mirror m.agc in C too.
precond = _;                       // raw passthrough (default, matches C ref)
// precond = _ : m.agc(8.0 / 1000.0);

// Input 0: acoustic samples. Outputs 0..2N-1: interleaved per-tone I,Q
// (I_0,Q_0,I_1,Q_1,...). The C layer integrates each (I,Q) over a symbol.
process = precond : m.tonebank_iq(N, base, spacing);
