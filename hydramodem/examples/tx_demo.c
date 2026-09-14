/* examples/tx_demo.c -- encode a message as one Punctim acoustic frame.
 *
 *   ./tx_demo "up to 17 bytes" out.wav
 *
 * The message is padded/truncated to exactly 17 bytes (the DCF payload size)
 * and written as a mono 16-bit WAV. Play it into another device's mic, or pipe
 * it through FFmpeg, then decode with rx_demo.
 */
#include "../src/hydra_modem.h"
#include "../src/wav.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    hydra_profile p;
    uint8_t payload[HYDRA_DCF_BYTES];
    float  *audio = NULL; size_t n = 0;
    const char *msg, *path;
    size_t mlen;
    int rc;

    if (argc < 3) {
        fprintf(stderr, "usage: %s \"message\" out.wav [--none|--rep3|--conv]\n", argv[0]);
        fprintf(stderr, "  FEC defaults to conv (K=7 soft-Viterbi); --none disables coding\n");
        return 2;
    }
    msg  = argv[1];
    path = argv[2];

    hydra_profile_default(&p);                 /* fec_mode defaults to CONV */
    if (argc >= 4) {
        if      (strcmp(argv[3], "--none") == 0) p.fec_mode = HYDRA_FEC_NONE;
        else if (strcmp(argv[3], "--rep3") == 0) p.fec_mode = HYDRA_FEC_REP3;
        else if (strcmp(argv[3], "--conv") == 0) p.fec_mode = HYDRA_FEC_CONV;
    }
    if (hydra_profile_init(&p) != 0) { fprintf(stderr, "bad profile\n"); return 2; }

    memset(payload, 0, sizeof payload);
    mlen = strlen(msg);
    if (mlen > HYDRA_DCF_BYTES) mlen = HYDRA_DCF_BYTES;
    memcpy(payload, msg, mlen);

    rc = hydra_modem_tx(&p, payload, &audio, &n);
    if (rc != HYDRA_OK) { fprintf(stderr, "TX failed (%d)\n", rc); return 1; }

    if (hydra_wav_write(path, audio, n, (int)p.sample_rate) != 0) {
        fprintf(stderr, "could not write %s\n", path); free(audio); return 1;
    }
    printf("wrote %s: %zu samples, %.1f ms, %.0f baud %d-FSK, FEC=%s, interleave=%s\n",
           path, n, 1000.0 * (double)n / p.sample_rate, p.baud, p.n_tones,
           p.fec_mode == HYDRA_FEC_NONE ? "none" :
           p.fec_mode == HYDRA_FEC_REP3 ? "rep3" : "conv",
           p.interleave ? "on" : "off");
    free(audio);
    return 0;
}
