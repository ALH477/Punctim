// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/false_frame.c -- FALSE-FRAME measurement for the HydraModem receivers.
 * Repo glue (DeMoD LLC, LGPL-3.0). Results: docs/RECEIVER.md, "False frames".
 *
 * A false frame is a CRC-valid 17-byte payload that the receiver outputs when no
 * intact frame of that profile was sent, or whose bytes differ from every frame
 * that was sent. For a mesh a manufactured frame is worse than a lost one, so
 * this counts them, per profile x receiver path x input class, next to how often
 * the receiver got far enough to be at risk:
 *
 *   attempts : decode_window runs (one-shot: calls long enough to decode;
 *              streaming: windows the energy segmenter handed to the decoder)
 *   sync     : attempts that passed acquisition and reached Viterbi + CRC
 *   crc_fail : sync hits whose HydraModem CRC-16 failed
 *   legit    : CRC-valid outputs equal to a frame that was actually sent
 *   false    : CRC-valid outputs that are not (the metric)
 *   false_gate : false frames that ALSO pass the DeModFrame gate (sync 0xD3,
 *              version nibble 1, CRC-16/CCITT-FALSE over bytes 0..14 in 15..16)
 *
 * The library is not modified. To see inside decode_window without changing it,
 * this file compiles src/hydra_modem.c textually with two EXTERNAL calls
 * renamed to counting wrappers: hydra_rx_dsp_process (called exactly once per
 * decode_window that has enough samples) and hydra_frame_decode_soft (called
 * exactly once per window that passed acquisition). Every other line of the
 * receiver is the library's own. The `codec` rows call hydra_frame_decode_soft
 * directly (no audio) for volume.
 *
 * Deterministic: every row has a fixed seed (xoshiro256**, Box-Muller); the same
 * command prints the same TSV.
 *
 *   false_frame [--scale X] [--profile P[,P..]] [--path oneshot|stream|codec]
 *               [--class C[,C..]] [--selftest]
 *   (no args = everything at scale 1; TSV on stdout, one row per cell)
 */
#include "../src/hydra_profile.h"
#include "../src/hydra_frame.h"
#include "../src/hydra_dsp.h"
#include "../src/hydra_crc.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

/* ---- counting wrappers around the two external calls inside decode_window ---- */
static long g_attempts, g_sync, g_crcfail;
static void ff_rx_dsp_process(hydra_rx_dsp *d, const float *a, float *iq, int n)
{ ++g_attempts; hydra_rx_dsp_process(d, a, iq, n); }
static int ff_decode_soft(const hydra_profile *p, const float *s, uint8_t *out)
{ int r; ++g_sync; r = hydra_frame_decode_soft(p, s, out); if (r != 0) ++g_crcfail; return r; }
#define hydra_rx_dsp_process    ff_rx_dsp_process
#define hydra_frame_decode_soft ff_decode_soft
#include "../src/hydra_modem.c"     /* the receiver, verbatim */
#undef hydra_rx_dsp_process
#undef hydra_frame_decode_soft

/* ------------------------------------------------------------------ PRNG */
typedef struct { uint64_t s[4]; int have; double spare; } rng_t;
static uint64_t splitmix(uint64_t *x)
{ uint64_t z = (*x += 0x9E3779B97F4A7C15ull); z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull; return z ^ (z >> 31); }
static void rng_seed(rng_t *r, uint64_t seed)
{ int i; for (i = 0; i < 4; ++i) r->s[i] = splitmix(&seed); r->have = 0; }
static uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
static uint64_t rng_u64(rng_t *r)
{ uint64_t *s = r->s, res = rotl(s[1] * 5, 7) * 9, t = s[1] << 17;
  s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3]; s[2] ^= t; s[3] = rotl(s[3], 45); return res; }
static double rng_unif(rng_t *r) { return (double)(rng_u64(r) >> 11) * (1.0 / 9007199254740992.0); }
static long rng_int(rng_t *r, long n) { return (long)(rng_unif(r) * (double)n); }
static double rng_gauss(rng_t *r)
{ double u, v, m; if (r->have) { r->have = 0; return r->spare; }
  do { u = 2 * rng_unif(r) - 1; v = 2 * rng_unif(r) - 1; m = u * u + v * v; } while (m >= 1 || m == 0);
  m = sqrt(-2 * log(m) / m); r->spare = v * m; r->have = 1; return u * m; }
static uint64_t hash_str(const char *s, uint64_t h)
{ while (*s) { h ^= (uint8_t)*s++; h *= 0x100000001B3ull; } return h; }

static float clip1(double x) { return (float)(x > 1.0 ? 1.0 : x < -1.0 ? -1.0 : x); }

/* ---------------------------------------------------------- DeModFrame gate */
static int demod_gate(const uint8_t f[17])
{
    return f[0] == 0xD3 && (f[1] >> 4) == 1 &&
           hydra_crc16_ccitt(f, 15) == (uint16_t)((f[15] << 8) | f[16]);
}

