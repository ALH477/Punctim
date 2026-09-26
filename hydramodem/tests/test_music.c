/* tests/test_music.c -- the musical tone-table profiles (hydra_profile_music).
 *
 *   [1] theory     -- every tone is an integer harmonic of the baud (so the
 *                     scale is just intonation AND orthogonal), symbols are
 *                     Gray-mapped (pitch-adjacent notes differ in one bit), and
 *                     the preamble alternates tonic/octave (fifth for 2/4 tones).
 *   [2] drone      -- each accompaniment partial leaks nothing into any data
 *                     correlator, at arbitrary (unaligned) window offsets.
 *   [3] envelope   -- the attack ramp removes the onset click; linear profiles
 *                     still refuse drones.
 *   [4] link       -- clean loopback for every scale, AWGN, clock offset, and the
 *                     streaming receiver.
 *   [5] polyphony  -- the duet: two frames in one burst (melody + bass voices),
 *                     each decoded with its own profile; cross-voice leakage,
 *                     AWGN, clock offset, and hydra_poly_check's refusals.
 *
 * Margins are set well inside what was measured (docs/MUSIC.md), so these are
 * regressions, not flaky thresholds.
 */
#include "../src/hydra_modem.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static int g_fail = 0;
#define CHECK(cond, msg) do { \
    if (cond) printf("  ok   %s\n", msg); \
    else    { printf("  FAIL %s\n", msg); ++g_fail; } } while (0)

static uint64_t rng = 0xC0FFEE11ULL;
static double u(void){ rng^=rng<<13; rng^=rng>>7; rng^=rng<<17; return (double)(rng>>11)/9007199254740992.0; }
static double gs(void){ double a=u(),b=u(); if(a<1e-12)a=1e-12; return sqrt(-2*log(a))*cos(2*M_PI*b); }
static double sigp(const float*x,size_t n){ double a=0; size_t i,c=0;
    for(i=0;i<n;i++){ if(fabsf(x[i])>1e-6f){ a+=(double)x[i]*x[i]; ++c; } }
    return c?a/c:0; }
static void awgn(float*x,size_t n,double snr){ double sd=sqrt(sigp(x,n)/pow(10,snr/10));
    size_t i; for(i=0;i<n;i++) x[i]+=(float)(sd*gs()); }
static void rand_payload(uint8_t *p){ int i; for(i=0;i<17;i++) p[i]=(uint8_t)(u()*256); }

static float *clock_resample(const float *in, size_t n, double ppm, size_t *out_n)
{
    double r = 1.0 + ppm * 1e-6;
    size_t m = (size_t)((double)(n - 1) / r), j;
    float *out = (float *)malloc(m * sizeof *out);
    for (j = 0; out && j < m; ++j) {
        double t = (double)j * r; size_t i0 = (size_t)t; double f = t - (double)i0;
        out[j] = (float)(in[i0] + f * (in[i0 + 1] - in[i0]));
    }
    *out_n = out ? m : 0;
    return out;
}

static int popcount(unsigned v){ int c=0; while(v){ c+=v&1u; v>>=1; } return c; }

static int roundtrip(const hydra_profile *p, double snr, double ppm)
{
    uint8_t tx[17], rx[17]; float *a = NULL, *b; size_t n = 0, m; int ok;
    rand_payload(tx);
    if (hydra_modem_tx(p, tx, &a, &n) != HYDRA_OK) return 0;
    if (snr < 99) awgn(a, n, snr);
    b = a; m = n;
    if (ppm != 0.0) b = clock_resample(a, n, ppm, &m);
    ok = b && hydra_modem_rx(p, b, m, rx) == HYDRA_OK && !memcmp(tx, rx, 17);
    if (b != a) free(b);
    free(a);
    return ok;
}

typedef struct { const char *name; hydra_profile p; } named;

