/* hydra_modem.h -- end-to-end acoustic transport for Punctim DCF frames.
 *
 * TX  : 17-byte DCF payload -> mono float audio (one-shot).
 * RX  : audio -> 17-byte payload, via an I/Q matched-filter (integrate-and-dump)
 *       receiver with symbol-timing recovery and soft-decision FEC.
 *
 * Two RX entry points:
 *   - one-shot   (hydra_modem_rx*)  : decode a whole captured buffer.
 *   - streaming  (hydra_rx_*)       : push audio blocks as they arrive from the
 *                                     sound card; a callback fires per frame.
 *                                     Bounded memory, no whole-record storage.
 */
#ifndef HYDRA_MODEM_H
#define HYDRA_MODEM_H

#include "hydra_profile.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    HYDRA_OK            =  0,
    HYDRA_ERR_ARG       = -1,
    HYDRA_ERR_ALLOC     = -2,
    HYDRA_ERR_NO_SIGNAL = -3,   /* never crossed the energy threshold      */
    HYDRA_ERR_NO_SYNC   = -4,   /* sync word not found within tolerance    */
    HYDRA_ERR_CRC       = -5    /* framed OK but payload CRC failed         */
} hydra_status;

/* Diagnostics (pass NULL if not wanted). */
typedef struct {
    long   frame_origin;   /* sample index of the recovered symbol-grid origin */
    int    sync_score;     /* matching sync bits (max = HYDRA_SYNC_BITS)        */
    float  peak_energy;    /* peak per-symbol tone energy seen                  */
    double est_sps;        /* tracked samples/symbol after timing recovery      */
    double clock_ppm;      /* implied TX/RX sample-clock offset, parts/million  */
} hydra_rx_diag;

const char *hydra_strerror(int status);

/* ------------------------------- TRANSMIT -------------------------------- */
/* Allocates *audio_out (caller free()) holding *nsamp_out mono float samples in
 * [-1,1]: lead silence + modulated frame + tail silence. The FEC mode and
 * interleaving come from the profile. Returns HYDRA_OK or a negative status. */
int hydra_modem_tx(const hydra_profile *p,
                   const uint8_t payload[HYDRA_DCF_BYTES],
                   float **audio_out, size_t *nsamp_out);

/* ---------------------------- RECEIVE (one-shot) ------------------------- */
int hydra_modem_rx(const hydra_profile *p,
                   const float *audio, size_t nsamp,
                   uint8_t payload_out[HYDRA_DCF_BYTES]);

int hydra_modem_rx_ex(const hydra_profile *p,
                      const float *audio, size_t nsamp,
                      uint8_t payload_out[HYDRA_DCF_BYTES],
                      hydra_rx_diag *diag);

/* ---- polyphony (hydra_profile.h: hydra_profile_duet, hydra_poly_check) ----
 * TX: one frame per voice, summed into one burst; voices must pass
 * hydra_poly_check (initialised musical profiles, one baud, disjoint tones,
 * gains summing to <= 1). Shorter voices enter later by whole symbols so all
 * end together. Caller frees *audio_out.
 * RX: decodes each voice from the same audio with its own profile; returns the
 * number of voices recovered (status_out[v], if given, is each one's
 * hydra_status), or -1 on bad arguments. */
int hydra_modem_tx_poly(const hydra_profile *voices, int nvoices,
                        const uint8_t payloads[][HYDRA_DCF_BYTES],
                        float **audio_out, size_t *nsamp_out);
int hydra_modem_rx_poly(const hydra_profile *voices, int nvoices,
                        const float *audio, size_t nsamp,
                        uint8_t payloads_out[][HYDRA_DCF_BYTES], int status_out[]);

/* ---------------------------- RECEIVE (streaming) ------------------------ */
typedef struct hydra_rx hydra_rx;

/* Called once per successfully decoded (CRC-valid) frame. payload is the
 * 17-byte DCF frame; do not retain the pointer past the callback. */
typedef void (*hydra_frame_cb)(const uint8_t payload[HYDRA_DCF_BYTES],
                               const hydra_rx_diag *diag, void *user);

hydra_rx *hydra_rx_create(const hydra_profile *p, hydra_frame_cb cb, void *user);
void      hydra_rx_destroy(hydra_rx *rx);

/* Feed `n` mono float samples. May invoke the callback zero or more times.
 * Returns the number of frames decoded during this call (>=0), or <0 on error. */
int       hydra_rx_push(hydra_rx *rx, const float *samples, size_t n);

/* Drop any in-progress acquisition state (e.g. after a stream discontinuity). */
void      hydra_rx_reset(hydra_rx *rx);

#ifdef __cplusplus
}
#endif
#endif
