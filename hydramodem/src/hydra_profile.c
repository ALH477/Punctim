/* hydra_profile.c */
#include "hydra_profile.h"
#include "hydra_conv.h"
#include "hydra_interleave.h"
#include <math.h>
#include <string.h>

void hydra_profile_default(hydra_profile *p)
{
    memset(p, 0, sizeof *p);             /* musical fields off => linear tone map */
    p->sample_rate   = 48000.0;
    p->baud          = 1000.0;
    p->n_tones       = 2;
    p->base_freq     = 2000.0;
    p->tone_spacing  = 1000.0;
    p->preamble_syms = 24;
    p->sync_word     = 0x2DD4u;
    p->fec_mode      = HYDRA_FEC_CONV;   /* production default: soft Viterbi */
    p->interleave    = 1;
    p->tx_gain       = 0.9;
    /* derived fields are filled by hydra_profile_init() */
}

void hydra_profile_aux_cable(hydra_profile *p)
{
    /* Aux-cable: wired line-level (3.5mm/TRS). 1.2x the default baud (1200 vs 1000);
     * with conv FEC one frame is 348 symbols = 0.290 s vs the default's 356 = 0.356 s.
     * Tones 1200/2400 Hz at 1200 baud — orthogonal (both are integer multiples of baud).
     * Short preamble (16 syms) since cable has no AGC settling.
     * NOT the python/modem/acoustic_frame.py "aux-cable" AFSK profile: that one shares
     * only the 1200 baud and a 16-unit preamble (its tones are 1000/1500 Hz, sync 0x7E,
     * CRC-8 or RS), so the two do not interoperate — see Documentation/DCF_MEDIUM_SPEC.md
     * (hydra: vs afsk:). The "4x" often quoted is AFSK aux-cable vs AFSK 300 baud. */
    memset(p, 0, sizeof *p);
    p->sample_rate   = 48000.0;
    p->baud          = 1200.0;
    p->n_tones       = 2;
    p->base_freq     = 1200.0;
    p->tone_spacing  = 1200.0;
    p->preamble_syms = 16;
    p->sync_word     = 0x2DD4u;
    p->fec_mode      = HYDRA_FEC_CONV;
    p->interleave    = 1;
    p->tx_gain       = 0.9;
    /* derived fields are filled by hydra_profile_init() */
}

/* ------------------------------ musical map -------------------------------
 * Why harmonics of the baud: the non-coherent integrate-and-dump detector is
 * exact only when every tone completes a whole number of cycles per symbol,
 * i.e. f = h * baud for integer h. The set {h * baud} is precisely the harmonic
 * series of the baud, and small-integer ratios between harmonics ARE just
 * intonation. So a consonant scale costs the detector nothing: pick the
 * harmonic numbers whose ratios spell the scale. */
static const int k_scale_fifth[2]  = { 2, 3 };
static const int k_scale_triad[4]  = { 4, 5, 6, 8 };
/* 1  9/8  5/4  3/2  5/3 | 2  9/4  5/2  -- LCD 24 */
static const int k_scale_major[8]  = { 24, 27, 30, 36, 40, 48, 54, 60 };
/* 1  6/5  4/3  3/2  9/5 | 2  12/5 8/3  -- LCD 30 */
static const int k_scale_minor[8]  = { 30, 36, 40, 45, 54, 60, 72, 80 };
static const int k_scale_major16[16] = { 24, 27, 30, 36, 40, 48, 54, 60,
                                         72, 80, 96, 108, 120, 144, 160, 192 };

static unsigned gray(unsigned d) { return d ^ (d >> 1); }

int hydra_profile_music(hydra_profile *p, hydra_scale scale, double baud,
                        double root_hz, int drone)
{
    const int *deg; int n, m, d, root;
    if (!p || baud <= 0.0 || root_hz <= 0.0) return -1;
    switch (scale) {
        case HYDRA_SCALE_FIFTH:        deg = k_scale_fifth;   n = 2;  break;
        case HYDRA_SCALE_TRIAD:        deg = k_scale_triad;   n = 4;  break;
        case HYDRA_SCALE_MAJOR_PENT:   deg = k_scale_major;   n = 8;  break;
        case HYDRA_SCALE_MINOR_PENT:   deg = k_scale_minor;   n = 8;  break;
        case HYDRA_SCALE_MAJOR_PENT16: deg = k_scale_major16; n = 16; break;
        default: return -1;
    }
    memset(p, 0, sizeof *p);
    p->sample_rate   = 48000.0;
    p->baud          = baud;
    p->n_tones       = n;
    p->preamble_syms = 12;
    p->sync_word     = 0x2DD4u;
    p->fec_mode      = HYDRA_FEC_CONV;
    p->interleave    = 1;
    p->tx_gain       = 0.9;
    p->ramp_ms       = 8.0;

    /* transpose by whole harmonics: tonic = deg[0] * m * baud, nearest root_hz */
    m = (int)floor(root_hz / ((double)deg[0] * baud) + 0.5);
    if (m < 1) m = 1;
    root = deg[0] * m;
    p->base_freq    = (double)root * baud;   /* informational: the tonic */
    p->tone_spacing = baud;                  /* informational: the grid  */

    /* Gray-map symbols onto degrees: symbol gray(d) plays degree d, so notes a
     * step apart in pitch differ in one bit. Symbol 0 = tonic. */
    for (d = 0; d < n; ++d)
        p->tone_mult[gray((unsigned)d)] = deg[d] * m;

    if (drone) {
        int k = 0;
        if (root % 2 == 0) p->drone_mult[k++] = root / 2;       /* tonic, 8ve down */
        if (root % 4 == 0) p->drone_mult[k++] = root * 3 / 4;   /* fifth above it  */
        p->drone_gain = 0.12;
    }
    return 0;
}