/* ------------------------------------------------------------ [1] theory -- */
static void test_theory(named *ps, int np)
{
    char msg[160]; int q, k, j;
    printf("[1] tone table: harmonic series, Gray map, preamble interval\n");
    for (q = 0; q < np; ++q) {
        const hydra_profile *p = &ps[q].p;
        int N = p->n_tones, harmonic = 1, distinct = 1, gray1 = 1;
        int order[HYDRA_MUSIC_MAX_TONES];
        for (k = 0; k < N; ++k) {
            double f = hydra_tone_freq(p, k), cyc = f * p->samples_per_symbol / p->sample_rate;
            if (fabs(cyc - floor(cyc + 0.5)) > 1e-9) harmonic = 0;
            for (j = 0; j < k; ++j) if (p->tone_mult[j] == p->tone_mult[k]) distinct = 0;
            order[k] = k;
        }
        for (k = 1; k < N; ++k)             /* sort symbols by pitch */
            for (j = k; j > 0 && p->tone_mult[order[j]] < p->tone_mult[order[j-1]]; --j)
                { int t = order[j]; order[j] = order[j-1]; order[j-1] = t; }
        for (k = 1; k < N; ++k)
            if (popcount((unsigned)(order[k] ^ order[k-1])) != 1) gray1 = 0;
        snprintf(msg, sizeof msg, "%-9s every tone is a whole number of cycles per symbol", ps[q].name);
        CHECK(harmonic && distinct, msg);
        snprintf(msg, sizeof msg, "%-9s pitch-adjacent notes differ in exactly one bit", ps[q].name);
        CHECK(gray1, msg);
        snprintf(msg, sizeof msg, "%-9s symbol 0 is the tonic (lowest note)", ps[q].name);
        CHECK(order[0] == 0, msg);
        {
            /* preamble alternates symbols 0 and N-1 */
            int lo = p->tone_mult[0], hi = p->tone_mult[N - 1];
            int octave = (hi == 2 * lo || hi == 4 * lo), fifth = (2 * hi == 3 * lo);
            snprintf(msg, sizeof msg, "%-9s preamble = %s tremolo (%d:%d)", ps[q].name,
                     octave ? (hi == 4 * lo ? "two-octave" : "octave") : fifth ? "fifth" : "??", lo, hi);
            CHECK(octave || fifth, msg);   /* octave(s) for 8/16 tones and bass, a fifth for 2/4 */
        }
    }
}

/* ------------------------------------------------------------- [2] drone -- */
static void test_drone(named *ps, int np)
{
    char msg[160]; int q, k, t, trial;
    printf("[2] drone partials are orthogonal to every data correlator (any window)\n");
    for (q = 0; q < np; ++q) {
        const hydra_profile *p = &ps[q].p;
        int L = p->samples_per_symbol, nd = 0;
        double worst = 0.0;
        for (k = 0; k < HYDRA_MUSIC_MAX_DRONES; ++k) {
            double fd;
            if (p->drone_mult[k] <= 0) continue;
            ++nd; fd = p->drone_mult[k] * p->baud;
            for (t = 0; t < p->n_tones; ++t) {
                double ft = hydra_tone_freq(p, t);
                for (trial = 0; trial < 8; ++trial) {
                    /* a unit data tone integrates to |.| = L/2; measure the drone's
                     * share relative to that at a random start sample and phase */
                    long a0 = (long)(u() * 50000.0); double ph = u() * 2 * M_PI;
                    double I = 0, Q = 0; int n;
                    for (n = 0; n < L; ++n) {
                        double x = sin(2 * M_PI * fd * (double)(a0 + n) / p->sample_rate + ph);
                        double w = 2 * M_PI * ft * (double)n / p->sample_rate;
                        I += x * cos(w); Q += x * sin(w);
                    }
                    { double lk = sqrt(I * I + Q * Q) / (L / 2.0); if (lk > worst) worst = lk; }
                }
            }
        }
        if (!nd) continue;
        snprintf(msg, sizeof msg, "%-9s %d drone partial(s), worst leakage %.1e of a data tone",
                 ps[q].name, nd, worst);
        CHECK(worst < 1e-6, msg);
    }
}

/* ---------------------------------------------------------- [3] envelope -- */
static void test_envelope(void)
{
    hydra_profile p, lin; uint8_t tx[17]; float *a = NULL; size_t n = 0, i, first = 0;
    double onset_step = 0.0;
    printf("[3] envelope and profile guards\n");
    hydra_profile_melody(&p); hydra_profile_init(&p);
    rand_payload(tx);
    if (hydra_modem_tx(&p, tx, &a, &n) == HYDRA_OK) {
        while (first < n && a[first] == 0.0f) ++first;
        for (i = first; i < first + 64 && i + 1 < n; ++i) {
            double d = fabs((double)a[i + 1] - (double)a[i]);
            if (d > onset_step) onset_step = d;
        }
        free(a);
    }
    /* an unramped sine at 600 Hz steps by up to 0.9*2*pi*600/48000 ~ 0.07 */
    CHECK(first > 0 && onset_step < 0.01,
          "melody onset rises from silence (max step < 0.01 over first 64 samples)");

    hydra_profile_default(&lin);
    CHECK(lin.tone_mult[0] == 0 && lin.drone_mult[0] == 0 && lin.ramp_ms == 0.0,
          "default profile keeps the linear map, no drone, no ramp");
    lin.drone_mult[0] = 3; lin.drone_gain = 0.1;
    CHECK(hydra_profile_init(&lin) != 0, "linear profile refuses a drone (orthogonality not guaranteed)");
    {
        hydra_profile bad; hydra_profile_music(&bad, HYDRA_SCALE_MAJOR_PENT, 7.0, 600.0, 0);
        CHECK(hydra_profile_init(&bad) != 0, "baud that does not divide the sample rate is refused");
    }
    {
        hydra_profile bad; hydra_profile_melody(&bad); bad.drone_mult[0] = bad.tone_mult[0];
        CHECK(hydra_profile_init(&bad) != 0, "drone colliding with a data tone is refused");
    }
}