/* ------------------------------------------------------------------ profiles */
typedef struct { const char *name; void (*fn)(hydra_profile *); } prof_def;
static const prof_def k_profiles[] = {
    { "default", hydra_profile_default }, { "aux", hydra_profile_aux_cable },
    { "melody", hydra_profile_melody }, { "bass", hydra_profile_bass },
    { "chime", hydra_profile_chime }, { "nocturne", hydra_profile_nocturne },
};
static int get_profile(const char *name, hydra_profile *p)
{
    size_t i;
    for (i = 0; i < sizeof k_profiles / sizeof k_profiles[0]; ++i)
        if (!strcmp(name, k_profiles[i].name)) {
            k_profiles[i].fn(p); if (hydra_profile_init(p) != 0) return -1; return 0;
        }
    return -1;
}

/* Render an arbitrary symbol stream exactly as hydra_modem_tx renders a frame's
 * (same lead/tail, same synthesis). Checked against hydra_modem_tx in selftest. */
static float *render_syms(const hydra_profile *p, const uint8_t *sym, size_t nsym, size_t *n_out)
{
    size_t spp = (size_t)p->samples_per_symbol, ramp, lead, body, total, i;
    float *audio;
    ramp = (size_t)(p->ramp_ms * 1e-3 * p->sample_rate + 0.5);
    lead = (size_t)(0.02 * p->sample_rate) + ramp;
    body = nsym * spp; total = lead + body + lead;
    audio = (float *)calloc(total, sizeof *audio);
    if (p->tone_mult[0] > 0) {
        music_render(p, sym, nsym, ramp, audio + lead - ramp);
    } else {
        float *freq = (float *)malloc(body * sizeof *freq);
        hydra_tx_dsp *tx = hydra_tx_dsp_create(p->sample_rate);
        long s;
        for (s = 0; s < (long)nsym; ++s) {
            double f = hydra_tone_freq(p, sym[s]);
            for (i = 0; i < spp; ++i) freq[(size_t)s * spp + i] = (float)f;
        }
        hydra_tx_dsp_process(tx, freq, audio + lead, (int)body);
        for (i = 0; i < body; ++i) audio[lead + i] *= (float)p->tx_gain;
        hydra_tx_dsp_destroy(tx); free(freq);
    }
    *n_out = total;
    return audio;
}
static size_t tx_len(const hydra_profile *p)
{
    size_t ramp = (size_t)(p->ramp_ms * 1e-3 * p->sample_rate + 0.5);
    size_t lead = (size_t)(0.02 * p->sample_rate) + ramp;
    return 2 * lead + p->total_syms * (size_t)p->samples_per_symbol;
}
/* zero-pad a buffer to at least `want` samples (a one-shot window must hold a frame) */
static float *pad_to(float *a, size_t *n, size_t want)
{
    size_t i;
    if (*n >= want) return a;
    a = (float *)realloc(a, want * sizeof *a);
    for (i = *n; i < want; ++i) a[i] = 0.0f;
    *n = want;
    return a;
}
static void rand_payload(rng_t *r, uint8_t pl[17]) { int i; for (i = 0; i < 17; ++i) pl[i] = (uint8_t)rng_u64(r); }

/* ======================= stationary generators (continuous) ================= */
enum { G_SILENCE, G_WGN, G_PINK, G_MUSIC_OFF, G_MUSIC_ON, G_TRILL, G_CLICKS };
typedef struct {
    int kind; double level; rng_t r; const hydra_profile *p;
    /* pink */
    double b0, b1, b2, b3, b4, b5, b6;
    /* music: up to 3 voices, phrase/rest structure */
    struct { double f, ph, amp; long left, age, len; int active; } v[3];
    long phrase_left, rest_left;
    /* on-grid: symbol sequencer */
    long sym_left; int cur_tone; double gph; int trill_left, trill_state;
    /* clicks */
    double click_env, click_amp; long click_len, click_i;
} gen_t;

static void gen_init(gen_t *g, int kind, double level_dbfs, uint64_t seed, const hydra_profile *p)
{
    memset(g, 0, sizeof *g);
    g->kind = kind; g->level = pow(10.0, level_dbfs / 20.0); g->p = p;
    rng_seed(&g->r, seed);
}

/* An off-grid musical pitch: 12-TET 110..1760 Hz, +/-30 cents, fundamental not
 * within 5% of a bin of the profile's baud (so its partials do not sit on the
 * receiver's correlator grid by construction). */
static double offgrid_pitch(gen_t *g)
{
    for (;;) {
        double f = 110.0 * pow(2.0, (double)rng_int(&g->r, 49) / 12.0 + (rng_unif(&g->r) - 0.5) * 0.05);
        double x = f / g->p->baud;
        if (fabs(x - floor(x + 0.5)) > 0.05) return f;
    }
}

