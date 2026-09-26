/* hydra_modem.c -- end-to-end acoustic modem.
 *
 * Receiver chain (the production design):
 *   acoustic samples
 *     -> Faust/ref DSP: per-tone quadrature down-conversion (I,Q)        [DSP]
 *     -> per-symbol integrate-and-dump on the recovered timing grid      [here]
 *        (the matched filter for a rectangular-envelope tone; rejects 2fc)
 *     -> non-coherent energy  E_k = (sum I)^2 + (sum Q)^2
 *     -> acquisition: match the full known prefix (preamble+sync) over the
 *        whole buffer to find the frame and symbol-grid origin             [here]
 *     -> symbol-timing tracking (early-late gate) to follow clock offset   [here]
 *     -> soft per-bit metrics (max-log over tones)                         [here]
 *     -> deinterleave + soft-Viterbi/rep3/none + CRC                       [frame]
 *
 * Integrate-and-dump is evaluated through prefix sums of the I/Q streams, so a
 * symbol energy is O(1) and a full-buffer acquisition scan is cheap.
 */
#include "hydra_modem.h"
#include "hydra_frame.h"
#include "hydra_dsp.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define HYDRA_MAXTONES 64

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* Symbol-timing loop tuning (see docs/RECEIVER.md). These were tuned jointly to
 * pass both the clock-offset sweep (>= +/-3000 ppm) and the AWGN sweep (100% to
 * -6 dB) at once; change them together, not in isolation.
 *   SEARCH : +/- samples the per-symbol energy-peak search spans.
 *   GATE   : a symbol updates timing only if its total-energy profile varies by
 *            more than this fraction (i.e. it straddles a real tone transition);
 *            same-tone runs are flat and carry no timing information.
 *   EMA    : smoothing of the per-symbol offset into the running drift estimate
 *            (noise averages to ~0, so the grid does not random-walk).
 *   TRACK  : fraction of the smoothed drift applied to the grid each symbol. */
#define HYDRA_TIMING_SEARCH 2
#define HYDRA_TIMING_GATE   0.15
#define HYDRA_TIMING_EMA    0.20
#define HYDRA_TIMING_TRACK  0.50

const char *hydra_strerror(int s)
{
    switch (s) {
        case HYDRA_OK:            return "ok";
        case HYDRA_ERR_ARG:       return "bad argument";
        case HYDRA_ERR_ALLOC:     return "out of memory";
        case HYDRA_ERR_NO_SIGNAL: return "no signal (energy below threshold)";
        case HYDRA_ERR_NO_SYNC:   return "sync word not found";
        case HYDRA_ERR_CRC:       return "CRC mismatch (frame corrupt)";
        default:                  return "unknown error";
    }
}

/* ============================== TRANSMIT ================================== */

/* ------------------------- musical synthesis (exact) -------------------------
 * A musical profile's tones and drone partials are integer harmonics of the
 * baud, so each completes a whole number of cycles in L = samples_per_symbol
 * samples: every phase the transmitter ever needs is k/L of a cycle for an
 * integer k. The musical path therefore keeps phase as an INTEGER counter
 * mod L (no accumulated f/fs rounding) and reads one quarter-wave sine table,
 * so the waveform is a pure function of that table -- which is what lets an
 * independent port (exsecutor/examples/hydramodem/melos*.exsc) reproduce the
 * WAV byte for byte. The raised-cosine ramp over R samples is cos(2 pi (2i+1)
 * / 4R), the same table at period 4R; with the presets' 10 ms ramp at 25 baud,
 * 4R == L and it is literally the same table.
 *
 * qsin(N, k) = sin(2 pi k / N), 0 <= k < N, N % 4 == 0: computed by libm only
 * on the first quarter (k <= N/4) and folded by symmetry elsewhere, so the
 * table is exactly odd and quarter-symmetric. */
static double qsin(long N, long k)
{
    long q = N / 4;
    if (k > 2 * q) return -qsin(N, k - 2 * q);
    if (k > q)     k = 2 * q - k;
    if (k == 0)    return 0.0;
    return sin((2.0 * M_PI * (double)k) / (double)N);
}