/* -------------------------------------------------------------- [4] link -- */
typedef struct { int got; uint8_t last[17]; } sink;
static void on_frame(const uint8_t payload[HYDRA_DCF_BYTES], const hydra_rx_diag *d, void *user)
{
    sink *s = (sink *)user; (void)d;
    memcpy(s->last, payload, 17); ++s->got;
}

static void test_link(named *ps, int np)
{
    char msg[160]; int q, t, ok;
    printf("[4] link: clean / AWGN / clock offset / streaming\n");
    for (q = 0; q < np; ++q) {
        for (ok = 0, t = 0; t < 4; ++t) ok += roundtrip(&ps[q].p, 100, 0);
        snprintf(msg, sizeof msg, "%-9s clean loopback %d/4 (%.2f s/frame)", ps[q].name, ok,
                 (double)ps[q].p.total_syms / ps[q].p.baud);
        CHECK(ok == 4, msg);
    }
    /* measured knee ~ -22 dB (wideband, 48 kHz) for melody; test 4 dB inside it */
    for (ok = 0, t = 0; t < 8; ++t) ok += roundtrip(&ps[0].p, -18.0, 0);
    snprintf(msg, sizeof msg, "melody    AWGN -18 dB wideband SNR, drone on: %d/8", ok);
    CHECK(ok >= 7, msg);
    /* measured: +/-5000 ppm decodes; test +/-3000 */
    ok = roundtrip(&ps[0].p, 100, 3000) + roundtrip(&ps[0].p, 100, -3000);
    snprintf(msg, sizeof msg, "melody    clock offset +/-3000 ppm: %d/2", ok);
    CHECK(ok == 2, msg);
    {
        const hydra_profile *p = &ps[1].p;   /* chime: shortest frame */
        uint8_t tx[17]; float *a = NULL; size_t n = 0, off;
        sink s; hydra_rx *rx;
        memset(&s, 0, sizeof s);
        rand_payload(tx);
        rx = hydra_rx_create(p, on_frame, &s);
        if (rx && hydra_modem_tx(p, tx, &a, &n) == HYDRA_OK) {
            for (off = 0; off < n; off += 1024)
                hydra_rx_push(rx, a + off, (n - off < 1024) ? n - off : 1024);
            {   float z[4096]; memset(z, 0, sizeof z); hydra_rx_push(rx, z, 4096); }
        }
        CHECK(s.got == 1 && !memcmp(s.last, tx, 17), "chime     streaming RX recovers the frame");
        free(a); hydra_rx_destroy(rx);
    }
}

/* --------------------------------------------------------- [5] polyphony -- */
static int duet_roundtrip(const hydra_profile *v, const hydra_profile *rx, double snr,
                          double ppm, int ok_out[2])
{
    uint8_t f[2][17], o[2][17]; float *a = NULL, *b; size_t n = 0, m; int st[2] = { -1, -1 };
    rand_payload(f[0]); rand_payload(f[1]);
    if (hydra_modem_tx_poly(v, 2, f, &a, &n) != HYDRA_OK) return -1;
    if (snr < 99) awgn(a, n, snr);
    b = a; m = n;
    if (ppm != 0.0) b = clock_resample(a, n, ppm, &m);
    if (b) hydra_modem_rx_poly(rx, 2, b, m, o, st);
    ok_out[0] += (st[0] == HYDRA_OK && !memcmp(o[0], f[0], 17));
    ok_out[1] += (st[1] == HYDRA_OK && !memcmp(o[1], f[1], 17));
    if (b != a) free(b);
    free(a);
    return 0;
}

