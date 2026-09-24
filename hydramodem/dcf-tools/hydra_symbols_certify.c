// SPDX-License-Identifier: LGPL-3.0-only
/* dcf-tools/hydra_symbols_certify.c -- certify the DCF-Medium `hydra_symbols` family
 * against the REAL HydraModem C code (the ground truth the vectors were ported from).
 *
 * codec/medium_vectors.gen.h is generated from python/MCP/mediumlab_core.py, a Python
 * port of hydra_frame_build(). This tool proves that port is exact, case by case:
 *
 *   profile  the vector profile tables equal hydra_profile_default()/_aux_cable(), and
 *            the sync anchor equals the C sync_word;
 *   sizing   hydra_profile_init() derives the vector's coded_bits / interleave_stride /
 *            total_syms;
 *   symbols  hydra_frame_build() emits the vector's symbol string EXACTLY (one hex digit
 *            per tone index) -- this is the certified tier;
 *   decode   the vector's symbols, fed as hard +/-1 metrics through the real
 *            hydra_frame_decode_soft() (deinterleave + FEC + CRC), give back the frame;
 *   loopback hydra_modem_tx() -> hydra_modem_rx_ex() with the linked DSP backend (the
 *            portable reference by default) recovers the 17 bytes byte-exact. The
 *            waveform is loopback-tested, not certified (DCF_MEDIUM_SPEC.md).
 *
 * HydraModem never parses the frame, so the 246-vector wire certificate is untouched.
 * Upstream hydramodem/src stays unmodified; this is repo glue (DeMoD LLC, LGPL-3.0).
 *
 *   cc -std=gnu11 hydra_symbols_certify.c ../build/libhydramodem.a -lm -o hydra_symbols_certify
 */
#include "../src/hydramodem.h"
#include "../src/hydra_frame.h"
#if defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-const-variable"  /* the other media families */
#endif
#include "../../codec/medium_vectors.gen.h"
#if defined(__GNUC__)
#pragma GCC diagnostic pop
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SYM_CAP 4096

static const char *FEC_NAME[3] = {"none", "rep3", "conv"};

/* one reporting group = profile x fec x interleave (x n_tones when not binary) */
typedef struct {
    const char *profile; int fec, interleave, n_tones;
    int cases, sym_ok, dec_ok, loop_ok;
} group_t;

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static const med_hydra_profile_t *vec_profile(const char *name)
{
    for (int i = 0; i < (int)(sizeof MED_HYDRA_PROFILES / sizeof MED_HYDRA_PROFILES[0]); ++i)
        if (!strcmp(MED_HYDRA_PROFILES[i].name, name)) return &MED_HYDRA_PROFILES[i];
    return NULL;
}

/* The C base profile for a vector profile name (the functions the port mirrors). */
static int c_profile(const char *name, hydra_profile *p)
{
    memset(p, 0, sizeof *p);
    if (!strcmp(name, "default")) { hydra_profile_default(p);   return 0; }
    if (!strcmp(name, "aux"))     { hydra_profile_aux_cable(p); return 0; }
    return -1;
}

/* profile tables: the vector's user fields == the C profile function's. */
static int check_profiles(void)
{
    int fail = 0;
    for (int i = 0; i < (int)(sizeof MED_HYDRA_PROFILES / sizeof MED_HYDRA_PROFILES[0]); ++i) {
        const med_hydra_profile_t *v = &MED_HYDRA_PROFILES[i];
        hydra_profile c;
        if (c_profile(v->name, &c) != 0) {
            printf("  FAIL profile %s: no C profile function of that name\n", v->name);
            fail = 1; continue;
        }
        if (v->sample_rate != c.sample_rate || v->baud != c.baud || v->n_tones != c.n_tones ||
            v->base_freq != c.base_freq || v->tone_spacing != c.tone_spacing ||
            v->preamble_syms != c.preamble_syms || v->sync_word != c.sync_word) {
            printf("  FAIL profile %s: vector {%.0f Hz, %.0f baud, %d tones, base %.0f, "
                   "spacing %.0f, preamble %d, sync 0x%04X} != C {%.0f Hz, %.0f baud, %d tones, "
                   "base %.0f, spacing %.0f, preamble %d, sync 0x%04X}\n", v->name,
                   v->sample_rate, v->baud, v->n_tones, v->base_freq, v->tone_spacing,
                   v->preamble_syms, v->sync_word, c.sample_rate, c.baud, c.n_tones,
                   c.base_freq, c.tone_spacing, c.preamble_syms, (unsigned)c.sync_word);
            fail = 1;
        }
        if (c.sync_word != MED_HYDRA_SYNC) {
            printf("  FAIL profile %s: C sync 0x%04X != MED_HYDRA_SYNC 0x%04X\n", v->name,
                   (unsigned)c.sync_word, (unsigned)MED_HYDRA_SYNC);
            fail = 1;
        }
    }
    if (!fail)
        printf("  profile tables : %d/%d equal the C hydra_profile_default/_aux_cable, "
               "sync 0x%04X\n", (int)(sizeof MED_HYDRA_PROFILES / sizeof MED_HYDRA_PROFILES[0]),
               (int)(sizeof MED_HYDRA_PROFILES / sizeof MED_HYDRA_PROFILES[0]),
               (unsigned)MED_HYDRA_SYNC);
    return fail;
}