/* Render the span (pre-roll + body + post-roll) of a musical profile into
 * out[0 .. ramp+body+ramp). Order of the double operations is normative: the
 * port repeats it exactly. */
static void music_render(const hydra_profile *p, const uint8_t *symbols, size_t nsym,
                         size_t ramp, float *out)
{
    long   L = p->samples_per_symbol, R = (long)ramp, a = 0;
    long   b[HYDRA_MUSIC_MAX_DRONES], h[HYDRA_MUSIC_MAX_DRONES];
    int    k, nd = 0;
    size_t body = nsym * (size_t)L, span = ramp + body + ramp, i;
    double gd;

    for (k = 0; k < HYDRA_MUSIC_MAX_DRONES; ++k)
        if (p->drone_mult[k] > 0) { h[nd] = p->drone_mult[k]; b[nd] = 0; ++nd; }
    gd = p->tx_gain - (double)nd * p->drone_gain;

    for (i = 0; i < span; ++i) {
        size_t s = (i < ramp) ? 0
                 : (i >= ramp + body) ? nsym - 1
                 : (i - ramp) / (size_t)L;
        double v, e = 1.0;
        /* data carrier: phase advanced BEFORE the output (hydra_dsp_ref.c) */
        a += p->tone_mult[symbols[s]];
        if (a >= L) a -= L;
        v = gd * qsin(L, a);
        /* drones: sample i sits at phase h*i, walked */
        for (k = 0; k < nd; ++k) {
            v = v + p->drone_gain * qsin(L, b[k]);
            b[k] += h[k];
            if (b[k] >= L) b[k] -= L;
        }
        if (i < ramp)
            e = 0.5 - 0.5 * qsin(4 * R, (2 * (long)i + 1 + R) % (4 * R));
        else if (i >= ramp + body)
            e = 0.5 + 0.5 * qsin(4 * R, (2 * (long)(i - ramp - body) + 1 + R) % (4 * R));
        /* `+=`: a single voice renders into zeros, so this is (float)(e*v)
         * exactly; polyphony sums its voices here (float adds, voice order). */
        out[i] += (float)(e * v);
    }
}

int hydra_modem_tx(const hydra_profile *p,
                   const uint8_t payload[HYDRA_DCF_BYTES],
                   float **audio_out, size_t *nsamp_out)
{
    uint8_t      *symbols = NULL;
    float        *freq    = NULL;
    float        *audio   = NULL;
    hydra_tx_dsp *tx      = NULL;
    size_t        nsym = 0, spp, body, ramp, lead, tail, total, i;
    long          s;
    int           rc;

    if (!p || !payload || !audio_out || !nsamp_out) return HYDRA_ERR_ARG;

    spp  = (size_t)p->samples_per_symbol;
    /* Optional attack/release (musical profiles): the first tone is pre-rolled
     * and the last post-rolled for `ramp` samples under a raised cosine,
     * OUTSIDE the symbol body -- no data symbol is attenuated, and the
     * pre-roll is the same tone as preamble symbol 0, so acquisition is
     * unaffected. */
    ramp = (size_t)(p->ramp_ms * 1e-3 * p->sample_rate + 0.5);
    lead = (size_t)(0.02 * p->sample_rate) + ramp;   /* 20 ms silence each side */
    tail = lead;

    symbols = (uint8_t *)malloc(p->total_syms);
    if (!symbols) { rc = HYDRA_ERR_ALLOC; goto done; }

    rc = hydra_frame_build(p, payload, symbols, p->total_syms, &nsym);
    if (rc != 0 || nsym == 0) { rc = HYDRA_ERR_ARG; goto done; }

    body  = nsym * spp;
    total = lead + body + tail;
    audio = (float *)calloc(total, sizeof *audio);   /* guards start at 0 */
    if (!audio) { rc = HYDRA_ERR_ALLOC; goto done; }

    if (p->tone_mult[0] > 0) {
        /* Musical: exact integer-phase synthesis, drones and ramp included
         * (docs/MUSIC.md). The DSP backend is not used: its oscillator
         * accumulates f/fs in floating point, which is the drift this avoids. */
        music_render(p, symbols, nsym, ramp, audio + lead - ramp);
    } else {
        /* Linear: the historical path, unchanged (ramp_ms is 0 here unless a
         * caller set it, in which case the pre/post-roll is silence). */
        freq = (float *)malloc(body * sizeof *freq);
        if (!freq) { rc = HYDRA_ERR_ALLOC; goto done; }
        for (s = 0; s < (long)nsym; ++s) {
            double f = hydra_tone_freq(p, symbols[s]);
            for (i = 0; i < spp; ++i)
                freq[(size_t)s * spp + i] = (float)f;
        }
        tx = hydra_tx_dsp_create(p->sample_rate);
        if (!tx) { rc = HYDRA_ERR_ALLOC; goto done; }
        hydra_tx_dsp_process(tx, freq, audio + lead, (int)body);
        for (i = 0; i < body; ++i)
            audio[lead + i] *= (float)p->tx_gain;
    }

    *audio_out = audio;
    *nsamp_out = total;
    audio = NULL;
    rc = HYDRA_OK;

done:
    free(symbols);
    free(freq);
    free(audio);
    hydra_tx_dsp_destroy(tx);
    return rc;
}