void hydra_profile_melody(hydra_profile *p)
{
    (void)hydra_profile_music(p, HYDRA_SCALE_MAJOR_PENT, 25.0, 600.0, 1);
}

void hydra_profile_chime(hydra_profile *p)
{
    (void)hydra_profile_music(p, HYDRA_SCALE_TRIAD, 100.0, 400.0, 1);
}

void hydra_profile_nocturne(hydra_profile *p)
{
    (void)hydra_profile_music(p, HYDRA_SCALE_MINOR_PENT, 25.0, 750.0, 1);
}

/* Validate the musical fields (only called when tone_mult[0] > 0). */
static int music_check(const hydra_profile *p)
{
    int k, j; double drone_sum = 0.0;
    if (p->n_tones > HYDRA_MUSIC_MAX_TONES) return -1;
    /* exact integer cycles need an integer number of samples per symbol */
    if (fabs((double)p->samples_per_symbol * p->baud - p->sample_rate) > 1e-6) return -1;
    for (k = 0; k < p->n_tones; ++k) {
        if (p->tone_mult[k] <= 0) return -1;
        if ((double)p->tone_mult[k] * p->baud >= 0.5 * p->sample_rate) return -1;
        for (j = 0; j < k; ++j) if (p->tone_mult[j] == p->tone_mult[k]) return -1;
    }
    for (k = 0; k < HYDRA_MUSIC_MAX_DRONES; ++k) {
        int h = p->drone_mult[k];
        if (h == 0) continue;
        if (h < 0 || (double)h * p->baud >= 0.5 * p->sample_rate) return -1;
        for (j = 0; j < p->n_tones; ++j) if (p->tone_mult[j] == h) return -1;
        drone_sum += p->drone_gain;
    }
    if (p->drone_gain < 0.0 || p->ramp_ms < 0.0) return -1;
    if (drone_sum >= p->tx_gain) return -1;     /* data must keep positive amplitude */
    return 0;
}

static int ilog2_pow2(int n)
{
    int b = 0;
    if (n <= 0) return -1;
    while ((n & 1) == 0) { n >>= 1; ++b; }
    return (n == 1) ? b : -1;   /* -1 if not a power of two */
}

int hydra_profile_init(hydra_profile *p)
{
    int bps;
    if (!p) return -1;
    if (p->sample_rate <= 0.0 || p->baud <= 0.0) return -1;
    if (p->tone_spacing <= 0.0 || p->base_freq <= 0.0) return -1;
    if (p->preamble_syms < 0) return -1;

    bps = ilog2_pow2(p->n_tones);
    if (bps < 1) return -1;                       /* n_tones must be 2,4,8,... */
    p->bits_per_symbol = bps;

    p->samples_per_symbol = (int)(p->sample_rate / p->baud + 0.5);
    if (p->samples_per_symbol < 2) return -1;

    p->lp_cut     = p->baud * 0.5;
    p->data_bits  = HYDRA_DCF_BITS + HYDRA_CRC_BITS;          /* 152 */

    switch (p->fec_mode) {
        case HYDRA_FEC_NONE: p->coded_bits = p->data_bits;             break;
        case HYDRA_FEC_REP3: p->coded_bits = p->data_bits * 3u;        break;
        case HYDRA_FEC_CONV: p->coded_bits = hydra_conv_coded_len(p->data_bits); break;
        default: return -1;
    }

    p->interleave_stride = p->interleave
                         ? hydra_interleave_stride(p->coded_bits) : 1;

    p->sync_syms  = (HYDRA_SYNC_BITS + (unsigned)bps - 1u) / (unsigned)bps; /* ceil */
    p->data_syms  = (p->coded_bits + (unsigned)bps - 1u) / (unsigned)bps;
    p->total_syms = (size_t)p->preamble_syms + p->sync_syms + p->data_syms;

    /* musical tone table: integer harmonics of the baud, orthogonal by
     * construction; validated separately (the linear checks below don't apply). */
    if (p->tone_mult[0] > 0) return music_check(p);
    /* drones are only guaranteed orthogonal against a harmonic tone table */
    if (p->drone_mult[0] || p->drone_mult[1] || p->ramp_ms < 0.0) return -1;

    /* Nyquist sanity: highest tone must fit. */
    if (hydra_tone_freq(p, p->n_tones - 1) >= 0.5 * p->sample_rate) return -1;

    /* Orthogonality: the non-coherent integrate-and-dump detector is only exact
     * when every tone completes an integer number of cycles per symbol, i.e.
     * base_freq and tone_spacing are integer multiples of the baud (this also
     * places the 2*f image on an integer cycle so it cancels). Reject configs
     * that violate it rather than mis-decode silently. */
    {
        double cb = p->base_freq   / p->baud;
        double cs = p->tone_spacing / p->baud;
        if (fabs(cb - floor(cb + 0.5)) > 1e-6) return -1;
        if (fabs(cs - floor(cs + 0.5)) > 1e-6) return -1;
    }

    return 0;
}
