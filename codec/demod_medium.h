/* SPDX-License-Identifier: LGPL-3.0-only
 *
 * demod_medium.h — DCF-Medium: the reference medium codecs in C (header-only, C11).
 *
 * A *medium codec* is a deterministic pair
 *     encode : [frame]        -> representation
 *     decode : representation -> ([frame], diagnostics)
 * that carries the 17-byte DeModFrame quantum over one medium WITHOUT parsing it beyond
 * the frame gate (sync 0xD3 + version nibble 1 + CRC-16/CCITT-FALSE). Media are
 * transports *beneath* the quantum, so the 246-vector wire certificate is untouched.
 *
 * Byte-identical port of python/MCP/mediumlab_core.py (canonical); pinned by
 * Documentation/medium_vectors.json + codec/medium_vectors.gen.h and certified by
 * C_SDK/tests/test_medium_certify.c. Spec: Documentation/DCF_MEDIUM_SPEC.md.
 *
 * Families (all byte-certified; the analog ones to their symbol / bit stream):
 *   stream         .dcf file / stdio — concatenated frames, byte-wise resync on decode
 *   hex            34 lowercase hex chars + "\n" per frame
 *   udp_proto      ProtoMessage envelope [type u8][seq u32][ts u64][len u32] (BE), FRAME=12
 *   udp_bare       bare 17-B frame or a 32-B SuperPack pair per datagram
 *   l2eth          [n u16 BE][SuperPack * ceil(n/2)] (DCF-Snake raw-L2 Ethernet payload)
 *   hydra_symbols  HydraModem M-FSK tone-index stream (port of hydramodem/src/hydra_*.c)
 *   afsk_bits      python/modem AFSK on-air bit stream (acoustic_frame.encode_bits)
 *
 * `hydra` and `afsk` are two DIFFERENT acoustic media (different tones, sync word and
 * FEC); they do not interoperate with each other. Only the symbol/bit streams are
 * certified here; the WAV waveform that carries them is loopback-tested.
 *
 * No malloc: every buffer is caller-supplied or a bounded stack array.
 */
#ifndef DCF_DEMOD_MEDIUM_H
#define DCF_DEMOD_MEDIUM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "demod_frame.h"
#include "demod_superpack.h"
#include "demod_fec.h"

/* ── shared ─────────────────────────────────────────────────────────────────────── */
#define DCF_MEDIUM_FRAME_LEN 17u

typedef void (*dcf_medium_frame_cb)(const uint8_t *frame17, void *user);

/* The frame gate (the existing validity rule; media never parse further): 17 bytes,
 * sync 0xD3, version nibble 1, CRC-16/CCITT-FALSE over [0..14] == [15..16] (BE). */
static inline bool dcf_medium_gate(const uint8_t *f) {
    if (f[0] != DCF_SYNC_BYTE) return false;
    if ((f[1] >> 4) != 1u) return false;
    uint16_t stored = (uint16_t)(((unsigned)f[15] << 8) | f[16]);
    return dcf_crc16(f, DCF_FRAME_CRC_COVER) == stored;
}

/* ══ stream (.dcf file, stdio) ═══════════════════════════════════════════════════════
 * encode = concatenated 17-byte frames. decode = byte-wise resync scan: at offset i, if
 * the 17-byte window passes the gate emit it and i += 17, else i += 1, skipped++. The
 * trailing < 17 bytes that can never hold a frame are tail_bytes (not counted skipped). */
typedef struct {
    size_t frames;
    size_t skipped_bytes;
    size_t tail_bytes;
} dcf_stream_result_t;

/* One-shot decode of a whole buffer; cb (may be NULL) receives each gated frame. */
static inline dcf_stream_result_t dcf_stream_scan(const uint8_t *buf, size_t len,
                                                  dcf_medium_frame_cb cb, void *user) {
    dcf_stream_result_t r = {0, 0, 0};
    size_t i = 0;
    while (len >= DCF_MEDIUM_FRAME_LEN && i <= len - DCF_MEDIUM_FRAME_LEN) {
        const size_t last = len - DCF_MEDIUM_FRAME_LEN;
        if (buf[i] != DCF_SYNC_BYTE) {
            /* fast-forward to the next 0xD3 with a full window (pure speed-up) */
            const uint8_t *p = (const uint8_t *)memchr(buf + i + 1, DCF_SYNC_BYTE, last - i);
            if (!p) { r.skipped_bytes += last + 1 - i; i = last + 1; break; }
            size_t j = (size_t)(p - buf);
            r.skipped_bytes += j - i;
            i = j;
            continue;
        }
        if (dcf_medium_gate(buf + i)) {
            if (cb) cb(buf + i, user);
            r.frames++;
            i += DCF_MEDIUM_FRAME_LEN;
        } else {
            i++;
            r.skipped_bytes++;
        }
    }
    r.tail_bytes = len - i;
    return r;
}

/* Incremental scanner: any chunking yields exactly dcf_stream_scan()'s frames and
 * skipped_bytes; at most 16 bytes are carried between feeds. */
typedef struct {
    uint8_t  carry[16];
    size_t   ncarry;
    uint64_t skipped;
} dcf_stream_scanner_t;

static inline void dcf_stream_scanner_init(dcf_stream_scanner_t *s) {
    memset(s, 0, sizeof *s);
}

static inline uint8_t dcf__stream_at(const dcf_stream_scanner_t *s, const uint8_t *chunk,
                                     size_t k) {
    return (k < s->ncarry) ? s->carry[k] : chunk[k - s->ncarry];
}