/* ================================ POLYPHONY ================================ */

int hydra_modem_tx_poly(const hydra_profile *voices, int nvoices,
                        const uint8_t payloads[][HYDRA_DCF_BYTES],
                        float **audio_out, size_t *nsamp_out)
{
    uint8_t *symbols[HYDRA_POLY_MAX_VOICES] = { NULL };
    size_t   nsym[HYDRA_POLY_MAX_VOICES], body_max = 0, spp, ramp, lead, total;
    float   *audio = NULL;
    int      v, rc;

    if (!voices || !payloads || !audio_out || !nsamp_out) return HYDRA_ERR_ARG;
    if (hydra_poly_check(voices, nvoices) != 0) return HYDRA_ERR_ARG;

    spp  = (size_t)voices[0].samples_per_symbol;
    ramp = (size_t)(voices[0].ramp_ms * 1e-3 * voices[0].sample_rate + 0.5);
    lead = (size_t)(0.02 * voices[0].sample_rate) + ramp;   /* as hydra_modem_tx */

    for (v = 0; v < nvoices; ++v) {
        symbols[v] = (uint8_t *)malloc(voices[v].total_syms);
        if (!symbols[v]) { rc = HYDRA_ERR_ALLOC; goto done; }
        if (hydra_frame_build(&voices[v], payloads[v], symbols[v],
                              voices[v].total_syms, &nsym[v]) != 0 || nsym[v] == 0) {
            rc = HYDRA_ERR_ARG; goto done;
        }
        if (nsym[v] * spp > body_max) body_max = nsym[v] * spp;
    }
    total = lead + body_max + lead;
    audio = (float *)calloc(total, sizeof *audio);
    if (!audio) { rc = HYDRA_ERR_ALLOC; goto done; }

    /* Right-aligned: a shorter voice enters later by WHOLE symbols, so every
     * voice's symbol boundaries fall on one grid -- the condition for the
     * voices to be orthogonal -- and all of them end together. */
    for (v = 0; v < nvoices; ++v) {
        size_t offset = body_max - nsym[v] * spp;
        music_render(&voices[v], symbols[v], nsym[v], ramp, audio + lead + offset - ramp);
    }

    *audio_out = audio;
    *nsamp_out = total;
    audio = NULL;
    rc = HYDRA_OK;
done:
    for (v = 0; v < HYDRA_POLY_MAX_VOICES; ++v) free(symbols[v]);
    free(audio);
    return rc;
}

int hydra_modem_rx_poly(const hydra_profile *voices, int nvoices,
                        const float *audio, size_t nsamp,
                        uint8_t payloads_out[][HYDRA_DCF_BYTES], int status_out[])
{
    int v, ok = 0;
    if (!voices || !audio || !payloads_out || nvoices < 1 ||
        nvoices > HYDRA_POLY_MAX_VOICES) return -1;
    for (v = 0; v < nvoices; ++v) {
        int st = hydra_modem_rx(&voices[v], audio, nsamp, payloads_out[v]);
        if (status_out) status_out[v] = st;
        if (st == HYDRA_OK) ++ok;
    }
    return ok;
}