/* phrase length: 0.5 s + U * max(3 s, 2 frames), so phrases can outlast one
 * frame (the streaming segmenter only decodes a burst at least a frame long) */
static double phrase_s(gen_t *g)
{
    double fr = (double)g->p->total_syms / g->p->baud;
    return 0.5 + rng_unif(&g->r) * (2.0 * fr > 3.0 ? 2.0 * fr : 3.0);
}

static double gen_music_off(gen_t *g, double fs)
{
    int k; double y = 0.0;
    if (g->rest_left > 0) { --g->rest_left; return 0.0; }
    if (g->phrase_left <= 0) {           /* new phrase 0.5..3 s, then rest 0.2..1 s */
        g->phrase_left = (long)(phrase_s(g) * fs);
        g->rest_left   = 0;
        for (k = 0; k < 3; ++k) g->v[k].active = 0;
    }
    if (--g->phrase_left == 0) { g->rest_left = (long)((0.2 + 0.8 * rng_unif(&g->r)) * fs); }
    for (k = 0; k < 3; ++k) {
        if (!g->v[k].active) {
            if (rng_unif(&g->r) < 20.0 / fs) {   /* ~20 onsets/s/voice when idle */
                g->v[k].active = 1; g->v[k].f = offgrid_pitch(g); g->v[k].ph = 0;
                g->v[k].len = (long)((0.08 + 0.52 * rng_unif(&g->r)) * fs);
                g->v[k].age = 0; g->v[k].amp = 0.12 + 0.1 * rng_unif(&g->r);
            }
            continue;
        }
        {
            double t = (double)g->v[k].age, env, s = 0.0; int h;
            double att = 0.01 * fs, rel = 0.03 * fs, L = (double)g->v[k].len;
            env = t < att ? t / att : (L - t < rel ? (L - t) / rel : 1.0);
            for (h = 1; h <= 4; ++h) s += sin(2 * M_PI * g->v[k].ph * h) / h;
            y += g->v[k].amp * env * s;
            g->v[k].ph += g->v[k].f / fs; g->v[k].ph -= floor(g->v[k].ph);
            if (++g->v[k].age >= g->v[k].len) g->v[k].active = 0;
        }
    }
    return y;
}

/* On-grid music: notes drawn from the profile's OWN tone set, continuous phase.
 * G_MUSIC_ON: note lengths 1..4 whole symbols, symbol-aligned (the adversarial
 * FSK lookalike). G_TRILL: each phrase opens with a tonic/top-tone trill at the
 * symbol rate (the preamble's own pattern) of 4..preamble+4 symbols, then
 * random own-tone notes. Phrases 0.5 s + U*max(3 s, 2 frames), rests 0.2..1 s. */
static double gen_music_on(gen_t *g, double fs)
{
    const hydra_profile *p = g->p; long L = p->samples_per_symbol; double y;
    if (g->rest_left > 0) { --g->rest_left; return 0.0; }
    if (g->phrase_left <= 0) {
        g->phrase_left = (long)(phrase_s(g) * fs / (double)L) * L + L;
        g->sym_left = 0; g->gph = 0;
        g->trill_left = (g->kind == G_TRILL) ? 4 + (int)rng_int(&g->r, p->preamble_syms + 1) : 0;
        g->trill_state = 0;
    }
    if (g->sym_left <= 0) {
        if (g->trill_left > 0) { g->cur_tone = g->trill_state ? p->n_tones - 1 : 0;
                                 g->trill_state ^= 1; --g->trill_left; g->sym_left = L; }
        else { g->cur_tone = (int)rng_int(&g->r, p->n_tones);
               g->sym_left = L * (g->kind == G_TRILL ? 1 : 1 + rng_int(&g->r, 4)); }
    }
    --g->sym_left;
    g->gph += hydra_tone_freq(p, g->cur_tone) / fs; g->gph -= floor(g->gph);
    y = 0.5 * sin(2 * M_PI * g->gph);
    if (--g->phrase_left == 0) g->rest_left = (long)((0.2 + 0.8 * rng_unif(&g->r)) * fs);
    return y;
}