/* Feed a chunk; cb receives each completed frame. Returns the number of frames emitted. */
static inline size_t dcf_stream_feed(dcf_stream_scanner_t *s, const uint8_t *chunk, size_t n,
                                     dcf_medium_frame_cb cb, void *user) {
    const size_t total = s->ncarry + n;
    size_t i = 0, frames = 0;
    uint8_t w[DCF_MEDIUM_FRAME_LEN];
    while (total >= DCF_MEDIUM_FRAME_LEN && i <= total - DCF_MEDIUM_FRAME_LEN) {
        const size_t last = total - DCF_MEDIUM_FRAME_LEN;
        if (i >= s->ncarry) {
            /* wholly inside the chunk: scan it directly (same arithmetic as the one-shot) */
            const uint8_t *base = chunk + (i - s->ncarry);
            if (base[0] != DCF_SYNC_BYTE) {
                const uint8_t *p = (const uint8_t *)memchr(base + 1, DCF_SYNC_BYTE, last - i);
                if (!p) { s->skipped += last + 1 - i; i = last + 1; break; }
                size_t j = (size_t)(p - chunk) + s->ncarry;
                s->skipped += j - i;
                i = j;
                continue;
            }
            if (dcf_medium_gate(base)) {
                if (cb) cb(base, user);
                frames++;
                i += DCF_MEDIUM_FRAME_LEN;
            } else {
                i++;
                s->skipped++;
            }
            continue;
        }
        if (dcf__stream_at(s, chunk, i) != DCF_SYNC_BYTE) { i++; s->skipped++; continue; }
        for (size_t k = 0; k < DCF_MEDIUM_FRAME_LEN; k++) w[k] = dcf__stream_at(s, chunk, i + k);
        if (dcf_medium_gate(w)) {
            if (cb) cb(w, user);
            frames++;
            i += DCF_MEDIUM_FRAME_LEN;
        } else {
            i++;
            s->skipped++;
        }
    }
    uint8_t nc[16];
    size_t m = total - i;                    /* < 17 by the loop condition */
    for (size_t k = 0; k < m; k++) nc[k] = dcf__stream_at(s, chunk, i + k);
    memcpy(s->carry, nc, m);
    s->ncarry = m;
    return frames;
}

/* Bytes currently carried (a possible frame prefix), <= 16. */
static inline size_t dcf_stream_pending(const dcf_stream_scanner_t *s) { return s->ncarry; }

/* End of stream: returns (and clears) the tail byte count. */
static inline size_t dcf_stream_flush(dcf_stream_scanner_t *s) {
    size_t t = s->ncarry;
    s->ncarry = 0;
    return t;
}

/* ══ hex (text lines) ════════════════════════════════════════════════════════════════
 * encode: one frame per line, 34 lowercase hex characters + "\n".
 * decode: split on "\n"; strip leading/trailing space, \t, \r, \v, \f; skip blank lines
 * and lines starting with '#'; accept uppercase; any other line that is not exactly 34
 * hex digits is a bad line. Frames are returned raw (NOT gated — the caller gates). */
#define DCF_HEX_LINE_LEN 35u   /* 34 hex + '\n' */

static inline void dcf_hex_encode_line(const uint8_t *f, char out[36]) {
    static const char d[] = "0123456789abcdef";
    for (size_t i = 0; i < DCF_MEDIUM_FRAME_LEN; i++) {
        out[2 * i]     = d[f[i] >> 4];
        out[2 * i + 1] = d[f[i] & 0xFu];
    }
    out[34] = '\n';
    out[35] = '\0';
}

static inline int dcf__hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static inline bool dcf__hex_ws(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\v' || c == '\f';
}

/* Streaming line classifier: bounded memory for arbitrarily long lines, exact semantics. */
typedef struct {
    char   buf[35];        /* first 35 chars of the stripped content */
    size_t content_len;    /* committed content length (excl. a trailing whitespace run) */
    size_t pending_ws;     /* whitespace run after the last content char */
    bool   started;        /* a non-whitespace char has been seen on this line */
} dcf_hex_line_t;

typedef enum { DCF_HEX_SKIP = 0, DCF_HEX_FRAME = 1, DCF_HEX_BAD = -1 } dcf_hex_line_result_t;

static inline void dcf_hex_line_reset(dcf_hex_line_t *l) { memset(l, 0, sizeof *l); }

static inline void dcf__hex_line_put(dcf_hex_line_t *l, char c) {
    if (l->content_len < sizeof l->buf) l->buf[l->content_len] = c;
    l->content_len++;
}

/* Push one character (never '\n'). */
static inline void dcf_hex_line_push(dcf_hex_line_t *l, char c) {
    if (dcf__hex_ws(c)) {
        if (l->started) l->pending_ws++;
        return;
    }
    if (!l->started) l->started = true;
    while (l->pending_ws) { dcf__hex_line_put(l, ' '); l->pending_ws--; }
    dcf__hex_line_put(l, c);
}

/* End of line: classify (and reset). On DCF_HEX_FRAME, out[17] holds the raw frame. */
static inline dcf_hex_line_result_t dcf_hex_line_end(dcf_hex_line_t *l, uint8_t *out) {
    dcf_hex_line_result_t r;
    if (l->content_len == 0 || l->buf[0] == '#') {
        r = DCF_HEX_SKIP;
    } else if (l->content_len != 2 * DCF_MEDIUM_FRAME_LEN) {
        r = DCF_HEX_BAD;
    } else {
        r = DCF_HEX_FRAME;
        for (size_t i = 0; i < DCF_MEDIUM_FRAME_LEN; i++) {
            int hi = dcf__hexval(l->buf[2 * i]), lo = dcf__hexval(l->buf[2 * i + 1]);
            if (hi < 0 || lo < 0) { r = DCF_HEX_BAD; break; }
            out[i] = (uint8_t)((hi << 4) | lo);
        }
    }
    dcf_hex_line_reset(l);
    return r;
}

/* One-shot decode of a whole text buffer. Returns frames emitted; *bad_lines (may be
 * NULL) receives the bad-line count. The segment after the last '\n' is a line too. */
static inline size_t dcf_hex_decode(const char *text, size_t len, dcf_medium_frame_cb cb,
                                    void *user, size_t *bad_lines) {
    dcf_hex_line_t l;
    uint8_t f[DCF_MEDIUM_FRAME_LEN];
    size_t frames = 0, bad = 0;
    dcf_hex_line_reset(&l);
    for (size_t i = 0; i <= len; i++) {
        if (i == len || text[i] == '\n') {
            dcf_hex_line_result_t r = dcf_hex_line_end(&l, f);
            if (r == DCF_HEX_FRAME) { if (cb) cb(f, user); frames++; }
            else if (r == DCF_HEX_BAD) bad++;
        } else {
            dcf_hex_line_push(&l, text[i]);
        }
    }
    if (bad_lines) *bad_lines = bad;
    return frames;
}