/* ===================== RECEIVE: integrate-and-dump core =================== */

/* Prefix sums of the per-tone I and Q streams: the integral over [a,b) is
 * P[b]-P[a], so any symbol's integrate-and-dump is O(1). Layout:
 *   PI[k*(nsamp+1) + n] = sum_{j<n} I_k[j]. */
typedef struct {
    int     N;
    long    nsamp;
    double *PI;
    double *PQ;
} iq_prefix;

static int prefix_build(iq_prefix *pf, const float *iq, int N, long nsamp)
{
    long n; int k;
    pf->N = N; pf->nsamp = nsamp;
    pf->PI = (double *)malloc((size_t)N * (size_t)(nsamp + 1) * sizeof(double));
    pf->PQ = (double *)malloc((size_t)N * (size_t)(nsamp + 1) * sizeof(double));
    if (!pf->PI || !pf->PQ) { free(pf->PI); free(pf->PQ); pf->PI = pf->PQ = NULL; return -1; }
    for (k = 0; k < N; ++k) {
        double sI = 0.0, sQ = 0.0;
        size_t base = (size_t)k * (size_t)(nsamp + 1);
        pf->PI[base] = 0.0; pf->PQ[base] = 0.0;
        for (n = 0; n < nsamp; ++n) {
            size_t idx = ((size_t)n * N + k) * 2;
            sI += iq[idx]; sQ += iq[idx + 1];
            pf->PI[base + (size_t)n + 1] = sI;
            pf->PQ[base + (size_t)n + 1] = sQ;
        }
    }
    return 0;
}

static void prefix_free(iq_prefix *pf) { free(pf->PI); free(pf->PQ); pf->PI = pf->PQ = NULL; }

static double pf_energy(const iq_prefix *pf, int k, long a, int len)
{
    size_t base = (size_t)k * (size_t)(pf->nsamp + 1);
    double I = pf->PI[base + (size_t)(a + len)] - pf->PI[base + (size_t)a];
    double Q = pf->PQ[base + (size_t)(a + len)] - pf->PQ[base + (size_t)a];
    return I * I + Q * Q;
}

static void pf_symbol_energies(const iq_prefix *pf, long a, int L, double *E)
{
    int k;
    for (k = 0; k < pf->N; ++k) E[k] = pf_energy(pf, k, a, L);
}

static int argmax_d(const double *E, int N)
{
    int k, best = 0;
    for (k = 1; k < N; ++k) if (E[k] > E[best]) best = k;
    return best;
}

/* soft metric for bit position b of a symbol (max-log over tones), amplitude
 * normalized to ~[-1,1]; sign>0 => bit 1. */
static float bit_soft(const double *E, int N, int bps, int b)
{
    double max1 = -1.0, max0 = -1.0;
    int t;
    for (t = 0; t < N; ++t) {
        int bit = (t >> (bps - 1 - b)) & 1;
        if (bit) { if (E[t] > max1) max1 = E[t]; }
        else     { if (E[t] > max0) max0 = E[t]; }
    }
    return (float)((max1 - max0) / (max1 + max0 + 1e-12));
}

/* trunc_out (optional, the streaming receiver's): on HYDRA_ERR_NO_SYNC, the
 * origin of a burst that starts in the window but runs off its end, or -1. */