static double gen_sample(gen_t *g, double fs)
{
    switch (g->kind) {
    case G_SILENCE: return 0.0;
    case G_WGN: return g->level * rng_gauss(&g->r);
    case G_PINK: {   /* Paul Kellet's refined filter; ~unit-RMS scaled below */
        double w = rng_gauss(&g->r), y;
        g->b0 = 0.99886 * g->b0 + w * 0.0555179; g->b1 = 0.99332 * g->b1 + w * 0.0750759;
        g->b2 = 0.96900 * g->b2 + w * 0.1538520; g->b3 = 0.86650 * g->b3 + w * 0.3104856;
        g->b4 = 0.55000 * g->b4 + w * 0.5329522; g->b5 = -0.7616 * g->b5 - w * 0.0168980;
        y = g->b0 + g->b1 + g->b2 + g->b3 + g->b4 + g->b5 + g->b6 + w * 0.5362;
        g->b6 = w * 0.115926;
        return g->level * y / 3.0;   /* the filter's RMS gain is ~3 for unit white */
    }
    case G_MUSIC_OFF: return gen_music_off(g, fs);
    case G_MUSIC_ON: case G_TRILL: return gen_music_on(g, fs);
    case G_CLICKS: {   /* Poisson 5 clicks/s over a -70 dBFS floor; each a 1-sample
                        * impulse or a 0.2..3 ms decaying noise burst, amp 0.1..1 */
        double y = 3.16e-4 * rng_gauss(&g->r);
        if (g->click_i < g->click_len) {
            y += g->click_amp * exp(-5.0 * (double)g->click_i / (double)g->click_len) * rng_gauss(&g->r);
            ++g->click_i;
        } else if (rng_unif(&g->r) < 5.0 / fs) {
            g->click_amp = 0.1 + 0.9 * rng_unif(&g->r);
            if (rng_unif(&g->r) < 0.5) { y += (rng_unif(&g->r) < 0.5 ? -1 : 1) * g->click_amp; g->click_len = 0; }
            else { g->click_len = (long)((0.0002 + 0.0028 * rng_unif(&g->r)) * fs); g->click_i = 0; }
        }
        return y;
    }
    }
    return 0.0;
}
static void gen_fill(gen_t *g, float *buf, size_t n, double fs)
{ size_t i; for (i = 0; i < n; ++i) buf[i] = clip1(gen_sample(g, fs)); }

/* ============================== result rows ================================ */
typedef struct {
    long trials, attempts, sync, crcfail, legit, fls, fls_gate, fls_d3, oneshot_ok;
    double audio_s;
} stats_t;

/* one-sided 95% upper bound on a Poisson mean given k observed */
static double pois_ub95(long k)
{
    double lo = 0, hi = 10.0 + 3.0 * (double)k;
    int it;
    for (it = 0; it < 200; ++it) {
        double mid = 0.5 * (lo + hi), term = exp(-mid), cdf = term; long j;
        for (j = 1; j <= k; ++j) { term *= mid / (double)j; cdf += term; }
        if (cdf > 0.05) lo = mid; else hi = mid;
    }
    return 0.5 * (lo + hi);
}

static void print_header(void)
{
    printf("profile\tpath\tclass\tseed\ttrials\taudio_h\tattempts\tsync\tcrc_fail\tlegit"
           "\tfalse\tfalse_gate\tfalse_d3\tub95_false_per_attempt\tub95_false_per_sync"
           "\tub95_false_per_hour\twall_s\n");
}
static void print_row(const char *prof, const char *path, const char *cls, uint64_t seed,
                      const stats_t *s, double wall)
{
    double ub = pois_ub95(s->fls);
    printf("%s\t%s\t%s\t%016llx\t%ld\t%.4f\t%ld\t%ld\t%ld\t%ld\t%ld\t%ld\t%ld\t%.3g\t%.3g\t%.3g\t%.1f\n",
           prof, path, cls, (unsigned long long)seed, s->trials, s->audio_s / 3600.0,
           s->attempts, s->sync, s->crcfail, s->legit, s->fls, s->fls_gate, s->fls_d3,
           s->attempts ? ub / (double)s->attempts : NAN,
           s->sync ? ub / (double)s->sync : NAN,
           s->audio_s > 0 ? ub / (s->audio_s / 3600.0) : NAN, wall);
    fflush(stdout);
}

/* classify one CRC-valid output against the frames legitimately sent */
static char g_label[96];
static void judge(stats_t *s, const uint8_t out[17], uint8_t legit[][17], int nlegit)
{
    int i;
    for (i = 0; i < nlegit; ++i) if (!memcmp(out, legit[i], 17)) { ++s->legit; return; }
    ++s->fls;
    if (out[0] == 0xD3) ++s->fls_d3;
    if (demod_gate(out)) ++s->fls_gate;
    fprintf(stderr, "FALSE FRAME %s: ", g_label);
    for (i = 0; i < 17; ++i) fprintf(stderr, "%02x", out[i]);
    fprintf(stderr, "\n");
}

/* streaming callback */
typedef struct { stats_t *s; uint8_t (*legit)[17]; int nlegit; } cb_ctx;
static void on_frame(const uint8_t payload[17], const hydra_rx_diag *d, void *user)
{ cb_ctx *c = (cb_ctx *)user; (void)d; judge(c->s, payload, c->legit, c->nlegit); }

/* ======================= structured (trial) classes ========================= */
enum { S_TRUNC, S_OVERLAP, S_WRONGPROF, S_BADSYNC, S_DATAFLIP, S_PREAMBLE, S_PRESYNC_RAND };

/* Build one trial's audio. legit[] receives the frames that were really sent
 * (intact, of this profile) -- an output equal to one of them is not false. */
