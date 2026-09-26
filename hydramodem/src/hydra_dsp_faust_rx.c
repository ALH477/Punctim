/* hydra_dsp_faust_rx.c -- RX half of hydra_dsp.h backed by the Faust-generated
 * one-sample C code. Own translation unit (see hydra_dsp_faust_tx.c rationale).
 *
 * Generate first (see BUILD.md / FAUST_MODERNIZATION.md):
 *   faust -lang c -os -double -cn hydrarx -I faust \
 *         faust/hydramodem_rx.dsp -o build/hydrarx.c
 *   (no -ftz: malformed C on Faust >= 2.83; output stays equivalent.)
 *
 * IMPORTANT: the tone count N is compiled into the .dsp (the `N` constant in
 * hydramodem_rx.dsp). It must equal hydra_profile.n_tones. We assert it at
 * create time via getNumOutputshydrarx().
 */
#include "hydra_dsp.h"
#include <stdlib.h>

#include <faust/gui/CInterface.h>   /* UIGlue, MetaGlue */
#include "../build/hydrarx.c"       /* hydrarx + new/init/compute/delete       */

/* Faust `-os` ABI shift (see hydra_dsp_faust_tx.c / FAUST_MODERNIZATION.md):
 * OLD (<= 2.74) controlhydrarx + computehydrarx(dsp,&in,outs,ic,fc);
 * NEW (>= 2.83) framehydrarx(dsp,&in,outs). FAUST_REAL_CONTROLS marks the OLD ABI. */
#if defined(FAUST_REAL_CONTROLS)
#  define HYDRA_FAUST_OLD_OS 1
#endif

#define HYDRA_ICTRL_MAX 8
#define HYDRA_FCTRL_MAX 8
#define HYDRA_MAX_TONES 64

struct hydra_rx_dsp {
    hydrarx *dsp;
    int      n_tones;
#ifdef HYDRA_FAUST_OLD_OS
    int      ictrl[HYDRA_ICTRL_MAX];
    double   fctrl[HYDRA_FCTRL_MAX];
#endif
};

/* one audio sample in -> 2N tone I/Q out */
static inline void hydra_rx_step(hydra_rx_dsp *d, FAUSTFLOAT *in, FAUSTFLOAT *outs)
{
#ifdef HYDRA_FAUST_OLD_OS
    computehydrarx(d->dsp, in, outs, d->ictrl, d->fctrl);
#else
    framehydrarx(d->dsp, in, outs);
#endif
}

hydra_rx_dsp *hydra_rx_dsp_create(const hydra_profile *p)
{
    hydra_rx_dsp *d;
    /* the compiled bank hard-codes a LINEAR tone plan; a musical tone table
     * (hydra_profile_music) needs the C reference RX (default `make`). */
    if (p->tone_mult[0] > 0) return NULL;
    d = (hydra_rx_dsp *)calloc(1, sizeof *d);
    if (!d) return NULL;
    d->dsp = newhydrarx();
    if (!d->dsp) { free(d); return NULL; }
    inithydrarx(d->dsp, (int)p->sample_rate);
    d->n_tones = p->n_tones;
    /* the compiled bank emits 2 outputs (I,Q) per tone; must match the profile */
    if (getNumOutputshydrarx(d->dsp) != 2 * p->n_tones ||
        p->n_tones > HYDRA_MAX_TONES) {
        deletehydrarx(d->dsp); free(d); return NULL;
    }
    return d;
}

void hydra_rx_dsp_destroy(hydra_rx_dsp *d)
{
    if (!d) return;
    if (d->dsp) deletehydrarx(d->dsp);
    free(d);
}

void hydra_rx_dsp_process(hydra_rx_dsp *d, const float *audio_in, float *iq_out, int n)
{
    int i, j, M = 2 * d->n_tones;          /* I,Q per tone */
    FAUSTFLOAT outs[2 * HYDRA_MAX_TONES];
#ifdef HYDRA_FAUST_OLD_OS
    /* old -os: precompute control-rate values (none for RX) before the loop */
    controlhydrarx(d->dsp, d->ictrl, d->fctrl);
#endif
    for (i = 0; i < n; ++i) {
        FAUSTFLOAT in = audio_in[i];
        hydra_rx_step(d, &in, outs);
        for (j = 0; j < M; ++j)
            iq_out[(size_t)i * M + j] = outs[j];
    }
}