static int decode_window(const hydra_profile *p, const float *audio, size_t nsamp,
                         uint8_t payload_out[HYDRA_DCF_BYTES], hydra_rx_diag *diag,
                         long *trunc_out)
{
    hydra_rx_dsp *rx = NULL;
    float    *iq = NULL, *soft = NULL;
    uint8_t   sync_bits[HYDRA_SYNC_BITS];
    uint8_t  *known = NULL;
    iq_prefix pf = {0, 0, NULL, NULL};
    double    E[HYDRA_MAXTONES];
    int       N, L, bps, rc, nknown;
    long      o, o_hi, best_o = -1, k;
    int       best_score = -1;
    double    peak = 0.0, pos, sps;
    size_t    need, j;

    if (trunc_out) *trunc_out = -1;
    if (!p || !audio || !payload_out) return HYDRA_ERR_ARG;
    N = p->n_tones; L = p->samples_per_symbol; bps = p->bits_per_symbol;
    if (N > HYDRA_MAXTONES) return HYDRA_ERR_ARG;

    need = p->total_syms * (size_t)L;
    if (nsamp < need) return HYDRA_ERR_NO_SIGNAL;

    iq = (float *)malloc(nsamp * (size_t)(2 * N) * sizeof *iq);
    if (!iq) { rc = HYDRA_ERR_ALLOC; goto done; }
    rx = hydra_rx_dsp_create(p);
    if (!rx) { rc = HYDRA_ERR_ALLOC; goto done; }
    hydra_rx_dsp_process(rx, audio, iq, (int)nsamp);

    if (prefix_build(&pf, iq, N, (long)nsamp) != 0) { rc = HYDRA_ERR_ALLOC; goto done; }
    free(iq); iq = NULL;

    /* known acquisition prefix: preamble symbols, then sync symbols. The sync
     * packing matches hydra_frame_build exactly (zero-padded last symbol when
     * bits_per_symbol does not divide HYDRA_SYNC_BITS, e.g. 8-FSK). */
    nknown = p->preamble_syms + (int)p->sync_syms;
    known  = (uint8_t *)malloc((size_t)nknown);
    if (!known) { rc = HYDRA_ERR_ALLOC; goto done; }
    for (k = 0; k < p->preamble_syms; ++k)
        known[k] = (k & 1) ? (uint8_t)(N - 1) : 0u;
    hydra_frame_sync_bits(p, sync_bits);
    hydra_bits_to_symbols(sync_bits, HYDRA_SYNC_BITS, bps,
                          known + p->preamble_syms);

    /* acquisition: scan every origin, match the full known prefix. 40 known
     * symbols make false alarm negligible -- robust to leading noise.
     * The match score is a PLATEAU (every origin within +/-L/2 of true detects
     * all preamble symbols), so we take the CENTRE of the best-scoring run, not
     * its leading edge -- otherwise the sampling phase is biased half a symbol
     * early and every data symbol is sampled off-centre. */
    o_hi = (long)nsamp - (long)(p->total_syms * (size_t)L);
    {
        long o_first = -1, o_last = -1;
        for (o = 0; o <= o_hi; ++o) {
            int score = 0;
            for (k = 0; k < nknown; ++k) {
                pf_symbol_energies(&pf, o + k * (long)L, L, E);
                if (argmax_d(E, N) == known[k]) ++score;
            }
            /* The plateau is the FIRST run of best-scoring origins: an origin
             * more than one symbol past its start belongs to another burst (a
             * streaming window may hold the next frame's prefix too, and both
             * then score in full). One burst's plateau is never wider than a
             * symbol, so for a single burst this is the old first..last. */
            if (score > best_score) { best_score = score; o_first = o; o_last = o; }
            else if (score == best_score && o <= o_first + (long)L) { o_last = o; }
        }
        best_o = (o_first + o_last) / 2;
    }
    if (best_o < 0) { rc = HYDRA_ERR_NO_SIGNAL; goto done; }

    /* Fine phase refinement: the match-count above is a plateau, so centring
     * still leaves up to a sample of static phase error -- and the rate loop
     * tracks drift, not a static offset, so it would oscillate trying to remove
     * it. Pin the phase precisely by maximizing the known-prefix energy (a sharp
     * peak, unlike the count) over a +/-L/2 window around the coarse origin. */
    {
        long od, od_best = 0; double best_e = -1.0;
        for (od = -L / 2; od <= L / 2; ++od) {
            long base = best_o + od; double e = 0.0; int ok = 1;
            if (base < 0) continue;
            for (k = 0; k < nknown; ++k) {
                long a = base + k * (long)L;
                if (a < 0 || a + L > (long)nsamp) { ok = 0; break; }
                e += pf_energy(&pf, known[k], a, L);
            }
            if (ok && e > best_e) { best_e = e; od_best = od; }
        }
        best_o += od_best;
    }

    { int kk; pf_symbol_energies(&pf, best_o, L, E);
      for (kk = 0; kk < N; ++kk) if (E[kk] > peak) peak = E[kk]; }
    if (diag) {
        diag->frame_origin = best_o;
        diag->sync_score   = best_score - p->preamble_syms;
        if (diag->sync_score < 0) diag->sync_score = 0;
        diag->peak_energy  = (float)peak;
    }
    if (best_score < nknown - 3) {
        /* No complete burst. For a streaming window, look past the
         * complete-burst range for a known prefix whose burst runs off the
         * window's end: the first origin matching nknown - 3 of it. The
         * streaming receiver replays from there instead of discarding a burst
         * that a false trigger (a click) started the window too early for.
         * This runs only after the scan above has failed, so no verdict
         * changes. Ported from Exsecutor's auditus.exsc. */
        if (trunc_out) {
            long q, q_hi = (long)nsamp - (long)nknown * (long)L;
            for (q = o_hi + 1; q <= q_hi; ++q) {
                int score = 0;
                for (k = 0; k < nknown; ++k) {
                    pf_symbol_energies(&pf, q + k * (long)L, L, E);
                    if (argmax_d(E, N) == known[k]) ++score;
                }
                if (score >= nknown - 3) { *trunc_out = q; break; }
            }
        }
        rc = HYDRA_ERR_NO_SYNC; goto done;
    }
    if (diag) diag->frame_origin = best_o;

    /* ---- data demod with a decision-directed timing loop ----
     * Integrate-and-dump gives a FLAT within-symbol energy envelope, so an
     * early-late gate has no stable lock point. We track on the TOTAL energy
     * across all tones, sum_k E_k(a): for a window straddling two (different)
     * symbols this equals A^2*(d^2 + (L-d)^2), which is maximized exactly at
     * symbol alignment (d=0) -- a smooth, symmetric peak. Its normalized
     * gradient g = (sumE(a+1) - sumE(a-1)) / sumE(a) is a correctly signed
     * S-curve that, unlike a single-tone gradient, does NOT depend on which tone
     * wins, so it never flips sign when the window drifts onto a neighbour
     * (the bug that broke negative clock offsets). When neighbours share a tone
     * there is no transition and g ~ 0 (harmless). A first-order loop with a
     * proportional (phase) and a slow integral (rate) term follows the TX/RX
     * clock offset; light EMA smoothing keeps it noise-robust. */
    soft = (float *)malloc(p->data_syms * (size_t)bps * sizeof *soft);
    if (!soft) { rc = HYDRA_ERR_ALLOC; goto done; }

    pos = (double)best_o + (double)(((size_t)p->preamble_syms + p->sync_syms) * (size_t)L);
    sps = (double)L;
    {
    double drift = 0.0, pos0 = pos;                   /* smoothed timing offset */
    for (j = 0; j < p->data_syms; ++j) {
        long   a0 = (long)lround(pos), a;
        int    b, best_off = 0, t, oi, best_oi = HYDRA_TIMING_SEARCH;
        double etmax = -1.0, etmin = 1e300;
        /* total energy across all tones at offsets -SEARCH..+SEARCH */
        for (oi = 0; oi <= 2 * HYDRA_TIMING_SEARCH; ++oi) {
            long aa = a0 + (oi - HYDRA_TIMING_SEARCH); double e = -1.0;
            if (aa >= 0 && aa + L <= (long)nsamp) {
                e = 0.0; for (t = 0; t < N; ++t) e += pf_energy(&pf, t, aa, L);
            }
            if (e > etmax) { etmax = e; best_oi = oi; }
            if (e >= 0.0 && e < etmin) etmin = e;
        }
        /* Total energy is PEAKED at a symbol boundary (the window straddles two
         * different tones) but FLAT during a same-tone run (a continuous tone --
         * any alignment captures full energy). Only a transition carries timing
         * information; updating on a flat profile would just rail the argmax to
         * the search edge and walk the grid away. So gate the update on the
         * profile being non-flat. */
        if (etmax > 0.0 && (etmax - etmin) > HYDRA_TIMING_GATE * etmax) {
            best_off = best_oi - HYDRA_TIMING_SEARCH;
            drift += HYDRA_TIMING_EMA * ((double)best_off - drift); /* noise -> ~0 */
        } else {
            best_off = 0;                                 /* uninformative: coast */
        }
        pos += sps + HYDRA_TIMING_TRACK * drift;

        a = a0 + best_off;
        if (a < 0 || a + L > (long)nsamp) { rc = HYDRA_ERR_NO_SYNC; goto done; }
        pf_symbol_energies(&pf, a, L, E);
        for (b = 0; b < bps; ++b)
            soft[j * (size_t)bps + (size_t)b] = bit_soft(E, N, bps, b);
    }
    /* effective samples/symbol = mean grid advance, including timing corrections */
    if (p->data_syms > 1) sps = (pos - pos0) / (double)(p->data_syms);
    }
    if (diag) {
        diag->est_sps   = sps;
        diag->clock_ppm = (sps / (double)L - 1.0) * 1e6;
    }

    rc = hydra_frame_decode_soft(p, soft, payload_out);
    rc = (rc == 0) ? HYDRA_OK : HYDRA_ERR_CRC;

done:
    free(known);
    free(soft);
    free(iq);
    prefix_free(&pf);
    hydra_rx_dsp_destroy(rx);
    return rc;
}