static void test_poly(void)
{
    hydra_profile v[2], rx[2], bad[2];
    char msg[160]; int t, ok[2];
    printf("[5] polyphony: the duet (melody + bass, one frame each, one burst)\n");
    hydra_profile_duet(v);
    hydra_profile_init(&v[0]); hydra_profile_init(&v[1]);
    /* the receivers are the plain single-voice profiles: drone and gain are TX-only */
    hydra_profile_melody(&rx[0]); hydra_profile_bass(&rx[1]);
    hydra_profile_init(&rx[0]); hydra_profile_init(&rx[1]);
    CHECK(hydra_poly_check(v, 2) == 0, "duet passes hydra_poly_check");
    CHECK(v[0].tx_gain + v[1].tx_gain <= 0.9 + 1e-12, "duet peak stays <= 0.9");

    bad[0] = rx[0]; bad[1] = rx[0]; bad[0].tx_gain = bad[1].tx_gain = 0.4;
    CHECK(hydra_poly_check(bad, 2) != 0, "two voices on the same tones are refused");
    bad[0] = v[0]; bad[1] = v[1]; bad[1].tx_gain = 0.6;
    CHECK(hydra_poly_check(bad, 2) != 0, "gains summing past 1.0 (clipping) are refused");
    bad[0] = v[0]; bad[1] = v[1]; bad[1].baud = 50.0; hydra_profile_init(&bad[1]);
    CHECK(hydra_poly_check(bad, 2) != 0, "voices at different bauds are refused");

    /* each voice's correlators see nothing of the other, on the symbol grid */
    {
        uint8_t f[1][17]; float *b = NULL; size_t m = 0, st; double worst[2] = { 0, 0 }; int who, k, j;
        for (who = 0; who < 2; ++who) {
            const hydra_profile *other = &v[1 - who], *mine = &v[who];
            rand_payload(f[0]);
            if (hydra_modem_tx_poly(other, 1, f, &b, &m) != HYDRA_OK) continue;
            for (st = 1440; st + 1920 <= m; st += 1920)
                for (k = 0; k < mine->n_tones; ++k) {
                    double I = 0, Q = 0, fr = mine->tone_mult[k] * mine->baud;
                    for (j = 0; j < 1920; ++j) {
                        double w = 2 * M_PI * fr * (double)j / mine->sample_rate;
                        I += b[st + (size_t)j] * cos(w); Q += b[st + (size_t)j] * sin(w);
                    }
                    { double lk = sqrt(I * I + Q * Q) / 960.0; if (lk > worst[who]) worst[who] = lk; }
                }
            free(b); b = NULL;
        }
        snprintf(msg, sizeof msg, "cross-voice leakage: bass->melody %.1e, melody->bass %.1e of a unit tone",
                 worst[0], worst[1]);
        CHECK(worst[0] < 1e-6 && worst[1] < 1e-6, msg);
    }

    ok[0] = ok[1] = 0;
    for (t = 0; t < 3; ++t) duet_roundtrip(v, rx, 100, 0, ok);
    snprintf(msg, sizeof msg, "duet clean: melody %d/3, bass %d/3 (%.2f s for 34 bytes)", ok[0], ok[1],
             (double)(v[1].total_syms * 1920 + 2 * 1440) / 48000.0);
    CHECK(ok[0] == 3 && ok[1] == 3, msg);
    /* measured knees in the duet: melody ~-21 dB, bass ~-20 dB (wideband, whole-burst power) */
    ok[0] = ok[1] = 0;
    for (t = 0; t < 6; ++t) duet_roundtrip(v, rx, -16.0, 0, ok);
    snprintf(msg, sizeof msg, "duet AWGN -16 dB: melody %d/6, bass %d/6", ok[0], ok[1]);
    CHECK(ok[0] >= 5 && ok[1] >= 5, msg);
    /* measured: bass holds +/-3000 ppm and fails +/-5000 (melody holds both) */
    ok[0] = ok[1] = 0;
    duet_roundtrip(v, rx, 100, 3000, ok); duet_roundtrip(v, rx, 100, -3000, ok);
    snprintf(msg, sizeof msg, "duet clock offset +/-3000 ppm: melody %d/2, bass %d/2", ok[0], ok[1]);
    CHECK(ok[0] == 2 && ok[1] == 2, msg);
}

int main(void)
{
    named ps[6];
    int np = 0, q;
    printf("== HydraModem musical profiles ==\n");
    ps[np].name = "melody";   hydra_profile_melody(&ps[np].p);   ++np;
    ps[np].name = "chime";    hydra_profile_chime(&ps[np].p);    ++np;
    ps[np].name = "nocturne"; hydra_profile_nocturne(&ps[np].p); ++np;
    ps[np].name = "pent16";   hydra_profile_music(&ps[np].p, HYDRA_SCALE_MAJOR_PENT16, 25.0, 300.0, 1); ++np;
    ps[np].name = "fifth";    hydra_profile_music(&ps[np].p, HYDRA_SCALE_FIFTH, 100.0, 400.0, 1); ++np;
    ps[np].name = "bass";     hydra_profile_bass(&ps[np].p);     ++np;
    for (q = 0; q < np; ++q) {
        char msg[96];
        snprintf(msg, sizeof msg, "%-9s profile initialises", ps[q].name);
        CHECK(hydra_profile_init(&ps[q].p) == 0, msg);
    }
    test_theory(ps, np);
    test_drone(ps, np);
    test_envelope();
    test_link(ps, np);
    test_poly();
    printf(g_fail ? "MUSIC TESTS FAILED (%d)\n" : "ALL MUSIC TESTS PASSED (%d failures)\n", g_fail);
    return g_fail ? 1 : 0;
}
