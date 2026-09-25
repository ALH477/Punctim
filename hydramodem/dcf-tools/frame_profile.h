// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/frame_profile.h -- shared CLI -> hydra_profile parsing for frame_tx/frame_rx.
 * Named profile (default|aux), FEC mode, interleaver on/off, preamble length and FDMA
 * tone-channel overrides (so each node can sit on a distinct frequency band of the line).
 * Repo glue (DeMoD LLC, LGPL-3.0). */
#ifndef DCF_FRAME_PROFILE_H
#define DCF_FRAME_PROFILE_H
#include "../src/hydramodem.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The option summary shared by the frame_tx/frame_rx usage text. The DCF `hydra:`
 * transport (python/dcf/transport.py hydra_tool_caps) probes the usage text for
 * "--profile" / "--interleave" / "--preamble", so keep those spellings here. */
#define FRAME_PROFILE_USAGE \
    "[--profile default|aux] [--none|--rep3|--conv] [--interleave 0|1] [--preamble N]\n" \
    "        [--base-freq HZ] [--tone-spacing HZ] [--baud HZ] [--n-tones N]"

/* Strict decimal int parse: whole string, no trailing junk. Returns 0 ok, -1 bad. */
static int frame_profile_int(const char *s, long lo, long hi, int *out)
{
    char *end = NULL;
    long v = strtol(s, &end, 10);
    if (!*s || *end || v < lo || v > hi) return -1;
    *out = (int)v;
    return 0;
}

/* Apply argv[start..] onto a profile. Returns 0 ok, <0 on a bad arg.
 *   --profile default|aux         base profile: hydra_profile_default() (2000/3000 Hz,
 *                                 1000 baud, 24-sym preamble) or hydra_profile_aux_cable()
 *                                 (1200/2400 Hz, 1200 baud, 16-sym preamble); both conv +
 *                                 interleave. Applied FIRST (re-defaults the profile), so
 *                                 it may appear anywhere and never clobbers the overrides.
 *   --none|--rep3|--conv          FEC mode
 *   --interleave 0|1              coded-bit block interleaver off/on
 *   --preamble N                  alternating-tone preamble length in symbols (N >= 0)
 *   --base-freq HZ                tone 0 frequency (FDMA channel)
 *   --tone-spacing HZ             spacing between tones
 *   --baud HZ  --n-tones N        symbol rate / M-FSK order
 * base_freq and tone_spacing must stay integer multiples of baud (HydraModem enforces it).
 * The caller MUST run hydra_profile_init() afterwards to recompute the derived fields. */
static int frame_profile_args(hydra_profile *p, int argc, char **argv, int start)
{
    /* pass 1: the named base profile (last one wins), so overrides are order-independent */
    for (int i = start; i < argc; ++i) {
        if (strcmp(argv[i], "--profile") != 0) continue;
        if (i + 1 >= argc) { fprintf(stderr, "--profile needs default|aux\n"); return -1; }
        const char *name = argv[++i];
        if      (!strcmp(name, "default")) hydra_profile_default(p);
        else if (!strcmp(name, "aux"))     hydra_profile_aux_cable(p);
        else { fprintf(stderr, "bad --profile: %s (default|aux)\n", name); return -1; }
    }
    /* pass 2: everything else overrides the base profile */
    for (int i = start; i < argc; ++i) {
        int v;
        if      (!strcmp(argv[i], "--profile") && i + 1 < argc) ++i;   /* pass 1 */
        else if (!strcmp(argv[i], "--none")) p->fec_mode = HYDRA_FEC_NONE;
        else if (!strcmp(argv[i], "--rep3")) p->fec_mode = HYDRA_FEC_REP3;
        else if (!strcmp(argv[i], "--conv")) p->fec_mode = HYDRA_FEC_CONV;
        else if (!strcmp(argv[i], "--interleave") && i + 1 < argc) {
            if (frame_profile_int(argv[++i], 0, 1, &v)) {
                fprintf(stderr, "bad --interleave: %s (0|1)\n", argv[i]); return -1;
            }
            p->interleave = v;
        }
        else if (!strcmp(argv[i], "--preamble") && i + 1 < argc) {
            if (frame_profile_int(argv[++i], 0, 65535, &v)) {
                fprintf(stderr, "bad --preamble: %s (N >= 0)\n", argv[i]); return -1;
            }
            p->preamble_syms = v;
        }
        else if (!strcmp(argv[i], "--base-freq")    && i + 1 < argc) p->base_freq    = atof(argv[++i]);
        else if (!strcmp(argv[i], "--tone-spacing") && i + 1 < argc) p->tone_spacing = atof(argv[++i]);
        else if (!strcmp(argv[i], "--baud")         && i + 1 < argc) p->baud         = atof(argv[++i]);
        else if (!strcmp(argv[i], "--n-tones")      && i + 1 < argc) p->n_tones      = atoi(argv[++i]);
        else { fprintf(stderr, "bad arg: %s\n", argv[i]); return -1; }
    }
    return 0;
}
#endif