/* ══ udp dialect "proto" (ProtoMessage envelope) ═════════════════════════════════════
 * Byte-identical to python/dcf/proto.py, go/node/proto.go, rust/src/lib.rs and
 * C_SDK/node/dcf_proto.h (kept self-contained here: codec/ never depends on C_SDK/):
 *   [0] msg_type u8 | [1:5] sequence u32 | [5:13] timestamp u64 | [13:17] len u32 | payload */
#define DCF_MEDIUM_PROTO_HDR   17u
#define DCF_MEDIUM_MSG_FRAME   12u
#define DCF_PROTO_FRAME_LEN    (DCF_MEDIUM_PROTO_HDR + DCF_MEDIUM_FRAME_LEN)   /* 34 */

/* Serialise into out[cap]; returns total bytes, or 0 if cap is too small. */
static inline size_t dcf_medium_proto_encode(uint8_t msg_type, uint32_t seq, uint64_t ts,
                                             const uint8_t *payload, uint32_t plen,
                                             uint8_t *out, size_t cap) {
    if (cap < (size_t)DCF_MEDIUM_PROTO_HDR + plen) return 0;
    out[0] = msg_type;
    for (int i = 0; i < 4; i++) out[1 + i] = (uint8_t)(seq >> (24 - 8 * i));
    for (int i = 0; i < 8; i++) out[5 + i] = (uint8_t)(ts >> (56 - 8 * i));
    for (int i = 0; i < 4; i++) out[13 + i] = (uint8_t)(plen >> (24 - 8 * i));
    if (plen && payload) memcpy(out + DCF_MEDIUM_PROTO_HDR, payload, plen);
    return (size_t)DCF_MEDIUM_PROTO_HDR + plen;
}

/* Parse one ProtoMessage. false on a short header or a payload_len that overruns the
 * datagram (the Go/Rust/C guards). Bytes after payload_len are ignored. */
static inline bool dcf_medium_proto_decode(const uint8_t *d, size_t len, uint8_t *msg_type,
                                           uint32_t *seq, uint64_t *ts,
                                           const uint8_t **payload, uint32_t *plen) {
    if (len < DCF_MEDIUM_PROTO_HDR) return false;
    uint32_t pl = 0, sq = 0;
    uint64_t t = 0;
    for (int i = 0; i < 4; i++) pl = (pl << 8) | d[13 + i];
    if ((uint64_t)len < (uint64_t)DCF_MEDIUM_PROTO_HDR + pl) return false;
    for (int i = 0; i < 4; i++) sq = (sq << 8) | d[1 + i];
    for (int i = 0; i < 8; i++) t = (t << 8) | d[5 + i];
    *msg_type = d[0];
    *seq = sq;
    *ts = t;
    *payload = d + DCF_MEDIUM_PROTO_HDR;
    *plen = pl;
    return true;
}

/* One frame as a 34-byte ProtoMessage(FRAME=12, seq, ts, len 17, frame). Returns 34. */
static inline size_t dcf_proto_frame_encode(const uint8_t *frame17, uint32_t seq, uint64_t ts,
                                            uint8_t out[34]) {
    return dcf_medium_proto_encode((uint8_t)DCF_MEDIUM_MSG_FRAME, seq, ts, frame17,
                                   DCF_MEDIUM_FRAME_LEN, out, DCF_PROTO_FRAME_LEN);
}

/* The carried frame iff msg_type == 12 and payload_len == 17 (types 1..11 are adapter
 * envelopes, not frames on this medium). Not gated — the caller gates. */
static inline bool dcf_proto_frame_decode(const uint8_t *dgram, size_t len, uint8_t *out17) {
    uint8_t t;
    uint32_t seq, plen;
    uint64_t ts;
    const uint8_t *p;
    if (!dcf_medium_proto_decode(dgram, len, &t, &seq, &ts, &p, &plen)) return false;
    if (t != DCF_MEDIUM_MSG_FRAME || plen != DCF_MEDIUM_FRAME_LEN) return false;
    memcpy(out17, p, DCF_MEDIUM_FRAME_LEN);
    return true;
}

/* ══ udp dialect "bare" (17-B frame / 32-B SuperPack per datagram) ══════════════════ */
typedef void (*dcf_medium_dgram_cb)(const uint8_t *dgram, size_t len, void *user);

/* Datagrams for a frame sequence: with `pair`, consecutive pairs become one 32-byte
 * SuperPack and a lone trailing frame goes raw (17 B); without, every frame goes raw.
 * A pair that cannot be packed (an ungated frame) goes as two raw datagrams.
 * Returns the number of datagrams emitted. */
static inline size_t dcf_bare_pack_pairs(const uint8_t (*frames)[DCF_MEDIUM_FRAME_LEN],
                                         size_t n, bool pair, dcf_medium_dgram_cb cb,
                                         void *user) {
    size_t i = 0, nd = 0;
    uint8_t sp[DCF_SUPER_LEN];
    if (pair) {
        while (i + 1 < n) {
            if (dcf_superpack_pack(frames[i], frames[i + 1], sp)) {
                if (cb) cb(sp, DCF_SUPER_LEN, user);
                nd++;
            } else {
                if (cb) { cb(frames[i], DCF_MEDIUM_FRAME_LEN, user); cb(frames[i + 1], DCF_MEDIUM_FRAME_LEN, user); }
                nd += 2;
            }
            i += 2;
        }
    }
    for (; i < n; i++) {
        if (cb) cb(frames[i], DCF_MEDIUM_FRAME_LEN, user);
        nd++;
    }
    return nd;
}

/* Frames carried by one bare datagram: a valid 32-byte SuperPack -> 2; a 17-byte
 * datagram -> 1 (not gated — the caller gates); anything else -> 0. */
static inline int dcf_bare_unpack(const uint8_t *dg, size_t len, uint8_t out[2][17]) {
    if (len == DCF_SUPER_LEN && dcf_superpack_is(dg, len))
        return dcf_superpack_unpack(dg, out[0], out[1]) ? 2 : 0;
    if (len == DCF_MEDIUM_FRAME_LEN) { memcpy(out[0], dg, DCF_MEDIUM_FRAME_LEN); return 1; }
    return 0;
}

