/* tests/test_loopback.c -- end-to-end validation of the Punctim acoustic modem.
 *
 *   [1] clean loopback : TX a 17-byte DCF payload -> audio -> RX, exact match.
 *   [2] AWGN sweep     : white Gaussian noise vs frame success, for all three
 *                        FEC modes (none / rep3 / conv) so the soft-Viterbi
 *                        coding gain is visible.
 *   [3] clock offset   : resample RX audio to emulate an independent device's
 *                        sample clock; show symbol-timing recovery still decodes
 *                        where open-loop timing would have drifted off the grid.
 *
 * Links against the portable reference DSP (hydra_dsp_ref.c).
 */
#include "../src/hydra_modem.h"
#include "../src/hydra_crc.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static uint64_t g_rng = 0x1234567890abcdefULL;
static double urand(void)
{
    g_rng ^= g_rng << 13; g_rng ^= g_rng >> 7; g_rng ^= g_rng << 17;
    return ((double)(g_rng >> 11)) / 9007199254740992.0;
}
static double grand(void)
{
    double u1 = urand(), u2 = urand();
    if (u1 < 1e-12) u1 = 1e-12;
    return sqrt(-2.0 * log(u1)) * cos(2.0 * M_PI * u2);
}

static double sig_power(const float *x, size_t n)
{
    double acc = 0.0; size_t i, c = 0;
    for (i = 0; i < n; ++i) if (x[i] != 0.0f) { acc += (double)x[i]*x[i]; ++c; }
    return (c > 0) ? acc / (double)c : 0.0;
}
static void add_awgn(float *x, size_t n, double snr_db, double sigpow)
{
    double sigma = sqrt(sigpow / pow(10.0, snr_db / 10.0));
    size_t i;
    for (i = 0; i < n; ++i) x[i] += (float)(sigma * grand());
}

/* linear-interpolation resampler: emulate a receiver clock off by `ppm`.
 * Output is the same signal observed on a clock scaled by (1+ppm/1e6). */
static float *clock_resample(const float *in, size_t n, double ppm, size_t *out_n)
{
    double r = 1.0 + ppm * 1e-6;             /* output sample step in input samples */
    size_t m = (size_t)((double)n / r);
    float *out = (float *)malloc(m * sizeof *out);
    size_t j;
    if (!out) { *out_n = 0; return NULL; }
    for (j = 0; j < m; ++j) {
        double t = (double)j * r;
        size_t i0 = (size_t)t;
        double f = t - (double)i0;
        double a = in[i0];
        double b = (i0 + 1 < n) ? in[i0 + 1] : in[i0];
        out[j] = (float)(a + f * (b - a));
    }
    *out_n = m;
    return out;
}

static void make_payload(uint8_t p[HYDRA_DCF_BYTES])
{
    memcpy(p, "HYDRA-DCF-17BYTE!", HYDRA_DCF_BYTES);
}

static const char *fec_name(hydra_fec_mode m)
{
    return m == HYDRA_FEC_NONE ? "none"
         : m == HYDRA_FEC_REP3 ? "rep3"
         : m == HYDRA_FEC_CONV ? "conv(K=7 soft Viterbi)" : "?";
}

static int run_clean(const hydra_profile *p)
{
    uint8_t tx[HYDRA_DCF_BYTES], rx[HYDRA_DCF_BYTES];
    float  *audio = NULL; size_t n = 0;
    hydra_rx_diag d; int rc;

    make_payload(tx);
    rc = hydra_modem_tx(p, tx, &audio, &n);
    if (rc != HYDRA_OK) { printf("  TX failed (%s)\n", hydra_strerror(rc)); return 1; }

    rc = hydra_modem_rx_ex(p, audio, n, rx, &d);
    printf("  audio: %zu samples (%.1f ms)  sync=%d/%d  origin=%ld  peakE=%.3f  est_sps=%.3f\n",
           n, 1000.0 * (double)n / p->sample_rate,
           d.sync_score, (int)HYDRA_SYNC_BITS, d.frame_origin,
           (double)d.peak_energy, d.est_sps);
    if (rc != HYDRA_OK) { printf("  RX failed (%s)\n", hydra_strerror(rc)); free(audio); return 1; }
    if (memcmp(tx, rx, HYDRA_DCF_BYTES) != 0) { printf("  PAYLOAD MISMATCH\n"); free(audio); return 1; }
    printf("  clean loopback OK  payload=\"%.*s\"\n", (int)HYDRA_DCF_BYTES, (const char*)rx);
    free(audio);
    return 0;
}