/* ----------------------------- one-shot API ------------------------------ */

int hydra_modem_rx_ex(const hydra_profile *p, const float *audio, size_t nsamp,
                      uint8_t payload_out[HYDRA_DCF_BYTES], hydra_rx_diag *diag)
{
    hydra_rx_diag local;
    if (!diag) diag = &local;
    memset(diag, 0, sizeof *diag);
    return decode_window(p, audio, nsamp, payload_out, diag, NULL);
}

int hydra_modem_rx(const hydra_profile *p, const float *audio, size_t nsamp,
                   uint8_t payload_out[HYDRA_DCF_BYTES])
{
    return hydra_modem_rx_ex(p, audio, nsamp, payload_out, NULL);
}

/* ============================ streaming RX =============================== */

enum { ST_SEARCH = 0, ST_COLLECT = 1 };

struct hydra_rx {
    hydra_profile  p;
    hydra_frame_cb cb;
    void          *user;

    float  *buf;
    size_t  cap;
    size_t  collected;
    size_t  body_len;
    size_t  frame_len;

    int     state;
    double  noise;
    size_t  quiet_run;
};

hydra_rx *hydra_rx_create(const hydra_profile *p, hydra_frame_cb cb, void *user)
{
    hydra_rx *rx;
    size_t spp, margin;
    if (!p) return NULL;
    rx = (hydra_rx *)calloc(1, sizeof *rx);
    if (!rx) return NULL;
    rx->p = *p; rx->cb = cb; rx->user = user;

    spp           = (size_t)p->samples_per_symbol;
    rx->body_len  = p->total_syms * spp;
    margin        = (size_t)(0.05 * p->sample_rate) + 4 * spp;
    rx->frame_len = rx->body_len + margin;
    rx->cap       = rx->frame_len + spp;
    rx->buf = (float *)malloc(rx->cap * sizeof *rx->buf);
    if (!rx->buf) { free(rx); return NULL; }

    rx->state = ST_SEARCH;
    return rx;
}