/* ══ l2eth (raw-L2 Ethernet payload; byte-identical to hydramodem/dcf-tools/snake_l2.h) ═
 * [n_frames u16 BE][SuperPack * ceil(n/2)]; an odd tail is paired with the canonical zero
 * DATA filler frame, which the receiver discards using n_frames. */
#define DCF_L2_HDR 2u

static inline void dcf_l2_filler(uint8_t out[17]) {
    memset(out, 0, DCF_MEDIUM_FRAME_LEN);
    out[0] = DCF_SYNC_BYTE;
    out[1] = 0x10u;                                   /* version 1, type DATA */
    uint16_t crc = dcf_crc16(out, DCF_FRAME_CRC_COVER);
    out[15] = (uint8_t)(crc >> 8);
    out[16] = (uint8_t)crc;
}

/* Frames that fit one Ethernet payload of the given MTU. */
static inline size_t dcf_l2_capacity(size_t mtu) {
    if (mtu < DCF_L2_HDR) return 0;
    return ((mtu - DCF_L2_HDR) / DCF_SUPER_LEN) * 2u;
}

/* Batch n frames into out[cap]. false if cap is too small, n > 65535, or a frame fails
 * the gate. *out_len = 2 + ceil(n/2)*32. */
static inline bool dcf_l2_batch(const uint8_t (*frames)[DCF_MEDIUM_FRAME_LEN], size_t n,
                                uint8_t *out, size_t cap, size_t *out_len) {
    if (n > 0xFFFFu) return false;
    size_t npairs = (n + 1u) / 2u;
    if (DCF_L2_HDR + npairs * DCF_SUPER_LEN > cap) return false;
    out[0] = (uint8_t)(n >> 8);
    out[1] = (uint8_t)n;
    uint8_t filler[DCF_MEDIUM_FRAME_LEN];
    dcf_l2_filler(filler);
    size_t off = DCF_L2_HDR;
    for (size_t i = 0; i < n; i += 2) {
        const uint8_t *b = (i + 1 < n) ? frames[i + 1] : filler;
        if (!dcf_superpack_pack(frames[i], b, out + off)) return false;
        off += DCF_SUPER_LEN;
    }
    *out_len = off;
    return true;
}

/* Split a payload back into up to `max` frames (bit-exact). false on a short/truncated
 * batch, n > max, or a corrupt SuperPack; trailing bytes (Ethernet padding) ignored. */
static inline bool dcf_l2_unbatch(const uint8_t *buf, size_t len,
                                  uint8_t (*frames)[DCF_MEDIUM_FRAME_LEN], size_t max,
                                  size_t *out_n) {
    if (len < DCF_L2_HDR) return false;
    size_t n = ((size_t)buf[0] << 8) | buf[1];
    if (n > max) return false;
    size_t npairs = (n + 1u) / 2u;
    if (len < DCF_L2_HDR + npairs * DCF_SUPER_LEN) return false;
    size_t off = DCF_L2_HDR, got = 0;
    for (size_t p = 0; p < npairs; p++) {
        uint8_t a[DCF_MEDIUM_FRAME_LEN], b[DCF_MEDIUM_FRAME_LEN];
        if (!dcf_superpack_unpack(buf + off, a, b)) return false;
        off += DCF_SUPER_LEN;
        memcpy(frames[got++], a, DCF_MEDIUM_FRAME_LEN);
        if (got < n) memcpy(frames[got++], b, DCF_MEDIUM_FRAME_LEN);
    }
    *out_n = n;
    return true;
}

/* ══ hydra_symbols (HydraModem M-FSK; port of hydramodem/src/hydra_{profile,frame,conv,
 *    fec,interleave,crc}.c — via the canonical python/MCP/mediumlab_core.py) ══════════ */
#define DCF_HYDRA_DCF_BYTES   17u
#define DCF_HYDRA_CRC_BYTES   2u
#define DCF_HYDRA_SYNC_BITS   16u
#define DCF_HYDRA_DATA_BITS   ((DCF_HYDRA_DCF_BYTES + DCF_HYDRA_CRC_BYTES) * 8u)   /* 152 */
#define DCF_HYDRA_CONV_MEM    6u
#define DCF_HYDRA_CONV_STATES 64u
#define DCF_HYDRA_G0          0x79u   /* 0171 octal; bit6 = newest tap */
#define DCF_HYDRA_G1          0x5Bu   /* 0133 octal */
#define DCF_HYDRA_CONV_LEN    (DCF_HYDRA_DATA_BITS + DCF_HYDRA_CONV_MEM)            /* 158 */
#define DCF_HYDRA_MAX_CODED   (3u * DCF_HYDRA_DATA_BITS)                            /* 456 */
#define DCF_HYDRA_SYNC_WORD   0x2DD4u

enum { DCF_HYDRA_FEC_NONE = 0, DCF_HYDRA_FEC_REP3 = 1, DCF_HYDRA_FEC_CONV = 2 };

typedef struct {
    /* user fields (hydra_profile_default / hydra_profile_aux_cable) */
    double   sample_rate, baud, base_freq, tone_spacing, tx_gain;
    int      n_tones, preamble_syms;
    uint16_t sync_word;
    int      fec_mode;          /* DCF_HYDRA_FEC_* */
    int      interleave;        /* 0 / 1 */
    /* derived by dcf_hydra_profile_init (== hydra_profile_init) */
    int      bits_per_symbol, samples_per_symbol;
    double   lp_cut;
    size_t   data_bits, coded_bits, interleave_stride, sync_syms, data_syms, total_syms;
} dcf_hydra_profile_t;

/* NOTE: the default profile ships fec = CONV and interleave = 1 (hydra_profile.c). */
static inline void dcf_hydra_profile_default(dcf_hydra_profile_t *p) {
    memset(p, 0, sizeof *p);
    p->sample_rate = 48000.0; p->baud = 1000.0; p->n_tones = 2;
    p->base_freq = 2000.0; p->tone_spacing = 1000.0; p->preamble_syms = 24;
    p->sync_word = (uint16_t)DCF_HYDRA_SYNC_WORD;
    p->fec_mode = DCF_HYDRA_FEC_CONV; p->interleave = 1; p->tx_gain = 0.9;
}