static float *make_trial(int kind, const hydra_profile *p, const char *src_prof, rng_t *r,
                         long t, size_t *n_out, uint8_t legit[][17], int *nlegit)
{
    uint8_t pl[17], sym[4096];
    size_t nsym = 0, n = 0, i;
    float *a = NULL;
    *nlegit = 0;
    rand_payload(r, pl);
    switch (kind) {
    case S_TRUNC: {    /* a real frame cut at a fraction of its body, zero-padded */
        static const double cut[] = { 0.10, 0.25, 0.50, 0.75, 0.90, 0.97, 0.99 };
        size_t ramp = (size_t)(p->ramp_ms * 1e-3 * p->sample_rate + 0.5);
        size_t lead = (size_t)(0.02 * p->sample_rate) + ramp;
        size_t body = p->total_syms * (size_t)p->samples_per_symbol, keep;
        hydra_modem_tx(p, pl, &a, &n);
        keep = lead + (size_t)(cut[t % 7] * (double)body);
        for (i = keep; i < n; ++i) a[i] = 0.0f;
        memcpy(legit[(*nlegit)++], pl, 17);   /* a lucky correct decode is not false */
        break;
    }
    case S_OVERLAP: {  /* two frames at half amplitude, B delayed */
        static const double off[] = { 0.02, 0.10, 0.25, 0.50, 0.75, 0.90 };
        uint8_t pl2[17]; float *b = NULL; size_t nb, d, body = p->total_syms * (size_t)p->samples_per_symbol;
        rand_payload(r, pl2);
        hydra_modem_tx(p, pl, &a, &n); hydra_modem_tx(p, pl2, &b, &nb);
        d = (t % 7 == 6) ? (size_t)p->samples_per_symbol / 3 : (size_t)(off[t % 7] * (double)body);
        a = (float *)realloc(a, (n + d) * sizeof *a);
        for (i = n; i < n + d; ++i) a[i] = 0.0f;
        for (i = 0; i < n + d; ++i) a[i] = 0.5f * a[i] + (i >= d ? 0.5f * b[i - d] : 0.0f);
        n += d; free(b);
        memcpy(legit[(*nlegit)++], pl, 17); memcpy(legit[(*nlegit)++], pl2, 17);
        break;
    }
    case S_WRONGPROF: {  /* a real frame of ANOTHER profile: nothing legit */
        hydra_profile q;
        get_profile(src_prof, &q);
        hydra_modem_tx(&q, pl, &a, &n);
        a = pad_to(a, &n, tx_len(p));
        break;
    }
    case S_BADSYNC: {  /* flip k of the 16 sync bits, k = 1..8; data intact */
        uint8_t sb[HYDRA_SYNC_BITS]; int k = 1 + (int)(t % 8), j, b;
        hydra_frame_build(p, pl, sym, sizeof sym, &nsym);
        hydra_frame_sync_bits(p, sb);
        for (j = 0; j < k; ++j) {        /* k distinct positions */
            do b = (int)rng_int(r, HYDRA_SYNC_BITS); while (sb[b] & 2);
            sb[b] = (uint8_t)((sb[b] ^ 1) | 2);
        }
        for (j = 0; j < (int)HYDRA_SYNC_BITS; ++j) sb[j] &= 1;
        hydra_bits_to_symbols(sb, HYDRA_SYNC_BITS, p->bits_per_symbol, sym + p->preamble_syms);
        a = render_syms(p, sym, nsym, &n);
        memcpy(legit[(*nlegit)++], pl, 17);
        break;
    }
    case S_DATAFLIP: { /* flip n coded bits (as transmitted), near the Viterbi cliff */
        static const int nf[] = { 2, 4, 8, 12, 16, 20, 24, 28, 32, 40, 48, 64 };
        int k = nf[t % 12], j, bps = p->bits_per_symbol;
        size_t d0 = (size_t)p->preamble_syms + p->sync_syms;
        uint8_t used[4096];
        hydra_frame_build(p, pl, sym, sizeof sym, &nsym);
        memset(used, 0, sizeof used);
        for (j = 0; j < k; ++j) {
            long cb;
            do cb = rng_int(r, (long)p->coded_bits); while (used[cb]);
            used[cb] = 1;
            sym[d0 + (size_t)cb / (size_t)bps] ^= (uint8_t)(1u << (bps - 1 - (int)(cb % bps)));
        }
        a = render_syms(p, sym, nsym, &n);
        memcpy(legit[(*nlegit)++], pl, 17);
        break;
    }
    case S_PREAMBLE: { /* even t: preamble alone; odd t: preamble + sync; then silence */
        size_t m = (size_t)p->preamble_syms;
        hydra_frame_build(p, pl, sym, sizeof sym, &nsym);
        if (t & 1) m += p->sync_syms;
        a = render_syms(p, sym, m, &n);
        a = pad_to(a, &n, tx_len(p));
        break;
    }
    case S_PRESYNC_RAND: { /* correct preamble + sync, uniformly random data symbols */
        size_t d0 = (size_t)p->preamble_syms + p->sync_syms, k;
        hydra_frame_build(p, pl, sym, sizeof sym, &nsym);
        for (k = d0; k < nsym; ++k) sym[k] = (uint8_t)rng_int(r, p->n_tones);
        a = render_syms(p, sym, nsym, &n);
        break;
    }
    }
    *n_out = n;
    return a;
}

