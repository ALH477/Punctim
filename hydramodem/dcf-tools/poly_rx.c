// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/poly_rx.c -- decode both frames of a `duet` WAV (poly_tx): prints two
 * lines, frame A (melody voice) then frame B (bass voice), each 34 hex chars, or
 * "-" for a voice that did not decode. Exit 0 when both decoded, 1 otherwise.
 * Each voice is decoded independently with its own profile (melody, bass), so
 * one voice can be recovered when the other is lost.
 * Repo glue (DeMoD LLC, LGPL-3.0). See hydramodem/docs/MUSIC.md, "Polyphony".
 *
 *   poly_rx in.wav
 */
#include "../src/hydramodem.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    hydra_profile v[2];
    uint8_t out[2][HYDRA_DCF_BYTES];
    int st[2], sr = 0, k, i;
    float *audio = NULL; size_t n = 0;
    if (argc != 2) { fprintf(stderr, "usage: %s in.wav\n", argv[0]); return 2; }
    hydra_profile_melody(&v[0]);
    hydra_profile_bass(&v[1]);
    if (hydra_profile_init(&v[0]) || hydra_profile_init(&v[1])) { fprintf(stderr, "bad profile\n"); return 2; }
    if (hydra_wav_read(argv[1], &audio, &n, &sr) != 0) { fprintf(stderr, "read %s failed\n", argv[1]); return 1; }
    k = hydra_modem_rx_poly(v, 2, audio, n, out, st);
    free(audio);
    for (int vo = 0; vo < 2; ++vo) {
        if (st[vo] != HYDRA_OK) { puts("-"); continue; }
        for (i = 0; i < (int)HYDRA_DCF_BYTES; ++i) printf("%02x", out[vo][i]);
        putchar('\n');
    }
    return k == 2 ? 0 : 1;
}