/* Aux-cable (wired line level): 1200/2400 Hz at 1200 baud, 16-symbol preamble. */
static inline void dcf_hydra_profile_aux(dcf_hydra_profile_t *p) {
    memset(p, 0, sizeof *p);
    p->sample_rate = 48000.0; p->baud = 1200.0; p->n_tones = 2;
    p->base_freq = 1200.0; p->tone_spacing = 1200.0; p->preamble_syms = 16;
    p->sync_word = (uint16_t)DCF_HYDRA_SYNC_WORD;
    p->fec_mode = DCF_HYDRA_FEC_CONV; p->interleave = 1; p->tx_gain = 0.9;
}

/* "default" | "aux" -> 0 (profile written) / -1 unknown name. */
static inline int dcf_hydra_profile_named(dcf_hydra_profile_t *p, const char *name) {
    if (!strcmp(name, "default")) { dcf_hydra_profile_default(p); return 0; }
    if (!strcmp(name, "aux"))     { dcf_hydra_profile_aux(p);     return 0; }
    return -1;
}

static inline size_t dcf__gcd(size_t a, size_t b) {
    while (b) { size_t t = a % b; a = b; b = t; }
    return a;
}

/* Deterministic coprime stride ~ sqrt(n) (hydra_interleave.c). */
static inline size_t dcf_hydra_interleave_stride(size_t n) {
    if (n < 3) return 1;
    size_t s = 1;
    while (s * s < n) s++;
    if (s >= n) s = n - 1;
    while (s < n && dcf__gcd(s, n) != 1) s++;
    if (s >= n) {
        s = 2;
        while (s < n && dcf__gcd(s, n) != 1) s++;
        if (s >= n) s = 1;
    }
    return s;
}

static inline bool dcf__hydra_integer_multiple(double v, double baud) {
    double c = v / baud;
    double d = c - (double)(long long)(c + 0.5);
    return !(d > 1e-6 || d < -1e-6);
}

/* Fill the derived fields exactly as hydra_profile_init(). 0 ok, -1 rejected config. */
static inline int dcf_hydra_profile_init(dcf_hydra_profile_t *p) {
    if (!(p->sample_rate > 0.0) || !(p->baud > 0.0)) return -1;
    if (!(p->tone_spacing > 0.0) || !(p->base_freq > 0.0)) return -1;
    if (p->preamble_syms < 0) return -1;
    int nt = p->n_tones;
    if (nt < 2 || (nt & (nt - 1)) != 0) return -1;
    int bps = 0;
    while ((1 << bps) < nt) bps++;
    p->bits_per_symbol = bps;
    p->samples_per_symbol = (int)(p->sample_rate / p->baud + 0.5);
    if (p->samples_per_symbol < 2) return -1;
    p->lp_cut = p->baud * 0.5;
    p->interleave = p->interleave ? 1 : 0;
    p->data_bits = DCF_HYDRA_DATA_BITS;
    switch (p->fec_mode) {
        case DCF_HYDRA_FEC_NONE: p->coded_bits = DCF_HYDRA_DATA_BITS; break;
        case DCF_HYDRA_FEC_REP3: p->coded_bits = 3u * DCF_HYDRA_DATA_BITS; break;
        case DCF_HYDRA_FEC_CONV: p->coded_bits = 2u * DCF_HYDRA_CONV_LEN; break;
        default: return -1;
    }
    p->interleave_stride = p->interleave ? dcf_hydra_interleave_stride(p->coded_bits) : 1u;
    p->sync_syms = (DCF_HYDRA_SYNC_BITS + (size_t)bps - 1u) / (size_t)bps;
    p->data_syms = (p->coded_bits + (size_t)bps - 1u) / (size_t)bps;
    p->total_syms = (size_t)p->preamble_syms + p->sync_syms + p->data_syms;
    if (p->base_freq + (double)(nt - 1) * p->tone_spacing >= 0.5 * p->sample_rate) return -1;
    if (!dcf__hydra_integer_multiple(p->base_freq, p->baud)) return -1;
    if (!dcf__hydra_integer_multiple(p->tone_spacing, p->baud)) return -1;
    return 0;
}

/* ---- bit / byte / symbol helpers (MSB-first) ---- */
static inline void dcf_hydra_bytes_to_bits(const uint8_t *data, size_t n, uint8_t *bits) {
    for (size_t i = 0; i < n; i++)
        for (unsigned b = 0; b < 8; b++) bits[8 * i + b] = (uint8_t)((data[i] >> (7u - b)) & 1u);
}

/* floor(nbits/8) bytes. */
static inline void dcf_hydra_bits_to_bytes(const uint8_t *bits, size_t nbits, uint8_t *out) {
    for (size_t i = 0; i < nbits / 8u; i++) {
        unsigned v = 0;
        for (unsigned j = 0; j < 8; j++) v = (v << 1) | (bits[8 * i + j] & 1u);
        out[i] = (uint8_t)v;
    }
}

/* Pack bits into symbols MSB-first; the final symbol is zero-padded. Returns nsym. */
static inline size_t dcf_hydra_bits_to_symbols(const uint8_t *bits, size_t n, int bps,
                                               uint8_t *syms) {
    size_t b = (size_t)bps, nsym = (n + b - 1u) / b;
    for (size_t s = 0; s < nsym; s++) {
        unsigned v = 0;
        for (size_t k = 0; k < b; k++) {
            size_t idx = s * b + k;
            v = (v << 1) | ((idx < n) ? (bits[idx] & 1u) : 0u);
        }
        syms[s] = (uint8_t)v;
    }
    return nsym;
}

static inline void dcf_hydra_symbols_to_bits(const uint8_t *syms, size_t nsym, int bps,
                                             uint8_t *bits) {
    size_t b = (size_t)bps;
    for (size_t s = 0; s < nsym; s++)
        for (size_t k = 0; k < b; k++)
            bits[s * b + k] = (uint8_t)((syms[s] >> (b - 1u - k)) & 1u);
}

