/* hydramodem.h -- single public header for the HydraModem acoustic modem.
 *
 * HydraModem transports the 17-byte Punctim/DCF wire frame over sound: a
 * Faust-generated (or portable-C) DSP front end does the continuous per-sample
 * work (quadrature tone generation and down-conversion), and the C core does
 * everything packet-shaped -- framing, FEC, interleaving, acquisition, symbol-
 * timing recovery and soft-decision decoding.
 *
 *   #include <hydramodem/hydramodem.h>
 *
 * Typical transmit:
 *   hydra_profile p; hydra_profile_default(&p); hydra_profile_init(&p);
 *   float *audio; size_t n;
 *   hydra_modem_tx(&p, payload17, &audio, &n);   // -> mono float PCM at p.sample_rate
 *   ... play / write audio ...; free(audio);
 *
 * Typical receive (one-shot over a captured buffer):
 *   uint8_t out[17]; hydra_rx_diag d;
 *   if (hydra_modem_rx_ex(&p, audio, n, out, &d) == HYDRA_OK) { ... }
 *
 * Typical receive (streaming from a live audio callback):
 *   hydra_rx *rx = hydra_rx_create(&p, on_frame, user);
 *   ... hydra_rx_push(rx, block, block_len);  // call from the audio thread ...
 *   hydra_rx_destroy(rx);
 *
 * All buffers are caller-managed; the library allocates only transient working
 * memory bounded by one frame. Everything is scalar double/float -- no SIMD or
 * vector-extension dependency (targets SiFive U74 / RV64GC as happily as x86).
 *
 * Thread-safety: the library holds no global mutable state. The one-shot
 * functions (hydra_modem_tx / _rx / _rx_ex) are reentrant and may be called
 * concurrently. A hydra_rx handle owns its own state and is NOT internally
 * locked -- use one handle per stream, or serialize access to a shared handle.
 */
#ifndef HYDRAMODEM_H
#define HYDRAMODEM_H

#define HYDRAMODEM_VERSION_MAJOR 2
#define HYDRAMODEM_VERSION_MINOR 0
#define HYDRAMODEM_VERSION_PATCH 0
#define HYDRAMODEM_VERSION       "2.0.0"

/* numeric form for comparisons: (major*10000 + minor*100 + patch) */
#define HYDRAMODEM_VERSION_NUMBER \
    (HYDRAMODEM_VERSION_MAJOR * 10000 + HYDRAMODEM_VERSION_MINOR * 100 + HYDRAMODEM_VERSION_PATCH)

#include "hydra_profile.h"   /* link parameters, FEC mode, frame sizing      */
#include "hydra_modem.h"     /* tx / rx one-shot + streaming, status, diag   */
#include "wav.h"             /* optional 16-bit mono WAV read/write helpers   */

#ifdef __cplusplus
extern "C" {
#endif

/* Returns the library version string, e.g. "2.0.0" (matches HYDRAMODEM_VERSION
 * at build time; useful when linked as a shared object). */
const char *hydramodem_version(void);

#ifdef __cplusplus
}
#endif

#endif /* HYDRAMODEM_H */
