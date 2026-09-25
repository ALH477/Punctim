/* hydra_profile.c */
#include "hydra_profile.h"
#include "hydra_conv.h"
#include "hydra_interleave.h"
#include <math.h>

void hydra_profile_default(hydra_profile *p)
{
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