/* ---- FEC: K=7 r=1/2 convolutional (G0=0171, G1=0133, 6 zero tail bits) ---- */
static inline unsigned dcf__parity7(unsigned v) {
    v &= 0x7Fu;
    v ^= v >> 4;
    v ^= v >> 2;
    v ^= v >> 1;
    return v & 1u;
}

/* n message bits -> 2*(n+6) coded bits (tail-flushed to state 0). Returns 2*(n+6). */
static inline size_t dcf_hydra_conv_encode(const uint8_t *bits, size_t n, uint8_t *coded) {
    unsigned state = 0;
    size_t w = 0;
    for (size_t i = 0; i < n + DCF_HYDRA_CONV_MEM; i++) {
        unsigned b = (i < n) ? (bits[i] & 1u) : 0u;
        unsigned reg = (b << 6) | state;
        coded[w++] = (uint8_t)dcf__parity7(reg & DCF_HYDRA_G0);
        coded[w++] = (uint8_t)dcf__parity7(reg & DCF_HYDRA_G1);
        state = reg >> 1;
    }
    return w;
}

/* Hard-decision Viterbi: coded bits (2*(n+6), n+6 <= DCF_HYDRA_CONV_LEN) -> n message
 * bits. The C soft decoder's correlation metric on +/-1 hard values (1 -> +1, 0 -> -1),
 * the same state/bit iteration order and strict '>' tie rule, start state 0, traceback
 * from state 0. Returns n (message bits written) or 0 on a bad length. */
static inline size_t dcf_hydra_conv_decode_hard(const uint8_t *coded, size_t nc, uint8_t *out) {
    if (nc % 2u || nc / 2u <= DCF_HYDRA_CONV_MEM || nc / 2u > DCF_HYDRA_CONV_LEN) return 0;
    const size_t L = nc / 2u, n_msg = L - DCF_HYDRA_CONV_MEM;
    const int NEG = -0x7FFFFFFF;
    int pm[DCF_HYDRA_CONV_STATES], npm[DCF_HYDRA_CONV_STATES];
    uint8_t tb_prev[DCF_HYDRA_CONV_LEN][DCF_HYDRA_CONV_STATES];
    uint8_t tb_bit[DCF_HYDRA_CONV_LEN][DCF_HYDRA_CONV_STATES];
    for (unsigned s = 0; s < DCF_HYDRA_CONV_STATES; s++) pm[s] = NEG;
    pm[0] = 0;
    for (size_t t = 0; t < L; t++) {
        const int s0 = (coded[2 * t] & 1u) ? 1 : -1;
        const int s1 = (coded[2 * t + 1] & 1u) ? 1 : -1;
        for (unsigned s = 0; s < DCF_HYDRA_CONV_STATES; s++) {
            npm[s] = NEG;
            tb_prev[t][s] = 0;
            tb_bit[t][s] = 0;
        }
        for (unsigned s = 0; s < DCF_HYDRA_CONV_STATES; s++) {
            if (pm[s] == NEG) continue;
            for (unsigned b = 0; b < 2; b++) {
                unsigned reg = (b << 6) | s;
                unsigned o0 = dcf__parity7(reg & DCF_HYDRA_G0);
                unsigned o1 = dcf__parity7(reg & DCF_HYDRA_G1);
                unsigned ns = reg >> 1;
                int cand = pm[s] + (o0 ? s0 : -s0) + (o1 ? s1 : -s1);
                if (npm[ns] == NEG || cand > npm[ns]) {
                    npm[ns] = cand;
                    tb_prev[t][ns] = (uint8_t)s;
                    tb_bit[t][ns] = (uint8_t)b;
                }
            }
        }
        memcpy(pm, npm, sizeof pm);
    }
    uint8_t bits[DCF_HYDRA_CONV_LEN];
    unsigned s = 0;
    for (size_t t = L; t-- > 0;) {
        bits[t] = tb_bit[t][s];
        s = tb_prev[t][s];
    }
    memcpy(out, bits, n_msg);
    return n_msg;
}

/* ---- FEC: repetition-3 (hydra_fec.c) ---- */
static inline size_t dcf_hydra_rep3_encode(const uint8_t *bits, size_t n, uint8_t *coded) {
    for (size_t i = 0; i < n; i++) {
        uint8_t b = bits[i] ? 1u : 0u;
        coded[3 * i] = coded[3 * i + 1] = coded[3 * i + 2] = b;
    }
    return 3u * n;
}

static inline size_t dcf_hydra_rep3_decode(const uint8_t *coded, size_t nc, uint8_t *bits) {
    size_t n = nc / 3u;
    for (size_t i = 0; i < n; i++) {
        unsigned v = (coded[3 * i] & 1u) + (coded[3 * i + 1] & 1u) + (coded[3 * i + 2] & 1u);
        bits[i] = (uint8_t)(v >= 2u ? 1u : 0u);
    }
    return n;
}

/* ---- coprime-stride block interleaver (hydra_interleave.c) ---- */
/* TX gather: out[i] = in[(i*stride) % n]. */
static inline void dcf_hydra_interleave(const uint8_t *in, size_t n, size_t stride,
                                        uint8_t *out) {
    for (size_t i = 0; i < n; i++) out[i] = in[(i * stride) % n];
}

/* RX scatter: out[(i*stride) % n] = in[i] — the exact inverse of dcf_hydra_interleave. */
static inline void dcf_hydra_deinterleave(const uint8_t *in, size_t n, size_t stride,
                                          uint8_t *out) {
    for (size_t i = 0; i < n; i++) out[(i * stride) % n] = in[i];
}

/* The full TX symbol stream of hydra_frame_build(), one tone index (0..n_tones-1) per
 * uint8_t:
 *   [preamble: preamble_syms alternating tone 0 / tone n_tones-1, starting with 0]
 *   [sync_word bits -> symbols]
 *   [interleave?(fec(frame17 || CRC16be(frame17))) -> symbols]
 * The profile must be dcf_hydra_profile_init()-ed. Returns 0 ok (*n = total_syms), -1 if
 * cap < total_syms or the profile is unusable (n_tones > 256). */