/* Build the case's profile from the vector profile + case overrides, then init. */
static int case_profile(const med_hydra_case_t *c, hydra_profile *p)
{
    const med_hydra_profile_t *v = vec_profile(c->profile);
    if (!v || c_profile(c->profile, p) != 0) return -1;   /* tx_gain etc. from C */
    p->sample_rate   = v->sample_rate;
    p->baud          = v->baud;
    p->n_tones       = v->n_tones;
    p->base_freq     = v->base_freq;       /* kept when n_tones differs (e.g. 4-FSK) */
    p->tone_spacing  = v->tone_spacing;
    p->preamble_syms = v->preamble_syms;
    p->sync_word     = (uint16_t)v->sync_word;
    if (c->fec < 0 || c->fec > 2) return -1;
    p->fec_mode      = (hydra_fec_mode)c->fec;
    p->interleave    = c->interleave;
    p->n_tones       = c->n_tones;
    return hydra_profile_init(p);
}

static void print_diff(const char *want, const uint8_t *got, size_t ngot)
{
    size_t nw = strlen(want), first = (size_t)-1;
    for (size_t i = 0; i < nw || i < ngot; ++i) {
        int w = i < nw ? hexval(want[i]) : -1, g = i < ngot ? (int)got[i] : -2;
        if (w != g) { first = i; break; }
    }
    printf("      vector (python) %zu syms: %s\n", nw, want);
    printf("      hydra_frame_build %zu syms: ", ngot);
    for (size_t i = 0; i < ngot; ++i) putchar("0123456789abcdef"[got[i] & 0xF]);
    printf("\n      first difference at symbol %zu\n", first);
}