void hydra_rx_destroy(hydra_rx *rx) { if (rx) { free(rx->buf); free(rx); } }

void hydra_rx_reset(hydra_rx *rx)
{
    if (!rx) return;
    rx->state = ST_SEARCH; rx->collected = 0; rx->quiet_run = 0;
}

static int rx_step(hydra_rx *rx, float sample);

/* Decode the collected window. On success only the samples through the frame's
 * end (origin + body + release ramp) are consumed; the rest -- which, for bursts
 * sent back to back, holds the start of the NEXT frame -- is replayed through
 * the segmenter. Discarding the whole window, as this did before, lost every
 * other frame of a back-to-back stream whenever the window outran one burst
 * (measured: 1 of 4 melody frames at a 60 ms gap).
 *
 * A failed window is discarded whole -- UNLESS it is FULL and a burst starts
 * inside it and runs off its end: a click opened the window early and noise
 * kept the silence rule from closing it. Then the window is replayed from one
 * symbol before that burst, so its preamble opens a fresh window. Only a full
 * window: one closed by silence cut the burst because the burst itself was cut
 * (a musical burst has no silent gap), and replaying would find the same cut,
 * one sample shorter each time. In a full window the truncated burst starts
 * after the last complete-burst origin, well past sample 0; the floor of 1 is a
 * backstop that keeps every replay strictly shorter than its window. */
