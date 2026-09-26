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

/* Musical tone map capacity (see hydra_profile_music). */
#define HYDRA_MUSIC_MAX_TONES   16
#define HYDRA_MUSIC_MAX_DRONES  2

/* Built-in scales for hydra_profile_music(). Each is a set of harmonic numbers
 * (frequency ratios to the baud), i.e. just intonation on the baud's harmonic
 * series. The tone count is fixed by the scale. */
typedef enum {
    HYDRA_SCALE_FIFTH      = 0, /*  2 tones  2:3               perfect fifth        */
    HYDRA_SCALE_TRIAD      = 1, /*  4 tones  4:5:6:8           major triad + octave */
    HYDRA_SCALE_MAJOR_PENT = 2, /*  8 tones  24:27:30:36:40:48:54:60 (do re mi so la do' re' mi') */
    HYDRA_SCALE_MINOR_PENT = 3, /*  8 tones  30:36:40:45:54:60:72:80 (la do re mi so la' do' re') */
    HYDRA_SCALE_MAJOR_PENT16 = 4 /* 16 tones major pentatonic 24..192, three octaves  */
} hydra_scale;

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

    /* --- musical tone map (optional; appended so the fields above keep their
     *     offsets). All zero => the linear map base_freq + k*tone_spacing.
     *     Filled by hydra_profile_music(); see docs/MUSIC.md. --- */
    int      tone_mult[HYDRA_MUSIC_MAX_TONES]; /* tone k = tone_mult[k] * baud Hz.   */
                              /*   Integer multiples of the baud ARE the harmonic  */
                              /*   series of the baud, so a table of them is a     */
                              /*   just-intonation scale that is orthogonal by     */
                              /*   construction. tone_mult[0] == 0 => linear map.  */
    int      drone_mult[HYDRA_MUSIC_MAX_DRONES]; /* accompaniment partials, same   */
                              /*   units; 0 = unused. Must not collide with a tone,*/
                              /*   which makes each partial orthogonal to every    */
                              /*   data correlator at ANY window alignment.        */
    double   drone_gain;      /* amplitude of each drone partial (TX only). The    */
                              /*   data carrier is scaled to tx_gain - sum(drone)  */
                              /*   so the peak never exceeds tx_gain.              */
    double   ramp_ms;         /* raised-cosine attack/release, rendered OUTSIDE    */
                              /*   the symbol body (pre-roll of the first tone,    */
                              /*   post-roll of the last), so no symbol is touched.*/
} hydra_profile;

/* A sensible default: orthogonal binary FSK in the voice band at 48 kHz.
 *   tones 2000 / 3000 Hz, 1000 baud (48 samples/symbol), 24-symbol preamble,
 *   sync 0x2DD4, FEC convolutional (K=7 r=1/2, soft Viterbi) with the coded-bit
 *   interleaver on, gain 0.9. Matches faust/hydramodem_{tx,rx}.dsp. */
void hydra_profile_default(hydra_profile *p);

/* Aux-cable profile: wired line-level connection (3.5mm/TRS).
 *   1200 baud (1.2x the default), tones 1200/2400 Hz (orthogonal at 48 kHz),
 *   16-symbol preamble (short — no AGC settling needed on cable), FEC convolutional.
 *   NOT interoperable with python/modem/acoustic_frame.py's "aux-cable" AFSK profile
 *   (different tones, sync word and FEC): that is the afsk: medium, this is hydra:. */
void hydra_profile_aux_cable(hydra_profile *p);

/* Musical profile: M-FSK whose tones are a just-intonation scale on the harmonic
 * series of the baud. Symbols are Gray-mapped onto scale degrees, so two
 * pitch-adjacent notes differ in exactly one bit, and the preamble (tones 0 and
 * N-1) becomes a tremolo on the tonic and its octave (two octaves for 16
 * tones, a fifth for FIFTH/TRIAD).
 *   scale      one of hydra_scale
 *   baud       symbol rate; sample_rate/baud must be an integer (exact cycles)
 *   root_hz    desired tonic; the achieved tonic is the nearest root_harmonic *
 *              m * baud (m >= 1 integer), i.e. the scale is transposed by whole
 *              harmonics of the baud and may sit a few cents off root_hz.
 *   drone      1 = add a root (octave below) + fifth drone where those partials
 *              are integers, 0 = none.
 * FEC conv + interleave, 48 kHz, tx_gain 0.9, ramp 8 ms, preamble 12. Call
 * hydra_profile_init() afterwards as usual. Returns 0 ok, <0 bad arguments.
 * NOT interoperable with the linear profiles; loopback-tested, not certified
 * beyond the symbol stream (the symbol stream is profile-generic). */
int  hydra_profile_music(hydra_profile *p, hydra_scale scale, double baud,
                         double root_hz, int drone);

/* Named musical presets (hydra_profile_music with fixed arguments):
 *   melody   : major pentatonic, 25 baud (40 ms notes), tonic 600 Hz, drone
 *   chime    : major triad arpeggio, 100 baud (10 ms notes), tonic 400 Hz, drone
 *   nocturne : minor pentatonic, 25 baud, tonic 750 Hz, drone (root only) */
void hydra_profile_melody(hydra_profile *p);
void hydra_profile_chime(hydra_profile *p);
void hydra_profile_nocturne(hydra_profile *p);

/* Compute derived fields and validate. Returns 0 on success, <0 on bad config:
 * n_tones not a power of two, non-positive rates, highest tone above Nyquist, or
 * tones not integer-cycle (base_freq / tone_spacing not integer multiples of the
 * baud) -- the integrate-and-dump detector requires that orthogonality. Any
 * n_tones that is a power of two is accepted; the 16-bit sync and coded payload
 * are zero-padded to whole symbols when bits/symbol does not divide them. */
int  hydra_profile_init(hydra_profile *p);

/* Tone index -> frequency (Hz). */
static inline double hydra_tone_freq(const hydra_profile *p, int tone) {
    if (p->tone_mult[0] > 0) return (double)p->tone_mult[tone] * p->baud;
    return p->base_freq + (double)tone * p->tone_spacing;
}

#ifdef __cplusplus
}
#endif
#endif /* HYDRA_PROFILE_H */
