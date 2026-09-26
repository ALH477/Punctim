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

int hydra_modem_tx(const hydra_profile *p,
                   const uint8_t payload[HYDRA_DCF_BYTES],
                   float **audio_out, size_t *nsamp_out)
{
    uint8_t      *symbols = NULL;
    float        *freq    = NULL;
    float        *audio   = NULL;
    hydra_tx_dsp *tx      = NULL;
    size_t        nsym = 0, spp, body, ramp, span, lead, tail, total, i;
    long          s;
    int           rc, k, ndrone = 0;
    double        data_gain, dstep[HYDRA_MUSIC_MAX_DRONES];

    if (!p || !payload || !audio_out || !nsamp_out) return HYDRA_ERR_ARG;

    spp  = (size_t)p->samples_per_symbol;
    /* Optional attack/release: the first tone is pre-rolled and the last tone
     * post-rolled for `ramp` samples under a raised-cosine envelope, OUTSIDE the
     * symbol body -- no data symbol is attenuated, and the pre-roll is the same
     * tone as preamble symbol 0, so acquisition is unaffected. ramp_ms == 0
     * (every linear profile) renders exactly the historical waveform. */
    ramp = (size_t)(p->ramp_ms * 1e-3 * p->sample_rate + 0.5);
    lead = (size_t)(0.02 * p->sample_rate) + ramp;   /* 20 ms silence each side */
    tail = lead;

    symbols = (uint8_t *)malloc(p->total_syms);
    if (!symbols) { rc = HYDRA_ERR_ALLOC; goto done; }

    rc = hydra_frame_build(p, payload, symbols, p->total_syms, &nsym);
    if (rc != 0 || nsym == 0) { rc = HYDRA_ERR_ARG; goto done; }

    body  = nsym * spp;
    span  = ramp + body + ramp;
    total = lead + body + tail;

    freq  = (float *)malloc(span  * sizeof *freq);
    audio = (float *)calloc(total, sizeof *audio);   /* guards start at 0 */
    if (!freq || !audio) { rc = HYDRA_ERR_ALLOC; goto done; }

    for (i = 0; i < ramp; ++i) {
        freq[i]               = (float)hydra_tone_freq(p, symbols[0]);
        freq[ramp + body + i] = (float)hydra_tone_freq(p, symbols[nsym - 1]);
    }
    for (s = 0; s < (long)nsym; ++s) {
        double f = hydra_tone_freq(p, symbols[s]);
        for (i = 0; i < spp; ++i)
            freq[ramp + (size_t)s * spp + i] = (float)f;
    }

    tx = hydra_tx_dsp_create(p->sample_rate);
    if (!tx) { rc = HYDRA_ERR_ALLOC; goto done; }
    hydra_tx_dsp_process(tx, freq, audio + lead - ramp, (int)span);

    /* Drone partials (musical profiles only): steady tones at integer harmonics
     * of the baud that are NOT data tones. For any two such frequencies the
     * cross term integrates to zero over any window of one symbol, whatever its
     * start, so the accompaniment is invisible to every correlator on the grid
     * and during the acquisition scan. The data carrier gives up the drones'
     * share of the amplitude so the peak stays <= tx_gain. */
    for (k = 0; k < HYDRA_MUSIC_MAX_DRONES; ++k)
        if (p->drone_mult[k] > 0)
            dstep[ndrone++] = (double)p->drone_mult[k] * p->baud / p->sample_rate;
    data_gain = p->tx_gain - (double)ndrone * p->drone_gain;

    for (i = 0; i < span; ++i) {
        double v = data_gain * (double)audio[lead - ramp + i], env = 1.0;
        for (k = 0; k < ndrone; ++k) {
            double ph = dstep[k] * (double)i;
            v += p->drone_gain * sin(2.0 * M_PI * (ph - floor(ph)));
        }
        if (i < ramp)
            env = 0.5 - 0.5 * cos(M_PI * ((double)i + 0.5) / (double)ramp);
        else if (i >= ramp + body)
            env = 0.5 + 0.5 * cos(M_PI * ((double)(i - ramp - body) + 0.5) / (double)ramp);
        audio[lead - ramp + i] = (float)(env * v);
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

static int decode_window(const hydra_profile *p, const float *audio, size_t nsamp,
                         uint8_t payload_out[HYDRA_DCF_BYTES], hydra_rx_diag *diag)
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
            if (score > best_score) { best_score = score; o_first = o; o_last = o; }
            else if (score == best_score) { o_last = o; }
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
    if (best_score < nknown - 3) { rc = HYDRA_ERR_NO_SYNC; goto done; }
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
    return decode_window(p, audio, nsamp, payload_out, diag);
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

static int rx_try_decode(hydra_rx *rx)
{
    uint8_t payload[HYDRA_DCF_BYTES];
    hydra_rx_diag diag;
    memset(&diag, 0, sizeof diag);
    if (decode_window(&rx->p, rx->buf, rx->collected, payload, &diag) == HYDRA_OK) {
        if (rx->cb) rx->cb(payload, &diag, rx->user);
        return 1;
    }
    return 0;
}

int hydra_rx_push(hydra_rx *rx, const float *samples, size_t n)
{
    size_t i;
    int    frames = 0;

    if (!rx || (!samples && n)) return HYDRA_ERR_ARG;

    for (i = 0; i < n; ++i) {
        double ax = fabs((double)samples[i]);
        double on_thr  = (6.0 * rx->noise > 0.02) ? 6.0 * rx->noise : 0.02;
        double off_thr = (3.0 * rx->noise > 0.01) ? 3.0 * rx->noise : 0.01;

        if (rx->state == ST_SEARCH) {
            rx->noise += 0.001 * (ax - rx->noise);
            if (ax > on_thr) {
                rx->state = ST_COLLECT;
                rx->collected = 0;
                rx->quiet_run = 0;
                rx->buf[rx->collected++] = samples[i];
            }
        } else { /* ST_COLLECT */
            if (rx->collected < rx->cap)
                rx->buf[rx->collected++] = samples[i];
            rx->quiet_run = (ax < off_thr) ? rx->quiet_run + 1 : 0;

            if (rx->collected >= rx->frame_len || rx->collected >= rx->cap) {
                frames += rx_try_decode(rx);
                rx->state = ST_SEARCH; rx->collected = 0; rx->quiet_run = 0;
            } else if (rx->quiet_run > 3u * (size_t)rx->p.samples_per_symbol) {
                /* burst ended (sustained silence). If at least a whole frame
                 * body was captured, decode it -- a lone frame is followed only
                 * by its own trailing guard, never reaching frame_len. A shorter
                 * burst was a false trigger and is dropped. */
                if (rx->collected >= rx->body_len)
                    frames += rx_try_decode(rx);
                rx->state = ST_SEARCH; rx->collected = 0; rx->quiet_run = 0;
            }
        }
    }
    return frames;
}