static int rx_try_decode(hydra_rx *rx)
{
    uint8_t payload[HYDRA_DCF_BYTES];
    hydra_rx_diag diag;
    size_t collected = rx->collected, end, i;
    long trunc = -1;
    int frames = 0;
    memset(&diag, 0, sizeof diag);
    rx->state = ST_SEARCH; rx->collected = 0; rx->quiet_run = 0;
    if (decode_window(&rx->p, rx->buf, collected, payload, &diag, &trunc) != HYDRA_OK) {
        if (trunc >= 0 && collected >= rx->frame_len) {
            long L = rx->p.samples_per_symbol;
            size_t start = (trunc > L + 1) ? (size_t)(trunc - L) : 1u;
            for (i = start; i < collected; ++i)
                frames += rx_step(rx, rx->buf[i]);
        }
        return frames;
    }
    if (rx->cb) rx->cb(payload, &diag, rx->user);
    frames = 1;
    end = (size_t)diag.frame_origin + rx->body_len
        + (size_t)(rx->p.ramp_ms * 1e-3 * rx->p.sample_rate + 0.5);
    if (end < collected) {
        /* replay in place: a replayed sample is written at an index no greater
         * than the one it is read from, so reads never see their own writes */
        for (i = end; i < collected; ++i)
            frames += rx_step(rx, rx->buf[i]);
    }
    return frames;
}

/* One sample through the energy segmenter. */
static int rx_step(hydra_rx *rx, float sample)
{
    double ax = fabs((double)sample);
    double on_thr  = (6.0 * rx->noise > 0.02) ? 6.0 * rx->noise : 0.02;
    double off_thr = (3.0 * rx->noise > 0.01) ? 3.0 * rx->noise : 0.01;

    if (rx->state == ST_SEARCH) {
        rx->noise += 0.001 * (ax - rx->noise);
        if (ax > on_thr) {
            rx->state = ST_COLLECT;
            rx->collected = 0;
            rx->quiet_run = 0;
            rx->buf[rx->collected++] = sample;
        }
        return 0;
    }
    /* ST_COLLECT */
    if (rx->collected < rx->cap)
        rx->buf[rx->collected++] = sample;
    rx->quiet_run = (ax < off_thr) ? rx->quiet_run + 1 : 0;

    if (rx->collected >= rx->frame_len || rx->collected >= rx->cap)
        return rx_try_decode(rx);
    if (rx->quiet_run > 3u * (size_t)rx->p.samples_per_symbol) {
        /* burst ended (sustained silence). If at least a whole frame body was
         * captured, decode it -- a lone frame is followed only by its own
         * trailing guard, never reaching frame_len. A shorter burst was a false
         * trigger and is dropped. */
        if (rx->collected >= rx->body_len)
            return rx_try_decode(rx);
        rx->state = ST_SEARCH; rx->collected = 0; rx->quiet_run = 0;
    }
    return 0;
}

int hydra_rx_push(hydra_rx *rx, const float *samples, size_t n)
{
    size_t i;
    int    frames = 0;
    if (!rx || (!samples && n)) return HYDRA_ERR_ARG;
    for (i = 0; i < n; ++i)
        frames += rx_step(rx, samples[i]);
    return frames;
}
