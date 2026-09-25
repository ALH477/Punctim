// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/frame_tx.c -- render ONE 17-byte DeModFrame (given as hex) to a WAV via
 * HydraModem. The single-frame counterpart to tx_campaign, used by the DCF `hydra:`
 * transport (which drives HydraModem as a subprocess PHY). Repo glue (DeMoD LLC, LGPL-3.0).
 *
 *   frame_tx <34-hex-char-frame> out.wav [--profile default|aux] [--none|--rep3|--conv]
 *            [--interleave 0|1] [--preamble N] [--base-freq HZ] [--tone-spacing HZ]
 *            [--baud HZ] [--n-tones N]
 * Defaults: profile default (2000/3000 Hz, 1000 baud), conv FEC, interleave on. The RX
 * side must use the same profile/FEC/interleave/tone flags (see frame_profile.h).
 */
#include "../src/hydramodem.h"
#include "frame_profile.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int parse_hex(const char *h, uint8_t *out, int n)
{
    if ((int)strlen(h) != 2 * n) return -1;
    for (int i = 0; i < n; ++i) {
        unsigned v;
        if (sscanf(h + 2 * i, "%2x", &v) != 1) return -1;
        out[i] = (uint8_t)v;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <17-byte-hex> out.wav " FRAME_PROFILE_USAGE "\n"
                        "  defaults: --profile default --conv --interleave 1\n", argv[0]);
        return 2;
    }
    uint8_t frame[HYDRA_DCF_BYTES];
    if (parse_hex(argv[1], frame, HYDRA_DCF_BYTES)) {
        fprintf(stderr, "bad hex (need %d bytes / %d hex chars)\n",
                (int)HYDRA_DCF_BYTES, 2 * (int)HYDRA_DCF_BYTES);
        return 2;
    }
    hydra_profile p;
    hydra_profile_default(&p);
    if (frame_profile_args(&p, argc, argv, 3) != 0) return 2;
    if (hydra_profile_init(&p) != 0) { fprintf(stderr, "bad profile\n"); return 2; }

    float *audio = NULL; size_t n = 0;
    if (hydra_modem_tx(&p, frame, &audio, &n) != HYDRA_OK) {
        fprintf(stderr, "tx failed\n"); return 1;
    }
    int rc = hydra_wav_write(argv[2], audio, n, (int)p.sample_rate);
    free(audio);
    if (rc != 0) { fprintf(stderr, "write %s failed\n", argv[2]); return 1; }
    return 0;
}