/* ================================ runners ================================== */
static const double FS = 48000.0;

static void run_stationary(const char *pname, const hydra_profile *p, int oneshot,
                           const char *cls, int kind, double level, double hours)
{
    uint64_t seed = hash_str(cls, hash_str(pname, hash_str(oneshot ? "oneshot" : "stream", 0xCBF29CE484222325ull)));
    gen_t g; stats_t s; size_t W = tx_len(p), i;
    float *buf = (float *)malloc(W * sizeof *buf);
    double target = hours * 3600.0 * FS, done = 0; clock_t c0 = clock();
    uint8_t out[17];
    memset(&s, 0, sizeof s);
    gen_init(&g, kind, level, seed, p);
    snprintf(g_label, sizeof g_label, "%s/%s/%s", pname, oneshot ? "oneshot" : "stream", cls);
    g_attempts = g_sync = g_crcfail = 0;
    if (oneshot) {
        /* one WAV-sized window per decode, as frame_rx decodes one file */
        while (done < target) {
            gen_fill(&g, buf, W, FS); done += (double)W; ++s.trials;
            if (hydra_modem_rx(p, buf, W, out) == HYDRA_OK) judge(&s, out, NULL, 0);
        }
    } else {
        cb_ctx c = { &s, NULL, 0 };
        hydra_rx *rx = hydra_rx_create(p, on_frame, &c);
        while (done < target) {
            gen_fill(&g, buf, W, FS); done += (double)W; ++s.trials;
            hydra_rx_push(rx, buf, W);
        }
        hydra_rx_destroy(rx);
    }
    (void)i;
    s.audio_s = done / FS; s.attempts = g_attempts; s.sync = g_sync; s.crcfail = g_crcfail;
    print_row(pname, oneshot ? "oneshot" : "stream", cls, seed, &s, (double)(clock() - c0) / CLOCKS_PER_SEC);
    free(buf);
}

static void run_structured(const char *pname, const hydra_profile *p, int oneshot,
                           const char *cls, int kind, const char *src, long trials)
{
    uint64_t seed = hash_str(cls, hash_str(pname, hash_str(oneshot ? "oneshot" : "stream", 0x84222325CBF29CE4ull)));
    rng_t r; stats_t s; long t; clock_t c0 = clock();
    uint8_t legit[4][17], out[17];
    int nlegit = 0;
    cb_ctx c = { &s, legit, 0 };
    hydra_rx *rx = NULL;
    size_t gap = (size_t)(0.25 * FS) + 8u * (size_t)p->samples_per_symbol;
    float *zeros = (float *)calloc(gap, sizeof *zeros);
    char label[64];
    if (src) seed = hash_str(src, seed);
    snprintf(g_label, sizeof g_label, "%s/%s/%s%s%s", pname, oneshot ? "oneshot" : "stream", cls,
             src ? "<-" : "", src ? src : "");
    rng_seed(&r, seed);
    memset(&s, 0, sizeof s);
    g_attempts = g_sync = g_crcfail = 0;
    if (!oneshot) rx = hydra_rx_create(p, on_frame, &c);
    for (t = 0; t < trials; ++t) {
        size_t n; float *a = make_trial(kind, p, src, &r, t, &n, legit, &nlegit);
        ++s.trials;
        if (oneshot) {
            s.audio_s += (double)n / FS;
            if (hydra_modem_rx(p, a, n, out) == HYDRA_OK) judge(&s, out, legit, nlegit);
        } else {
            c.nlegit = nlegit;
            hydra_rx_push(rx, a, n);
            hydra_rx_push(rx, zeros, gap);   /* flush: the silence rule closes the burst */
            s.audio_s += (double)(n + gap) / FS;
        }
        free(a);
    }
    if (rx) hydra_rx_destroy(rx);
    free(zeros);
    s.attempts = g_attempts; s.sync = g_sync; s.crcfail = g_crcfail;
    snprintf(label, sizeof label, src ? "%s<-%s" : "%s", cls, src ? src : "");
    print_row(pname, oneshot ? "oneshot" : "stream", label, seed, &s, (double)(clock() - c0) / CLOCKS_PER_SEC);
}

/* codec-only volume rows: soft metrics straight into deinterleave+Viterbi+CRC
 * (hydra_frame_decode_soft) -- what decode_window does after acquisition. */
