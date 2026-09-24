/* hydra_profile.h -- Punctim acoustic modem profile + shared constants.
 *
 * One profile fully describes a compatible TX/RX pair. The Faust top-level
 * .dsp files hard-code the same numbers as compile-time constants; keep them
 * in sync (see faust/hydramodem_rx.dsp). The DCF payload is always 17 bytes
 * and is transported opaquely -- the modem neither parses nor depends on its
 * internal wire format.
 */
#ifndef HYDRA_PROFILE_H
#define HYDRA_PROFILE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The Punctim DCF frame transported by one acoustic packet. Fixed size. */
#define HYDRA_DCF_BYTES   17u
#define HYDRA_DCF_BITS    (HYDRA_DCF_BYTES * 8u)   /* 136 */
#define HYDRA_CRC_BYTES   2u
#define HYDRA_CRC_BITS    (HYDRA_CRC_BYTES * 8u)   /* 16  */
#define HYDRA_SYNC_BITS   16u

/* Forward error correction mode. */
typedef enum {
    HYDRA_FEC_NONE = 0,   /* uncoded                                          */
    HYDRA_FEC_REP3 = 1,   /* repetition-3 + majority vote (simple, hard)      */
    HYDRA_FEC_CONV = 2    /* K=7 rate-1/2 convolutional + soft Viterbi (best) */
} hydra_fec_mode;

typedef struct {
    /* --- user-set physical parameters --- */
    double   sample_rate;     /* Hz, e.g. 48000                                  */
    double   baud;            /* symbols/sec, e.g. 1000                          */
    int      n_tones;         /* MUST be a power of two (2,4,8,16,...)           */
    double   base_freq;       /* Hz, tone 0; MUST be an integer multiple of baud */
    double   tone_spacing;    /* Hz, tone k = base_freq + k*tone_spacing;         */
                              /*   MUST be an integer multiple of baud (orthogonal)*/
    int      preamble_syms;   /* alternating-tone lead-in length, e.g. 24        */
    uint16_t sync_word;       /* 16-bit frame-alignment anchor, e.g. 0x2DD4      */
    hydra_fec_mode fec_mode;  /* none / rep3 / convolutional                     */
    int      interleave;      /* 1 = block-interleave coded bits (recommended    */
                              /*     with CONV for burst/reverb resilience)      */
    double   tx_gain;         /* 0..1 output scale (DAC headroom), e.g. 0.9      */

    /* --- derived (filled by hydra_profile_init) --- */
    int      bits_per_symbol; /* log2(n_tones)                                   */
    int      samples_per_symbol;
    double   lp_cut;          /* legacy correlator cutoff = baud/2 (unused by    */
                              /*   the I/Q receiver; kept for the energy bank)   */
    size_t   data_bits;       /* DCF + CRC bits before coding (152)             */
    size_t   coded_bits;      /* after the selected FEC                          */
    int      interleave_stride;/* coprime stride for the coded-bit interleaver   */
    size_t   sync_syms;       /* HYDRA_SYNC_BITS / bits_per_symbol               */
    size_t   data_syms;       /* ceil(coded_bits / bits_per_symbol)             */
    size_t   total_syms;      /* preamble + sync + data                          */
} hydra_profile;

/* A sensible default: orthogonal binary FSK in the voice band at 48 kHz.
 *   tones 2000 / 3000 Hz, 1000 baud (48 samples/symbol), 24-symbol preamble,
 *   sync 0x2DD4, FEC convolutional (K=7 r=1/2, soft Viterbi) with the coded-bit
 *   interleaver on, gain 0.9. Matches faust/hydramodem_{tx,rx}.dsp. */
void hydra_profile_default(hydra_profile *p);

/* Aux-cable profile: wired line-level connection (3.5mm/TRS).
 *   1200 baud (4x faster than default), tones 1200/2400 Hz (orthogonal at 48 kHz),
 *   16-symbol preamble (short — no AGC settling needed on cable), FEC convolutional.
 *   Matches python/modem/acoustic_frame.py "aux-cable" profile behavior. */
void hydra_profile_aux_cable(hydra_profile *p);

/* Compute derived fields and validate. Returns 0 on success, <0 on bad config:
 * n_tones not a power of two, non-positive rates, highest tone above Nyquist, or
 * tones not integer-cycle (base_freq / tone_spacing not integer multiples of the
 * baud) -- the integrate-and-dump detector requires that orthogonality. Any
 * n_tones that is a power of two is accepted; the 16-bit sync and coded payload
 * are zero-padded to whole symbols when bits/symbol does not divide them. */
int  hydra_profile_init(hydra_profile *p);

/* Tone index -> frequency (Hz). */
static inline double hydra_tone_freq(const hydra_profile *p, int tone) {
    return p->base_freq + (double)tone * p->tone_spacing;
}

#ifdef __cplusplus
}
#endif
#endif /* HYDRA_PROFILE_H */
