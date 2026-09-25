// SPDX-License-Identifier: LGPL-3.0-only
// ============================================================================
// DCF Modem — Faust signal design for "modulations across quanta mediums".
//
// Normative waveform spec for the DCF C-SDK Faust-DSP modem transport. The
// certified layer is the byte<->symbol Gray mapping (codec/demod_modulation.h,
// codec/src/modulation.rs, python/MCP/modulationlab_core.py); THIS file defines
// how a symbol index is rendered onto a carrier for the *audio* medium (no
// live-audio backend is wired up yet; see DCF_MODEM_SPEC.md). The portable C reference (C_SDK/node/dcf_modem.h)
// renders the same designs as sample snippets and recovers them by matched
// filtering — exact over an ideal (loopback/file) medium. Per DCF-Audio policy,
// the synthesised waveform is NOT byte-certified across languages; only the
// symbol mapping is.
//
//   modulation_id 0  FSK  : binary tones (Bell-202 inspired) — mark/space
//   modulation_id 1  OOK  : on-off keying of a single carrier
//   modulation_id 2  PSK  : QPSK — carrier phase in {0, 90, 180, 270} degrees
//   modulation_id 3  QAM  : 16-QAM — I/Q baseband, levels {-3,-1,1,3} (Gray)
//
// declare-only carrier constants mirror dcf_modem.h (cycles/sample at the modem
// rate; the audio backend scales to its sample rate).
// ============================================================================

declare name      "DCF Modem";
declare author    "DeMoD LLC";
declare license   "LGPL-3.0-only";
declare version   "0.3.0";

import("stdfaust.lib");

// Carrier / tone constants (cycles per sample) — identical to dcf_modem.h.
fc   = 0.25;     // PSK/QAM carrier
fsk0 = 0.1875;   // FSK space tone (symbol 0)
fsk1 = 0.3125;   // FSK mark  tone (symbol 1)

// Symbol-driven oscillators. `sym` is the Gray-coded symbol index on a control
// line; `bit` its low bit. Phasor `ph(f)` accumulates a normalised carrier phase.
ph(f) = os.phasor(1.0, f);

// FSK: choose tone by bit.
fsk(bit) = sin(2.0 * ma.PI * ph(select2(bit, fsk0, fsk1)));

// OOK: gate the carrier by bit.
ook(bit) = sin(2.0 * ma.PI * ph(fc)) * bit;

// PSK (QPSK): phase offset = (pi/2) * value, value = ungray(sym) in {0..3}.
psk(value) = cos(2.0 * ma.PI * ph(fc) + (ma.PI / 2.0) * value);

// QAM (16): I/Q baseband modulation; i,q in {-3,-1,1,3}, normalised by 3.
qam(i, q) = (i * cos(2.0 * ma.PI * ph(fc)) - q * sin(2.0 * ma.PI * ph(fc))) / 3.0;

// Default process: a bit input drives the binary FSK carrier (the simplest,
// fully self-describing path). The C SDK selects the scheme per modulation_id.
process = fsk;
