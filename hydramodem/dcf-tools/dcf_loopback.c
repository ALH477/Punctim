// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/dcf_loopback.c -- Punctim<->HydraModem interop check.
 *
 * Proves the contract that lets HydraModem live in this monorepo: a real 17-byte
 * DeModFrame, built by the repo's reference wire codec (codec/demod_frame.h),
 * survives HydraModem TX -> RX byte-exact and still passes the DCF wire CRC, for
 * every FEC mode. HydraModem is a transport *beneath* the wire quantum -- it
 * never parses the frame -- so this leaves the 246-vector wire certificate
 * untouched; it only confirms the 17 opaque bytes are carried faithfully.
 *
 * Repo glue (DeMoD LLC, LGPL-3.0).
 *
 *   cc -std=gnu11 dcf_loopback.c ../build/libhydramodem.a -lm -o dcf_loopback
 */
#include "../src/hydramodem.h"
#include "../src/hydra_crc.h"
#include "../../codec/demod_frame.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int roundtrip(hydra_fec_mode fec, const uint8_t frame[DCF_FRAME_SIZE])
{
    hydra_profile p;
    hydra_profile_default(&p);
    p.fec_mode = fec;
    if (hydra_profile_init(&p) != 0) { fprintf(stderr, "bad profile\n"); return 1; }

    const char *name = fec == HYDRA_FEC_NONE ? "none"
                     : fec == HYDRA_FEC_REP3 ? "rep3" : "conv";

    float *audio = NULL; size_t n = 0;
    if (hydra_modem_tx(&p, frame, &audio, &n) != HYDRA_OK) {
        printf("  FEC=%-4s : TX FAILED\n", name); return 1;
    }
    uint8_t out[DCF_FRAME_SIZE]; hydra_rx_diag d;
    int rc = hydra_modem_rx_ex(&p, audio, n, out, &d);
    free(audio);

    if (rc != HYDRA_OK) {
        printf("  FEC=%-4s : RX FAILED (%s)\n", name, hydra_strerror(rc)); return 1;
    }
    if (memcmp(out, frame, DCF_FRAME_SIZE) != 0) {
        printf("  FEC=%-4s : payload MISMATCH\n", name); return 1;
    }
    if (!dcf_frame_valid(out)) {
        printf("  FEC=%-4s : recovered bytes fail the DCF wire CRC\n", name); return 1;
    }
    printf("  FEC=%-4s : OK  byte-exact, wire CRC valid, sync %d/%d, clock %.0f ppm\n",
           name, d.sync_score, (int)HYDRA_SYNC_BITS, d.clock_ppm);
    return 0;
}

int main(void)
{
    int fail = 0;

    /* The modem CRC and the repo wire CRC are the same CRC-16/CCITT-FALSE; the
     * "123456789" -> 0x29B1 anchor is the certificate's CRC anchor. */
    if (hydra_crc16_ccitt((const uint8_t *)"123456789", 9) != 0x29B1u) {
        printf("CRC anchor MISMATCH (hydramodem)\n"); fail = 1;
    }
    if (dcf_crc16((const uint8_t *)"123456789", 9) != 0x29B1u) {
        printf("CRC anchor MISMATCH (codec)\n"); fail = 1;
    }

    /* Build a real, valid DeModFrame with the reference codec. */
    dcf_frame_t f;
    dcf_frame_init(&f, 1, DCF_TYPE_DATA, 0x1234, 0x00A1, DCF_BROADCAST);
    f.payload[0] = 0xDE; f.payload[1] = 0xAD; f.payload[2] = 0xBE; f.payload[3] = 0xEF;
    f.timestamp_us = 0x0A1B2Cu;
    uint8_t frame[DCF_FRAME_SIZE];
    dcf_frame_encode(&f, frame);

    printf("DCF frame: ");
    for (int i = 0; i < (int)DCF_FRAME_SIZE; ++i) printf("%02X", frame[i]);
    printf("  (CRC anchor 0x29B1 OK)\n");
    printf("HydraModem TX->RX interop (default profile: 48kHz, 1000 baud, 2-FSK):\n");

    fail |= roundtrip(HYDRA_FEC_NONE, frame);
    fail |= roundtrip(HYDRA_FEC_REP3, frame);
    fail |= roundtrip(HYDRA_FEC_CONV, frame);

    if (fail) { printf("INTEROP FAILED\n"); return 1; }
    printf("INTEROP PASSED -- HydraModem carries the 17-byte DeModFrame faithfully\n");
    return 0;
}
