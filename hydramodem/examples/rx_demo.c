/* examples/rx_demo.c -- decode a Punctim acoustic frame from a WAV file.
 *
 *   ./rx_demo in.wav [--fec]
 *
 * Prints the recovered 17-byte DCF payload (as text + hex) or the failure
 * reason. Use --fec if the frame was encoded with repetition-3 FEC.
 */
#include "../src/hydra_modem.h"
#include "../src/wav.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *status_str(int rc)
{
    switch (rc) {
        case HYDRA_OK:            return "OK";
        case HYDRA_ERR_ARG:       return "bad argument";
        case HYDRA_ERR_ALLOC:     return "out of memory";
        case HYDRA_ERR_NO_SIGNAL: return "no signal (energy below threshold)";
        case HYDRA_ERR_NO_SYNC:   return "sync word not found";
        case HYDRA_ERR_CRC:       return "CRC mismatch (frame corrupt)";
        default:                  return "unknown";
    }
}

int main(int argc, char **argv)
{
    hydra_profile p;
    uint8_t payload[HYDRA_DCF_BYTES];
    float  *audio = NULL; size_t n = 0; int sr = 0;
    hydra_rx_diag d;
    int rc, i;

    if (argc < 2) { fprintf(stderr, "usage: %s in.wav [--none|--rep3|--conv]  (must match TX FEC)\n", argv[0]); return 2; }

    hydra_profile_default(&p);                 /* must match the TX profile */
    if (argc >= 3) {
        if      (strcmp(argv[2], "--none") == 0) p.fec_mode = HYDRA_FEC_NONE;
        else if (strcmp(argv[2], "--rep3") == 0) p.fec_mode = HYDRA_FEC_REP3;
        else if (strcmp(argv[2], "--conv") == 0) p.fec_mode = HYDRA_FEC_CONV;
    }
    if (hydra_profile_init(&p) != 0) { fprintf(stderr, "bad profile\n"); return 2; }

    if (hydra_wav_read(argv[1], &audio, &n, &sr) != 0) {
        fprintf(stderr, "could not read %s\n", argv[1]); return 1;
    }
    if (sr != (int)p.sample_rate)
        fprintf(stderr, "warning: WAV is %d Hz, profile is %.0f Hz\n", sr, p.sample_rate);

    rc = hydra_modem_rx_ex(&p, audio, n, payload, &d);
    free(audio);

    printf("sync %d/%d, grid origin %ld, peak energy %.3f\n",
           d.sync_score, (int)HYDRA_SYNC_BITS, d.frame_origin, (double)d.peak_energy);

    if (rc != HYDRA_OK) { printf("decode FAILED: %s\n", status_str(rc)); return 1; }

    printf("payload (text): \"");
    for (i = 0; i < (int)HYDRA_DCF_BYTES; ++i)
        putchar((payload[i] >= 32 && payload[i] < 127) ? payload[i] : '.');
    printf("\"\npayload (hex) :");
    for (i = 0; i < (int)HYDRA_DCF_BYTES; ++i) printf(" %02X", payload[i]);
    printf("\nCRC OK\n");
    return 0;
}
