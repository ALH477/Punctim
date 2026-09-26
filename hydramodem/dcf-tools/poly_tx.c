// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/poly_tx.c -- render TWO 17-byte DeModFrames into ONE WAV, one per voice
 * of the polyphonic `duet` profile: frame A on the melody voice (major pentatonic,
 * 600-1500 Hz), frame B on the bass voice (D2 B2 D3 A3, 75-225 Hz), sounding
 * together. The acoustic counterpart of SuperPack: a frame pair in one burst.
 * Repo glue (DeMoD LLC, LGPL-3.0). See hydramodem/docs/MUSIC.md, "Polyphony".
 *
 *   poly_tx <34-hex frame A> <34-hex frame B> out.wav
 *   poly_tx <34-hex frame A> out.wav      (a lone frame: the melody voice alone, as the
 *                                         `hydra:profile=duet` medium sends an odd frame)
 */
#include "../src/hydramodem.h"
#include "frame_profile.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    uint8_t f[2][HYDRA_DCF_BYTES];
    hydra_profile v[2];
    float *audio = NULL; size_t n = 0;
    int rc, nv = argc - 2;              /* 1 or 2 voices */
    const char *out = argv[argc - 1];
    if (argc != 3 && argc != 4) {
        fprintf(stderr, "usage: %s <17-byte-hex A (melody)> [<17-byte-hex B (bass)>] out.wav\n", argv[0]);
        return 2;
    }
    if (frame_parse_hex(argv[1], f[0], HYDRA_DCF_BYTES) ||
        (nv == 2 && frame_parse_hex(argv[2], f[1], HYDRA_DCF_BYTES))) {
        fprintf(stderr, "bad hex (need %d hex chars per frame)\n", 2 * (int)HYDRA_DCF_BYTES);
        return 2;
    }
    hydra_profile_duet(v);
    if (hydra_profile_init(&v[0]) || hydra_profile_init(&v[1])) { fprintf(stderr, "bad profile\n"); return 2; }
    if (hydra_modem_tx_poly(v, nv, f, &audio, &n) != HYDRA_OK) { fprintf(stderr, "tx failed\n"); return 1; }
    rc = hydra_wav_write(out, audio, n, (int)v[0].sample_rate);
    free(audio);
    if (rc != 0) { fprintf(stderr, "write %s failed\n", out); return 1; }
    return 0;
}