int main(void)
{
    static group_t g[64];
    int ng = 0, fail = 0;
    int n_sym = 0, n_dec = 0, n_loop = 0;

    printf("HydraModem hydra_symbols certification: %d cases from codec/medium_vectors.gen.h\n"
           "  vs the real hydra_frame_build / hydra_frame_decode_soft / hydra_modem_tx->rx\n",
           MED_N_HYDRA);
    fail |= check_profiles();

    for (int k = 0; k < MED_N_HYDRA; ++k) {
        const med_hydra_case_t *c = &MED_HYDRA_CASES[k];
        hydra_profile p;
        int gi, ok_sym = 1, ok_dec = 1, ok_loop = 1;

        for (gi = 0; gi < ng; ++gi)
            if (!strcmp(g[gi].profile, c->profile) && g[gi].fec == c->fec &&
                g[gi].interleave == c->interleave && g[gi].n_tones == c->n_tones) break;
        if (gi == ng) {
            if (ng == (int)(sizeof g / sizeof g[0])) { printf("too many groups\n"); return 1; }
            g[ng].profile = c->profile; g[ng].fec = c->fec;
            g[ng].interleave = c->interleave; g[ng].n_tones = c->n_tones;
            g[ng].cases = g[ng].sym_ok = g[ng].dec_ok = g[ng].loop_ok = 0;
            ++ng;
        }
        g[gi].cases++;

        if (case_profile(c, &p) != 0) {
            printf("  FAIL %s: hydra_profile_init rejected the case profile\n", c->name);
            fail = 1; continue;
        }

        /* sizing */
        if (p.coded_bits != (size_t)c->coded_bits ||
            p.interleave_stride != c->interleave_stride ||
            p.total_syms != (size_t)c->total_syms) {
            printf("  FAIL %s: sizing C coded_bits/stride/total_syms = %zu/%d/%zu, "
                   "vector = %d/%d/%d\n", c->name, p.coded_bits, p.interleave_stride,
                   p.total_syms, c->coded_bits, c->interleave_stride, c->total_syms);
            ok_sym = 0;
        }

        /* symbols: the real hydra_frame_build vs the vector string */
        uint8_t syms[SYM_CAP]; size_t n = 0;
        size_t nw = strlen(c->symbols);
        if (p.total_syms > SYM_CAP ||
            hydra_frame_build(&p, c->frame, syms, SYM_CAP, &n) != 0) {
            printf("  FAIL %s: hydra_frame_build failed\n", c->name);
            ok_sym = 0; n = 0;
        } else {
            int same = (n == nw);
            for (size_t i = 0; same && i < n; ++i)
                if (hexval(c->symbols[i]) != (int)syms[i]) same = 0;
            if (!same) {
                printf("  FAIL %s: symbol stream mismatch\n", c->name);
                print_diff(c->symbols, syms, n);
                ok_sym = 0;
            }
        }

        /* decode: vector symbols -> hard metrics -> the real soft decoder -> frame */
        {
            static uint8_t bits[SYM_CAP * 4];
            static float soft[SYM_CAP * 4];
            uint8_t out[HYDRA_DCF_BYTES];
            size_t head = (size_t)p.preamble_syms + p.sync_syms;
            int bad = (nw != p.total_syms);
            for (size_t i = 0; !bad && i < nw; ++i) {
                int v = hexval(c->symbols[i]);
                if (v < 0 || v >= p.n_tones) bad = 1; else syms[i] = (uint8_t)v;
            }
            if (!bad) {
                hydra_symbols_to_bits(syms + head, p.data_syms, p.bits_per_symbol, bits);
                for (size_t i = 0; i < p.coded_bits; ++i) soft[i] = bits[i] ? 1.0f : -1.0f;
                bad = hydra_frame_decode_soft(&p, soft, out) != 0 ||
                      memcmp(out, c->frame, HYDRA_DCF_BYTES) != 0;
            }
            if (bad) {
                printf("  FAIL %s: vector symbols do not decode to the frame in "
                       "hydra_frame_decode_soft\n", c->name);
                ok_dec = 0;
            }
        }

        /* loopback: TX -> RX over the ideal (in-memory) medium */
        {
            float *audio = NULL; size_t ns = 0;
            uint8_t out[HYDRA_DCF_BYTES]; hydra_rx_diag d;
            int rc = hydra_modem_tx(&p, c->frame, &audio, &ns);
            if (rc == HYDRA_OK) rc = hydra_modem_rx_ex(&p, audio, ns, out, &d);
            free(audio);
            if (rc != HYDRA_OK || memcmp(out, c->frame, HYDRA_DCF_BYTES) != 0) {
                printf("  FAIL %s: TX->RX loopback %s\n", c->name,
                       rc != HYDRA_OK ? hydra_strerror(rc) : "payload mismatch");
                ok_loop = 0;
            }
        }

        g[gi].sym_ok += ok_sym; g[gi].dec_ok += ok_dec; g[gi].loop_ok += ok_loop;
        n_sym += ok_sym; n_dec += ok_dec; n_loop += ok_loop;
        if (!(ok_sym && ok_dec && ok_loop)) fail = 1;
    }

    for (int i = 0; i < ng; ++i) {
        int pass = g[i].sym_ok == g[i].cases && g[i].dec_ok == g[i].cases &&
                   g[i].loop_ok == g[i].cases;
        char nt[16] = "";
        if (g[i].n_tones != 2) snprintf(nt, sizeof nt, " nt%d", g[i].n_tones);
        printf("  %s %-7s %-4s il%d%-4s : %2d cases  symbols %2d/%-2d  decode %2d/%-2d  "
               "loopback %2d/%d\n", pass ? "PASS" : "FAIL", g[i].profile,
               (g[i].fec >= 0 && g[i].fec <= 2) ? FEC_NAME[g[i].fec] : "?", g[i].interleave,
               nt, g[i].cases, g[i].sym_ok, g[i].cases, g[i].dec_ok, g[i].cases,
               g[i].loop_ok, g[i].cases);
    }
    printf("  totals: symbols %d/%d byte-exact vs hydra_frame_build, decode %d/%d, "
           "TX->RX loopback %d/%d\n", n_sym, MED_N_HYDRA, n_dec, MED_N_HYDRA, n_loop,
           MED_N_HYDRA);

    if (fail || MED_N_HYDRA == 0) {
        printf("HYDRA SYMBOL CERTIFICATION FAILED\n");
        return 1;
    }
    printf("ALL HYDRA SYMBOL VECTORS HOLD\n");
    return 0;
}