static void run_codec(const hydra_profile *p, const char *cls, long trials)
{
    uint64_t seed = hash_str(cls, 0x5EEDC0DEC0DEull);
    rng_t r; stats_t s; long t; clock_t c0 = clock();
    size_t nc = p->coded_bits, k;
    float *soft = (float *)malloc(nc * sizeof *soft);
    uint8_t out[17], pl[17], sym[4096], legit[1][17];
    int flips = 0;
    rng_seed(&r, seed);
    memset(&s, 0, sizeof s);
    g_attempts = g_sync = g_crcfail = 0;
    if (!strncmp(cls, "flip", 4)) flips = atoi(cls + 4);
    snprintf(g_label, sizeof g_label, "codec/%s", cls);
    for (t = 0; t < trials; ++t) {
        int nlegit = 0;
        if (!strcmp(cls, "gauss")) {            /* soft ~ N(0,1): pure noise after sync */
            for (k = 0; k < nc; ++k) soft[k] = (float)rng_gauss(&r);
        } else if (!strcmp(cls, "hard")) {      /* random hard +/-1 */
            for (k = 0; k < nc; ++k) soft[k] = (rng_u64(&r) >> 63) ? 1.0f : -1.0f;
        } else {                                /* valid codeword, `flips` coded bits inverted */
            size_t nsym, d0 = (size_t)p->preamble_syms + p->sync_syms;
            uint8_t used[1024]; int j;
            rand_payload(&r, pl); memcpy(legit[nlegit++], pl, 17);
            hydra_frame_build(p, pl, sym, sizeof sym, &nsym);
            for (k = 0; k < nc; ++k) soft[k] = sym[d0 + k] ? 1.0f : -1.0f;   /* binary profile */
            memset(used, 0, sizeof used);
            for (j = 0; j < flips; ++j) { long cb; do cb = rng_int(&r, (long)nc); while (used[cb]);
                                          used[cb] = 1; soft[cb] = -soft[cb]; }
        }
        ++s.trials; ++g_attempts;
        if (ff_decode_soft(p, soft, out) == 0) judge(&s, out, legit, nlegit);
    }
    s.attempts = g_attempts; s.sync = g_sync; s.crcfail = g_crcfail;
    print_row("codec", "codec", cls, seed, &s, (double)(clock() - c0) / CLOCKS_PER_SEC);
    free(soft);
}

/* ================================ selftest ================================= */
static int selftest(void)
{
    const char *names[] = { "default", "aux", "melody", "bass", "chime" };
    int i, bad = 0; rng_t r; rng_seed(&r, 1);
    if (hydra_crc16_ccitt((const uint8_t *)"123456789", 9) != 0x29B1) { fprintf(stderr, "crc anchor\n"); bad = 1; }
    for (i = 0; i < 5; ++i) {
        hydra_profile p; uint8_t pl[17], sym[4096], out[17]; size_t nsym, n1, n2; float *a, *b;
        get_profile(names[i], &p); rand_payload(&r, pl);
        hydra_modem_tx(&p, pl, &a, &n1);
        hydra_frame_build(&p, pl, sym, sizeof sym, &nsym);
        b = render_syms(&p, sym, nsym, &n2);
        if (n1 != n2 || memcmp(a, b, n1 * sizeof *a)) { fprintf(stderr, "render != tx (%s)\n", names[i]); bad = 1; }
        if (hydra_modem_rx(&p, a, n1, out) != HYDRA_OK || memcmp(out, pl, 17)) { fprintf(stderr, "loopback %s\n", names[i]); bad = 1; }
        free(a); free(b);
    }
    /* CCITT-FALSE residue: a valid DeModFrame's 17 bytes always have CRC 0x0000, so
     * the HydraModem CRC field of a gate-passing payload is necessarily 0x0000. */
    for (i = 0; i < 100000; ++i) {
        uint8_t f[17]; uint16_t c; rand_payload(&r, f); f[0] = 0xD3; f[1] = (uint8_t)(0x10 | (f[1] & 15));
        c = hydra_crc16_ccitt(f, 15); f[15] = (uint8_t)(c >> 8); f[16] = (uint8_t)c;
        if (!demod_gate(f) || hydra_crc16_ccitt(f, 17) != 0) { fprintf(stderr, "residue\n"); bad = 1; break; }
    }
    fprintf(stderr, "selftest: %s (render==tx, loopback, CRC anchor 0x29B1, residue 0 on 1e5 frames)\n", bad ? "FAIL" : "ok");
    return bad;
}

/* ================================== main =================================== */
static int want(const char *list, const char *x)
{
    size_t n = strlen(x); const char *s = list;
    if (!list) return 1;
    while ((s = strstr(s, x)) != NULL) {
        if ((s == list || s[-1] == ',') && (s[n] == 0 || s[n] == ',')) return 1;
        s += n;
    }
    return 0;
}