static void run_awgn_sweep(hydra_fec_mode mode)
{
    const double snrs[] = { 24, 18, 12, 9, 6, 3, 0, -3 };
    const int    TRIALS = 150;
    hydra_profile p; size_t si;

    hydra_profile_default(&p);
    p.fec_mode = mode;
    hydra_profile_init(&p);

    printf("\n  FEC=%s  (%d frames/point, %zu syms/frame):\n",
           fec_name(mode), TRIALS, p.total_syms);
    printf("    SNR(dB) | frame success\n    --------+--------------\n");
    for (si = 0; si < sizeof snrs / sizeof snrs[0]; ++si) {
        int ok = 0, t;
        for (t = 0; t < TRIALS; ++t) {
            uint8_t tx[HYDRA_DCF_BYTES], rx[HYDRA_DCF_BYTES];
            float  *audio = NULL; size_t n = 0; double sp; int j;
            for (j = 0; j < (int)HYDRA_DCF_BYTES; ++j) tx[j] = (uint8_t)(urand() * 256.0);
            if (hydra_modem_tx(&p, tx, &audio, &n) != HYDRA_OK) continue;
            sp = sig_power(audio, n);
            add_awgn(audio, n, snrs[si], sp);
            if (hydra_modem_rx(&p, audio, n, rx) == HYDRA_OK &&
                memcmp(tx, rx, HYDRA_DCF_BYTES) == 0) ++ok;
            free(audio);
        }
        printf("    %6.0f  |  %3d/%d  (%.0f%%)\n", snrs[si], ok, TRIALS, 100.0*ok/TRIALS);
    }
}

static void run_clock_offset(void)
{
    const double ppms[] = { 0, 200, 500, 1000, 2000, -1000, -2000 };
    hydra_profile p; size_t i;

    hydra_profile_default(&p);          /* CONV default */
    hydra_profile_init(&p);

    printf("\n[3] sample-clock offset tolerance (timing recovery), FEC=%s\n",
           fec_name(p.fec_mode));
    printf("    body = %zu syms x %d = %zu samples; open-loop drift at 2000ppm ~ %.1f symbols\n",
           p.total_syms, p.samples_per_symbol, p.total_syms*(size_t)p.samples_per_symbol,
           (double)(p.total_syms*(size_t)p.samples_per_symbol)*2000e-6/p.samples_per_symbol);
    printf("    ppm   | decode | recovered est_sps (nominal %d) | implied ppm\n", p.samples_per_symbol);
    printf("    ------+--------+--------------------------------+------------\n");
    for (i = 0; i < sizeof ppms / sizeof ppms[0]; ++i) {
        uint8_t tx[HYDRA_DCF_BYTES], rx[HYDRA_DCF_BYTES];
        float *audio = NULL, *res = NULL; size_t n = 0, m = 0;
        hydra_rx_diag d; int rc; int match;
        make_payload(tx);
        hydra_modem_tx(&p, tx, &audio, &n);
        res = clock_resample(audio, n, ppms[i], &m);
        rc  = hydra_modem_rx_ex(&p, res, m, rx, &d);
        match = (rc == HYDRA_OK) && (memcmp(tx, rx, HYDRA_DCF_BYTES) == 0);
        printf("    %5.0f |  %-5s |            %8.3f            | %+8.0f\n",
               ppms[i], match ? "OK" : "FAIL", d.est_sps, d.clock_ppm);
        free(audio); free(res);
    }
}

int main(void)
{
    hydra_profile p; int rc;

    hydra_profile_default(&p);
    if (hydra_profile_init(&p) != 0) { printf("bad profile\n"); return 2; }

    printf("Punctim acoustic modem -- production validation\n");
    printf("profile: %.0f Hz, %.0f baud, %d-FSK @ %.0f + k*%.0f Hz, %d samp/sym, "
           "%zu syms/frame, FEC=%s, interleave=%s\n",
           p.sample_rate, p.baud, p.n_tones, p.base_freq, p.tone_spacing,
           p.samples_per_symbol, p.total_syms, fec_name(p.fec_mode),
           p.interleave ? "on" : "off");

    printf("\n[1] clean loopback (default profile)\n");
    rc = run_clean(&p);

    printf("\n[2] AWGN sweep -- FEC mode comparison\n");
    run_awgn_sweep(HYDRA_FEC_NONE);
    run_awgn_sweep(HYDRA_FEC_REP3);
    run_awgn_sweep(HYDRA_FEC_CONV);

    run_clock_offset();

    printf("\n%s\n", rc == 0 ? "RESULT: clean loopback PASSED" : "RESULT: clean loopback FAILED");
    return rc;
}
