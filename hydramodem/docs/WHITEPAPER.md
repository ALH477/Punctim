# HydraModem: a Faust-authored acoustic M-FSK physical layer for a certified mesh-protocol frame

**A dual reference/Faust DSP backend with hardware validation, and an honest novelty assessment.**

*Author:* Asher LeRoy — DeMoD LLC  ⟨demodllc@gmail.com⟩  ·  ORCID [0009-0006-5842-4975](https://orcid.org/0009-0006-5842-4975)
*Version 1.0 — 2026-06-27. Part of the DCF / Punctim project (LGPL-3.0).*

---

## Abstract

HydraModem is an acoustic physical layer (PHY) that carries the 17-byte `DeModFrame` of the
DCF / Punctim protocol over sound. Its per-sample DSP — a continuous-phase M-FSK (CPFSK)
modulator and a **non-coherent quadrature integrate-and-dump tone-correlator demodulator
bank** — is authored in **Faust** and compiled to C; the variable-length, packet-shaped layer
(framing, CRC-16, a K=7 rate-1/2 convolutional code with soft-decision Viterbi decoding, a
block interleaver, preamble/sync acquisition, and decision-directed symbol-timing recovery) is
implemented in C. The modem is a *transport beneath the wire quantum*: it carries the frame
opaquely, leaving DCF's 246-vector wire certificate untouched. We describe the architecture, a
dual backend (a portable-C reference DSP and the compiled-Faust DSP, kept equivalent and
buildable across Faust 2.72–2.85), and a hardware validation over two cross-cabled audio
interfaces achieving **0% packet error over 200 frames in each direction** and a clean
**full-duplex** result in one direction (300/300) with **zero cross-link bleed**. We give a
careful prior-art assessment: HydraModem is **not** the first Faust modem (GRAME published one
in 2019); the defensible contribution is the specific correlator-bank receiver plus an
FEC-protected framing stack carrying a certified protocol frame, hardware-verified.

## 1. Background

DCF (the DeMoD Communication Framework) / Punctim is a handshakeless, encryption-free mesh
protocol whose single invariant is a 17-byte wire frame, the `DeModFrame`, pinned by a
246-vector golden certificate and implemented byte-identically across many languages. The
frame is `sync(0xD3) | flags | seq | src | dst | payload(4) | ts24 | crc16`, validated by the
sync byte and a CRC-16/CCITT-FALSE over bytes [0..14] (anchor `CRC("123456789")=0x29B1`).
Everything else — UDP, Steam, WebSocket/WASM, SDR, JANUS, and acoustic links — is a *transport*
or *adapter* over this quantum.

HydraModem is the acoustic transport: it turns one 17-byte frame into a sound and recovers it,
without parsing or altering the frame, so the certificate is unaffected.

## 2. Architecture: Faust owns the per-sample DSP; C owns the packet

The design follows a strict boundary. **Faust** — a synchronous, statically-bounded stream
language — expresses what it is good at: the CPFSK phase accumulator and the quadrature mixer
bank. **C** expresses everything variable-length and data-dependent: framing, FEC,
interleaving, acquisition, timing recovery, and soft-decision decoding. Data crosses the
boundary as continuous sample streams (TX: an instantaneous-frequency signal in; RX: per-tone
I/Q out).

**Default profile.** 48 kHz sample rate; 1000 baud (48 samples/symbol); binary FSK with tones
at 2000 and 3000 Hz (integer-cycle per symbol, hence exactly orthogonal over one symbol);
24-symbol alternating preamble; 16-bit sync word `0x2DD4`; K=7, rate-1/2 convolutional code
with a block interleaver. M-FSK orders 2/4/8/16 are supported and validated.

**Receiver.** The hard part of an acoustic link is that the two devices share no sample clock
and the signal is buried in noise. The receiver is:
1. **Quadrature down-conversion** (Faust/reference DSP) to per-tone I/Q.
2. **Prefix-sum integrate-and-dump** — a symbol's energy `(ΣI)²+(ΣQ)²` in O(1) — the matched
   filter for a rectangular-envelope tone, and the reason the RX needs no carrier recovery.
3. **Acquisition** over the entire 40-symbol known prefix (24 preamble + 16 sync), so false
   alarm is negligible even when leading noise carries signal-level energy.
4. **Decision-directed symbol-timing recovery** using a total-energy timing discriminator
   gated to transition symbols; tolerates **≥ ±3000 ppm** clock offset (real audio crystals are
   ±100 ppm; walking-speed acoustic Doppler ≈ 2900 ppm).
5. **Soft-decision decode** — per-bit max-log metrics → deinterleave → soft Viterbi → CRC.

The 17-byte DCF frame plus a CRC-16 is the coded payload; the modem never interprets it.

## 3. Two DSP backends, kept equivalent

A single per-sample interface (`hydra_dsp.h`) has two implementations: a **portable-C
reference** (`hydra_dsp_ref.c`, default build, zero dependencies, the numeric reference) and a
**compiled-Faust** backend generated from the normative `.dsp` sources. They are equivalent:
`make faust-check` passes the full clean/AWGN/clock-offset suite on both; a frame modulated by
one decodes on the other (cross-decode 8/8 each way); and their TX waveforms carry identical
tone content, differing only in carrier *phase* — which the non-coherent detector ignores by
design. The Faust adapters are **version-tolerant across Faust 2.72–2.85** (the one-sample
`-os` architecture ABI changed at ~2.83; a compile-time switch selects the call convention),
verified on 2.72.14, 2.83.1, and 2.85.5.

## 4. Hardware validation

**Setup.** Two USB audio interfaces — a Native Instruments Komplete Audio 2 and a Yamaha MG-XU
— cross-connected with 1/4" (TS) instrument cables at line level (output 1 → input 1 each way).
Audio run at 48 kHz / 16-bit mono. The 17-byte payload is a real `DeModFrame` with an
incrementing per-frame counter so loss is measured exactly. Each direction is tagged with a
distinct `src_id`; the receiver rejects the other direction's frames as cross-link bleed. This
is the `DCF_FIELD_USE.md` "T2 wired-coupling" tier (pass: packet-error rate, PER, < 1% with
FEC).

**Sequential (one direction at a time), 200 frames each:**

| Direction | Frames | PER | Sync |
|---|---|---|---|
| A→B (Komplete→MG-XU) | 200/200 | **0.00%** | 16/16 |
| B→A (MG-XU→Komplete) | 200/200 | **0.00%** | 16/16 |

The two interfaces' independent crystals showed ~13 ppm of offset, absorbed by the timing
recovery. (Reaching 0% on B→A required two engineering fixes surfaced *by the measurement*: a
~1 s warm-up lead-in so the analog path settles before the first frame, and capturing the
full-resolution channel rather than a downmixed/quantized mono stream.)

**Full duplex (both directions simultaneously), 300 frames each:**

| Direction | Frames | PER | Cross-link bleed |
|---|---|---|---|
| A→B | 300/300 | **0.00%** | 0 |
| B→A | ~287/300 | ~4% | 0 |

Full-duplex operation works — both cables live at once, each interface simultaneously playing
and capturing — with **zero cross-link bleed** in both directions (the `src_id` tags confirm
the two links never contaminate each other). The residual B→A loss under *simultaneous* load is
a reproducible ~10-frame burst at a clean signal level with no clipping and no dropouts; it
recurs at the same point regardless of gain and is attributable to a host/USB full-duplex
timing transient on the interface that captures while it is also playing — not the codec, link,
level, or framing. We report it honestly rather than tuning it away.

## 5. Prior art and an honest novelty claim

HydraModem is **not** the first Faust modem. The Faust authors themselves (GRAME-CNCM / CCRMA)
published a Bell 202 audio-FSK modem with both modulator and demodulator in Faust,
hardware-verified, carrying UART frames [1]. Faust SDR demodulators also predate this work [2].
Acoustic FSK modems with convolutional FEC and a framed packet are, moreover, *standardized* —
NATO STANAG 4748 (JANUS) [3]. So "first Faust modem / PHY / demodulator" would be an overclaim.

The defensible contribution is narrower and specific: **a continuous-phase M-FSK acoustic modem
whose entire per-sample PHY in Faust is a non-coherent quadrature integrate-and-dump
tone-correlator demodulator bank (plus CPFSK modulator), coupled to a C packet layer (CRC-16,
K=7 r=1/2 convolutional FEC, block interleaver, soft-decision Viterbi, acquisition, ±3000 ppm
timing recovery) carrying a byte-certified protocol frame, hardware-verified at 0% PER.** GRAME's
2019 receiver used zero-crossing / cross-correlation; we use a correlator-bank receiver and add
the FEC-protected framing stack. To our knowledge this is the first openly documented Faust
non-coherent quadrature correlator-bank M-FSK receiver — a claim we make with that hedge, as
marketing language, not a priority claim against the published record.

## 6. Reproducibility

All results are reproducible from the public repository (DCF / Punctim) under Nix:

```sh
nix build .#hydramodem            # reference DSP: full suite (CRC 0x29B1, FEC, timing, fuzz)
nix build .#hydramodem-faust      # compiled-Faust DSP: passes the same loopback suite
hydramodem/dcf-tools/build.sh && hydramodem/dcf-tools/build/dcf_loopback   # DCF interop, byte-exact
hydramodem/dcf-tools/field-test.sh --tx-dev plughw:A,0 --rx-dev plughw:B,0 -n 200   # cabled PER
hydramodem/dcf-tools/duplex-test.sh --dev-a plughw:A,0 --dev-b plughw:B,0 -n 300    # full duplex
```

The wire certificate is not regenerated — HydraModem adds a transport, not a codec change.

## 7. License

LGPL-3.0-only (DeMoD LLC). The DSP is authored in Faust; the Faust standard libraries used at
build time are under their own permissive licenses.

## How to cite

> LeRoy, A. (DeMoD LLC). *HydraModem: a Faust-authored acoustic M-FSK physical layer for a
> certified mesh-protocol frame.* 2026. DOI: 10.5281/zenodo.XXXXXXX (assigned on archive).

A machine-readable `CITATION.cff` at the repository root tracks authorship and version.

## References

[1] R. Michon, Y. Orlarey, S. Letz, D. Fober. "Comparison and Implementation of Data
Transmission Techniques Through Analog Audio Signals in the Context of Augmented Mobile
Instruments." Sound and Music Computing Conf. (SMC-19), Málaga, 2019.
https://www.smc2019.uma.es/articles/S3/S3_01_SMC2019_paper.pdf

[2] dazdsp.org — Faust DSP code for SDR demodulation (AM/SSB/PLL), in development since 2018.
https://dazdsp.org/tech/faust/index.html

[3] NATO STANAG 4748 (JANUS) — the first ratified digital underwater acoustic communications
standard. Reference implementation (GPL-3.0): https://github.com/mission-systems-pty-ltd/janus-c