int main(int argc, char **argv)
{
    double scale = 1.0, hours = 1.0, tscale = 1.0, cscale = 1.0; long presync = 0;
    const char *profs = NULL, *paths = NULL, *classes = NULL;
    int i, pi;
    for (i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--scale") && i + 1 < argc) scale = atof(argv[++i]);
        else if (!strcmp(argv[i], "--hours") && i + 1 < argc) hours = atof(argv[++i]);
        else if (!strcmp(argv[i], "--trials") && i + 1 < argc) tscale = atof(argv[++i]);
        else if (!strcmp(argv[i], "--codec") && i + 1 < argc) cscale = atof(argv[++i]);
        else if (!strcmp(argv[i], "--presync") && i + 1 < argc) presync = atol(argv[++i]);
        else if (!strcmp(argv[i], "--profile") && i + 1 < argc) profs = argv[++i];
        else if (!strcmp(argv[i], "--path") && i + 1 < argc) paths = argv[++i];
        else if (!strcmp(argv[i], "--class") && i + 1 < argc) classes = argv[++i];
        else if (!strcmp(argv[i], "--selftest")) return selftest();
        else { fprintf(stderr, "usage: %s [--scale X] [--hours H] [--trials X] [--codec X] [--presync N] [--profile P,..] [--path oneshot,stream,codec] [--class C,..] [--selftest]\n", argv[0]); return 2; }
    }
    if (selftest() != 0) return 1;
    print_header();

    if (want(paths, "codec")) {
        hydra_profile p; get_profile("default", &p);   /* codec is profile-independent: 316 coded bits */
        const char *cc[] = { "gauss", "hard", "flip8", "flip16", "flip20", "flip24", "flip28", "flip32", "flip48" };
        long nt[] = { 4000000, 2000000, 100000, 200000, 500000, 500000, 500000, 500000, 100000 };
        int j;
        for (j = 0; j < 9; ++j) if (want(classes, cc[j])) run_codec(&p, cc[j], (long)(nt[j] * scale * cscale));
    }

    for (pi = 0; pi < 5; ++pi) {
        static const char *pn[] = { "default", "aux", "melody", "bass", "chime" };
        /* wrong-profile sources for each profile under test */
        static const char *src[5][3] = { { "aux", "melody", NULL }, { "default", "bass", NULL },
                                         { "bass", "chime", "nocturne" }, { "melody", "chime", NULL },
                                         { "melody", "bass", NULL } };
        hydra_profile p; int op;
        double hscale = 1.0;
        if (!want(profs, pn[pi])) continue;
        get_profile(pn[pi], &p);
        for (op = 0; op < 2; ++op) {
            int oneshot = (op == 0); int j;
            struct { const char *n; int k; double lvl; double h; } st[] = {
                { "silence", G_SILENCE, 0, 0.5 }, { "wgn-40", G_WGN, -40, 1 }, { "wgn-20", G_WGN, -20, 1 },
                { "wgn-6", G_WGN, -6, 1 }, { "pink-20", G_PINK, -20, 1 }, { "music_offgrid", G_MUSIC_OFF, 0, 1 },
                { "music_ongrid", G_MUSIC_ON, 0, 1 }, { "trill_ongrid", G_TRILL, 0, 1 }, { "clicks", G_CLICKS, 0, 1 } };
            struct { const char *n; int k; long tr; } sc[] = {
                { "trunc", S_TRUNC, 700 }, { "overlap", S_OVERLAP, 700 }, { "badsync", S_BADSYNC, 800 },
                { "dataflip", S_DATAFLIP, 1200 }, { "preamble", S_PREAMBLE, 400 },
                { "presync_random", S_PRESYNC_RAND, 20000 } };
            if (!want(paths, oneshot ? "oneshot" : "stream")) continue;
            for (j = 0; j < 9; ++j)
                if (want(classes, st[j].n))
                    run_stationary(pn[pi], &p, oneshot, st[j].n, st[j].k, st[j].lvl, st[j].h * hscale * scale * hours);
            for (j = 0; j < 6; ++j)
                if (want(classes, sc[j].n)) {
                    long tr = (long)(sc[j].tr * scale * tscale);
                    if (pi >= 2) tr = tr / 4 > 0 ? tr / 4 : 1;   /* musical frames are ~10x longer */
                    if (sc[j].k == S_PRESYNC_RAND && presync > 0) tr = presync;
                    run_structured(pn[pi], &p, oneshot, sc[j].n, sc[j].k, NULL, tr);
                }
            if (want(classes, "wrongprof")) {
                int q;
                for (q = 0; q < 3 && src[pi][q]; ++q)
                    run_structured(pn[pi], &p, oneshot, "wrongprof", S_WRONGPROF, src[pi][q],
                                   (long)((pi >= 2 ? 100 : 400) * scale * tscale) > 0 ? (long)((pi >= 2 ? 100 : 400) * scale * tscale) : 1);
            }
        }
    }
    return 0;
}