static inline int dcf_hydra_symbols_encode(const dcf_hydra_profile_t *p, const uint8_t *frame17,
                                           uint8_t *out, size_t cap, size_t *n) {
    if (cap < p->total_syms || p->n_tones > 256 || p->bits_per_symbol < 1) return -1;
    uint8_t field[DCF_HYDRA_DCF_BYTES + DCF_HYDRA_CRC_BYTES];
    uint8_t data_bits[DCF_HYDRA_DATA_BITS];
    uint8_t coded[DCF_HYDRA_MAX_CODED], il[DCF_HYDRA_MAX_CODED];
    memcpy(field, frame17, DCF_HYDRA_DCF_BYTES);
    uint16_t crc = dcf_crc16(frame17, DCF_HYDRA_DCF_BYTES);
    field[17] = (uint8_t)(crc >> 8);
    field[18] = (uint8_t)crc;
    dcf_hydra_bytes_to_bits(field, sizeof field, data_bits);
    size_t nc;
    switch (p->fec_mode) {
        case DCF_HYDRA_FEC_NONE: memcpy(coded, data_bits, DCF_HYDRA_DATA_BITS);
                                 nc = DCF_HYDRA_DATA_BITS; break;
        case DCF_HYDRA_FEC_REP3: nc = dcf_hydra_rep3_encode(data_bits, DCF_HYDRA_DATA_BITS, coded); break;
        case DCF_HYDRA_FEC_CONV: nc = dcf_hydra_conv_encode(data_bits, DCF_HYDRA_DATA_BITS, coded); break;
        default: return -1;
    }
    if (nc != p->coded_bits) return -1;
    const uint8_t *src = coded;
    if (p->interleave) {
        dcf_hydra_interleave(coded, nc, p->interleave_stride, il);
        src = il;
    }
    size_t w = 0;
    for (int k = 0; k < p->preamble_syms; k++)
        out[w++] = (uint8_t)((k & 1) ? (p->n_tones - 1) : 0);
    uint8_t sb[2] = { (uint8_t)(p->sync_word >> 8), (uint8_t)p->sync_word };
    uint8_t sync_bits[DCF_HYDRA_SYNC_BITS];
    dcf_hydra_bytes_to_bits(sb, 2, sync_bits);
    w += dcf_hydra_bits_to_symbols(sync_bits, DCF_HYDRA_SYNC_BITS, p->bits_per_symbol, out + w);
    w += dcf_hydra_bits_to_symbols(src, nc, p->bits_per_symbol, out + w);
    if (w != p->total_syms) return -1;
    *n = w;
    return 0;
}

/* Inverse with hard decisions: strip the preamble by count, check the 16 sync bits,
 * deinterleave, FEC-decode (none / rep3 majority / hard Viterbi), then the CRC-16 check.
 * Returns 0 and writes out17 on success; -1 on a wrong length, a symbol >= n_tones, a
 * sync mismatch, or a CRC mismatch. */
static inline int dcf_hydra_symbols_decode(const dcf_hydra_profile_t *p, const uint8_t *syms,
                                           size_t n, uint8_t *out17) {
    if (n != p->total_syms || p->bits_per_symbol < 1 || p->bits_per_symbol > 8) return -1;
    if (p->coded_bits > DCF_HYDRA_MAX_CODED) return -1;
    for (size_t i = 0; i < n; i++) if ((int)syms[i] >= p->n_tones) return -1;
    const size_t bps = (size_t)p->bits_per_symbol;
    size_t off = (size_t)p->preamble_syms;
    /* sync: sync_syms*bps <= 16 + 7 bits */
    uint8_t sbits[DCF_HYDRA_SYNC_BITS + 8];
    dcf_hydra_symbols_to_bits(syms + off, p->sync_syms, p->bits_per_symbol, sbits);
    uint8_t sb[2] = { (uint8_t)(p->sync_word >> 8), (uint8_t)p->sync_word };
    uint8_t want[DCF_HYDRA_SYNC_BITS];
    dcf_hydra_bytes_to_bits(sb, 2, want);
    if (memcmp(sbits, want, DCF_HYDRA_SYNC_BITS) != 0) return -1;
    off += p->sync_syms;
    uint8_t cbits[DCF_HYDRA_MAX_CODED + 8], coded[DCF_HYDRA_MAX_CODED];
    if (p->data_syms * bps > sizeof cbits) return -1;
    dcf_hydra_symbols_to_bits(syms + off, p->data_syms, p->bits_per_symbol, cbits);
    const size_t nc = p->coded_bits;
    if (p->interleave) dcf_hydra_deinterleave(cbits, nc, p->interleave_stride, coded);
    else memcpy(coded, cbits, nc);
    uint8_t data[DCF_HYDRA_CONV_LEN];
    switch (p->fec_mode) {
        case DCF_HYDRA_FEC_NONE: memcpy(data, coded, DCF_HYDRA_DATA_BITS); break;
        case DCF_HYDRA_FEC_REP3: dcf_hydra_rep3_decode(coded, nc, data); break;
        case DCF_HYDRA_FEC_CONV:
            if (dcf_hydra_conv_decode_hard(coded, nc, data) != DCF_HYDRA_DATA_BITS) return -1;
            break;
        default: return -1;
    }
    uint8_t field[DCF_HYDRA_DCF_BYTES + DCF_HYDRA_CRC_BYTES];
    dcf_hydra_bits_to_bytes(data, DCF_HYDRA_DATA_BITS, field);
    uint16_t crc = (uint16_t)(((unsigned)field[17] << 8) | field[18]);
    if (dcf_crc16(field, DCF_HYDRA_DCF_BYTES) != crc) return -1;
    memcpy(out17, field, DCF_HYDRA_DCF_BYTES);
    return 0;
}

/* Symbols <-> a string of lowercase hex digits, one per symbol (n_tones <= 16). */
static inline void dcf_hydra_symbols_to_str(const uint8_t *syms, size_t n, char *out) {
    static const char d[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) out[i] = d[syms[i] & 0xFu];
    out[n] = '\0';
}

