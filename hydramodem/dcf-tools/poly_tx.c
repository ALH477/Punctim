// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/poly_tx.c -- render TWO 17-byte DeModFrames into ONE WAV, one per voice
 * of the polyphonic `duet` profile: frame A on the melody voice (major pentatonic,
 * 600-1500 Hz), frame B on the bass voice (D2 B2 D3 A3, 75-225 Hz), sounding
 * together. The acoustic counterpart of SuperPack: a frame pair in one burst.
 * Repo glue (DeMoD LLC, LGPL-3.0). See hydramodem/docs/MUSIC.md, "Polyphony".
 *
 *   poly_tx <34-hex frame A> <34-hex frame B> out.wav
 */
#include "../src/hydramodem.h"
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
    uint8_t f[2][HYDRA_DCF_BYTES];
    hydra_profile v[2];
    float *audio = NULL; size_t n = 0;
    int rc;
    if (argc != 4) {
        fprintf(stderr, "usage: %s <17-byte-hex A (melody)> <17-byte-hex B (bass)> out.wav\n", argv[0]);
        return 2;
    }
    if (parse_hex(argv[1], f[0], HYDRA_DCF_BYTES) || parse_hex(argv[2], f[1], HYDRA_DCF_BYTES)) {
        fprintf(stderr, "bad hex (need %d hex chars per frame)\n", 2 * (int)HYDRA_DCF_BYTES);
        return 2;
    }
    hydra_profile_duet(v);
    if (hydra_profile_init(&v[0]) || hydra_profile_init(&v[1])) { fprintf(stderr, "bad profile\n"); return 2; }
    if (hydra_modem_tx_poly(v, 2, f, &audio, &n) != HYDRA_OK) { fprintf(stderr, "tx failed\n"); return 1; }
    rc = hydra_wav_write(argv[3], audio, n, (int)v[0].sample_rate);
    free(audio);
    if (rc != 0) { fprintf(stderr, "write %s failed\n", argv[3]); return 1; }
    return 0;
}