/* Returns the symbol count, or (size_t)-1 on a non-hex character / cap overflow. */
static inline size_t dcf_hydra_symbols_from_str(const char *s, uint8_t *syms, size_t cap) {
    size_t n = 0;
    for (; s[n]; n++) {
        int v = dcf__hexval(s[n]);
        if (v < 0 || n >= cap) return (size_t)-1;
        syms[n] = (uint8_t)v;
    }
    return n;
}

/* ══ afsk_bits (python/modem/acoustic_frame.py: preamble + 0x7E + frame+crc8 | RS16 +
 *    16-bit postamble) ════════════════════════════════════════════════════════════════ */
#define DCF_AFSK_SYNC           0x7Eu
#define DCF_AFSK_POSTAMBLE_BITS 16u
#define DCF_AFSK_NPARITY        16u
#define DCF_AFSK_MAX_BITS       (240u + 8u + 8u * (17u + DCF_AFSK_NPARITY) + DCF_AFSK_POSTAMBLE_BITS)

typedef struct { const char *name; double mark, space; int baud, preamble_bits; } dcf_afsk_profile_t;

static const dcf_afsk_profile_t DCF_AFSK_PROFILES[3] = {
    {"standard",  1200.0, 2200.0,  300,  80},
    {"handheld",  1200.0, 1800.0,  300, 240},
    {"aux-cable", 1000.0, 1500.0, 1200,  16},
};

/* Profile by name, or NULL. */
static inline const dcf_afsk_profile_t *dcf_afsk_profile(const char *name) {
    for (size_t i = 0; i < 3; i++)
        if (!strcmp(DCF_AFSK_PROFILES[i].name, name)) return &DCF_AFSK_PROFILES[i];
    return NULL;
}

/* Poly-0x31 CRC-8 (MSB-first, init 0x00, non-reflected) — the deployed Faust modem check. */
static inline uint8_t dcf_afsk_crc8(const uint8_t *d, size_t n) {
    unsigned crc = 0;
    for (size_t i = 0; i < n; i++) {
        crc ^= d[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x80u) ? (((crc << 1) ^ 0x31u) & 0xFFu) : ((crc << 1) & 0xFFu);
    }
    return (uint8_t)crc;
}

/* On-air bits (one 0/1 per uint8_t) for a 17-byte frame. Returns the bit count, or 0 if
 * cap is too small. fec=false: frame + crc8; fec=true: the RS(17+16) codeword. */
static inline size_t dcf_afsk_bits_encode(const uint8_t *frame17, int preamble_bits, bool fec,
                                          uint8_t *bits, size_t cap) {
    uint8_t payload[17 + DCF_AFSK_NPARITY];
    size_t plen;
    if (preamble_bits < 0) return 0;
    if (fec) {
        dcf_fec_encode(frame17, 17, (uint8_t)DCF_AFSK_NPARITY, payload);
        plen = 17 + DCF_AFSK_NPARITY;
    } else {
        memcpy(payload, frame17, 17);
        payload[17] = dcf_afsk_crc8(frame17, 17);
        plen = 18;
    }
    size_t need = (size_t)preamble_bits + 8u + 8u * plen + DCF_AFSK_POSTAMBLE_BITS;
    if (need > cap) return 0;
    size_t w = 0;
    for (size_t i = 0; i < (size_t)preamble_bits; i++) bits[w++] = (uint8_t)(i % 2u);
    uint8_t sync = (uint8_t)DCF_AFSK_SYNC;
    dcf_hydra_bytes_to_bits(&sync, 1, bits + w);
    w += 8;
    dcf_hydra_bytes_to_bits(payload, plen, bits + w);
    w += 8u * plen;
    for (size_t i = 0; i < DCF_AFSK_POSTAMBLE_BITS; i++) bits[w++] = (uint8_t)(i % 2u);
    return w;
}

/* Index of the first bit AFTER the 0x7E sync word (bit-level search), or -1. */
static inline long dcf_afsk_find_sync(const uint8_t *bits, size_t n) {
    static const uint8_t pat[8] = {0, 1, 1, 1, 1, 1, 1, 0};
    for (size_t i = 0; i + 8 <= n; i++) {
        size_t k = 0;
        while (k < 8 && (bits[i + k] & 1u) == pat[k]) k++;
        if (k == 8) return (long)(i + 8);
    }
    return -1;
}

/* Inverse (acoustic_frame.decode_bits): 0x7E search, then crc8 check or RS decode.
 * Returns 0 and writes out17 on success, -1 otherwise. Not gated. */
static inline int dcf_afsk_bits_decode(const uint8_t *bits, size_t n, bool fec, uint8_t *out17) {
    long pos = dcf_afsk_find_sync(bits, n);
    if (pos < 0) return -1;
    size_t avail = (n - (size_t)pos) / 8u;          /* whole bytes after the sync */
    size_t need = fec ? (17u + DCF_AFSK_NPARITY) : 18u;
    if (avail < need) return -1;
    uint8_t data[17 + DCF_AFSK_NPARITY];
    dcf_hydra_bits_to_bytes(bits + pos, need * 8u, data);
    if (fec) {
        if (dcf_fec_decode(data, need, (uint8_t)DCF_AFSK_NPARITY, 17) < 0) return -1;
    } else if (dcf_afsk_crc8(data, 17) != data[17]) {
        return -1;
    }
    memcpy(out17, data, 17);
    return 0;
}

/* "0"/"1" string helpers. from_str returns the bit count or (size_t)-1 on a bad char. */
static inline void dcf_afsk_bits_to_str(const uint8_t *bits, size_t n, char *out) {
    for (size_t i = 0; i < n; i++) out[i] = (bits[i] & 1u) ? '1' : '0';
    out[n] = '\0';
}

static inline size_t dcf_afsk_bits_from_str(const char *s, uint8_t *bits, size_t cap) {
    size_t n = 0;
    for (; s[n]; n++) {
        if ((s[n] != '0' && s[n] != '1') || n >= cap) return (size_t)-1;
        bits[n] = (uint8_t)(s[n] == '1');
    }
    return n;
}

#endif /* DCF_DEMOD_MEDIUM_H */
