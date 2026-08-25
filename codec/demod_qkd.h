// SPDX-License-Identifier: LGPL-3.0-only
/*
 * transport/demod_qkd.h — DCF-QKD: ETSI GS QKD 014 key-ID beacon over DeModFrame
 * DeMoD LLC | LGPL-3.0
 *
 * The key-ID beacon is an ADAPTER over the 17-byte DeModFrame quantum (demod_frame.h),
 * not a new wire format — exactly like DCF-Audio (demod_audio.h) and DCF-Text
 * (demod_text.h).  One ETSI 014 key_ID is serialised into exactly 4 ordinary CTRL
 * frames.  This L2 framing is byte-deterministic across C/Rust/Python — it is pinned by
 * Documentation/qkd_vectors.json.  See DCF_QKD_SPEC.md.
 *
 * ("Wire quantum" is quantum as in *quanta* — an indivisible unit.  Nothing in this
 * header is quantum: a key_ID is an opaque 128-bit identifier minted by external KME
 * hardware.)
 *
 * WHY THIS EXISTS: ETSI GS QKD 014 deliberately leaves the transport of key_ID from
 * master SAE to slave SAE OUT OF SCOPE, so every deployment invents a carrier.  The
 * arithmetic makes the quantum an unusually good fit:
 *     key_ID = UUID = 128 bits = 16 bytes = exactly 4 x 4-byte DeModFrame payloads
 *
 * WHY NO DESCRIPTOR: every other fragmenting adapter burns frag_idx 0 on a length
 * descriptor.  A key_ID is ALWAYS 16 bytes, so length and frag_total are known a
 * priori.  All four fragments are data.  Normative design property, not an omission.
 *
 * L2 framing (all frames version=1, type=CTRL(3); see WIRE_QUANTUM_SPEC.md):
 *   seq (u16) = epoch[15:2] (14 bits, 0..16383) | frag_idx[1:0] (2 bits, 0..3)
 *   frag_idx 0..3 data : payload = key_id_bytes[idx*4 .. +4]   (no padding, ever)
 *   frag_total = 4 (constant)
 *
 * The 14:2 split is unique among the CTRL(3) adapters (audio 11:5, cue 9:7, snake
 * 5:11).  As everywhere else there is no in-band adapter tag: run exactly one
 * reassembler per dst channel.
 *
 * EXPORT / SECURITY (normative): the wire carries ONLY the key_ID, a non-secret
 * identifier.  Key material MUST NOT be placed in a DeModFrame payload.  This header
 * never sees a key.  See Documentation/DCF_QKD_SPEC.md.
 */
#pragma once
#include "demod_frame.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* ── L2 constants ──────────────────────────────────────────────────────────── */
#define DCF_QKD_FRAG_BITS     2u
#define DCF_QKD_FRAG_MASK     0x3u                      /* low 2 bits of seq        */
#define DCF_QKD_FRAGS         4u                        /* always exactly 4          */
#define DCF_QKD_KEY_ID_BYTES  16u                       /* 128-bit UUID              */
#define DCF_QKD_MAX_EPOCH     16383u                    /* 14-bit epoch              */

/* Map a channel/passphrase to a 16-bit rendezvous dst (crc16, same hash as the rest of
 * the repo).  NULL/"" => broadcast (0xFFFF). */
static inline uint16_t dcf_qkd_channel_id(const char *name) {
    if (!name || name[0] == '\0') return 0xFFFFu;
    return dcf_crc16((const uint8_t *)name, strlen(name));
}

/* ── L2: packetize ─────────────────────────────────────────────────────────── */
/*
 * Serialise one key_ID (16 raw bytes) into exactly DCF_QKD_FRAGS DeModFrame CTRL
 * frames.  `frames` must hold at least DCF_QKD_FRAGS rows of DCF_FRAME_SIZE bytes.
 * Returns false on invalid args; on success *out_n == DCF_QKD_FRAGS.
 */
static inline bool dcf_qkd_packetize(const uint8_t key_id[DCF_QKD_KEY_ID_BYTES],
                                     uint16_t epoch, uint32_t ts_us,
                                     uint16_t src, uint16_t dst,
                                     uint8_t frames[][DCF_FRAME_SIZE],
                                     size_t max_frames, size_t *out_n) {
    if (!key_id)                     return false;
    if (epoch > DCF_QKD_MAX_EPOCH)   return false;
    if (max_frames < DCF_QKD_FRAGS)  return false;

    dcf_frame_t f;
    f.version = 1u;
    f.type    = DCF_TYPE_CTRL;
    f.src_id  = src;
    f.dst_id  = dst;
    f.timestamp_us = ts_us;

    for (uint16_t idx = 0; idx < DCF_QKD_FRAGS; idx++) {
        f.seq = (uint16_t)(((uint16_t)epoch << DCF_QKD_FRAG_BITS) | idx);
        memcpy(f.payload, key_id + (size_t)idx * 4u, 4);
        dcf_frame_encode(&f, frames[idx]);
    }

    *out_n = DCF_QKD_FRAGS;
    return true;
}

/* ── L2: reassembler ───────────────────────────────────────────────────────── */
#ifndef DCF_QKD_REASM_SLOTS
#define DCF_QKD_REASM_SLOTS 8   /* concurrent in-flight beacons (override for embedded) */
#endif

typedef struct {
    uint16_t epoch;
    uint32_t ts_us;
    uint16_t src;
    uint16_t dst;
    uint8_t  key_id[DCF_QKD_KEY_ID_BYTES];
} dcf_qkd_key_t;

typedef struct {
    uint16_t epoch;
    uint16_t src;
    uint16_t dst;
} dcf_qkd_lost_t;

typedef struct {
    bool     in_use;
    uint32_t age;                              /* insertion order, for oldest-first evict */
    uint16_t epoch;
    uint16_t src;
    uint16_t dst;
    uint32_t ts_us;
    uint8_t  present;                          /* bit k = fragment k received (k in 0..3) */
    uint8_t  data[DCF_QKD_KEY_ID_BYTES];
} dcf_qkd_slot_t;

typedef struct {
    dcf_qkd_slot_t slots[DCF_QKD_REASM_SLOTS];
    int32_t        accept_dst;                 /* -1 => accept every channel              */
    uint32_t       clock;                      /* monotonic insertion counter             */
} dcf_qkd_reasm_t;

typedef enum {
    DCF_QKD_REASM_NONE    = 0,  /* frame accepted, beacon not yet complete           */
    DCF_QKD_REASM_KEY     = 1,  /* a complete key_ID is in *out_key                  */
    DCF_QKD_REASM_IGNORED = 2,  /* not a CTRL beacon frame, a duplicate, or filtered */
} dcf_qkd_reasm_status_t;

/* accept_dst: -1 accepts every channel; otherwise only that dst plus DCF_BROADCAST. */
static inline void dcf_qkd_reasm_init(dcf_qkd_reasm_t *r, int32_t accept_dst) {
    memset(r, 0, sizeof(*r));
    r->accept_dst = accept_dst;
}

static inline dcf_qkd_slot_t *dcf__qkd_slot_for(dcf_qkd_reasm_t *r, uint16_t src,
                                                uint16_t dst, uint16_t epoch) {
    dcf_qkd_slot_t *free_slot = NULL;
    dcf_qkd_slot_t *oldest    = &r->slots[0];
    for (size_t i = 0; i < DCF_QKD_REASM_SLOTS; i++) {
        dcf_qkd_slot_t *s = &r->slots[i];
        if (s->in_use && s->epoch == epoch && s->src == src && s->dst == dst) return s;
        if (!s->in_use && !free_slot) free_slot = s;
        if (s->in_use && s->age < oldest->age) oldest = s;
    }
    /* Table full => evict the oldest incomplete beacon so a lossy link cannot wedge
     * the reassembler.  NOTE: the Python reference additionally *reports* an evicted
     * beacon as lost; that reporting is a runtime concern and is not covered by
     * qkd_vectors.json (no vector stream exceeds the slot table). */
    dcf_qkd_slot_t *s = free_slot ? free_slot : oldest;
    memset(s, 0, sizeof(*s));
    s->in_use = true;
    s->epoch  = epoch;
    s->src    = src;
    s->dst    = dst;
    s->age    = ++r->clock;
    return s;
}

/*
 * Push one 17-byte frame.  On DCF_QKD_REASM_KEY, *out_key holds the assembled key_ID.
 * Duplicates, non-CTRL frames and frames for a filtered dst are ignored.
 */
static inline dcf_qkd_reasm_status_t dcf_qkd_reasm_push(dcf_qkd_reasm_t *r,
                                                        const uint8_t frame[DCF_FRAME_SIZE],
                                                        dcf_qkd_key_t *out_key) {
    dcf_frame_t d;
    if (!dcf_frame_decode(frame, &d)) return DCF_QKD_REASM_IGNORED;
    if (d.type != DCF_TYPE_CTRL)      return DCF_QKD_REASM_IGNORED;
    if (r->accept_dst >= 0 && d.dst_id != (uint16_t)r->accept_dst
        && d.dst_id != DCF_BROADCAST) return DCF_QKD_REASM_IGNORED;

    uint16_t epoch    = (uint16_t)(d.seq >> DCF_QKD_FRAG_BITS);
    uint16_t frag_idx = (uint16_t)(d.seq & DCF_QKD_FRAG_MASK);

    dcf_qkd_slot_t *s = dcf__qkd_slot_for(r, d.src_id, d.dst_id, epoch);
    s->ts_us = d.timestamp_us;
    if (!((s->present >> frag_idx) & 1u)) {
        s->present |= (uint8_t)(1u << frag_idx);
        memcpy(s->data + (size_t)frag_idx * 4u, d.payload, 4);
    }

    if (s->present != 0x0Fu) return DCF_QKD_REASM_NONE;
    out_key->epoch = s->epoch;
    out_key->ts_us = s->ts_us;
    out_key->src   = s->src;
    out_key->dst   = s->dst;
    memcpy(out_key->key_id, s->data, DCF_QKD_KEY_ID_BYTES);
    memset(s, 0, sizeof(*s));   /* frees the slot */
    return DCF_QKD_REASM_KEY;
}

/*
 * Report every still-incomplete beacon as lost (ascending src, dst, epoch — the same
 * order the Python reference uses) and clear the reassembler.  Returns the count; up
 * to `max` entries are written to `lost`.
 */
static inline size_t dcf_qkd_reasm_finalize(dcf_qkd_reasm_t *r,
                                            dcf_qkd_lost_t *lost, size_t max) {
    size_t n = 0;
    for (size_t i = 0; i < DCF_QKD_REASM_SLOTS; i++) {
        if (r->slots[i].in_use && n < max) {
            lost[n].epoch = r->slots[i].epoch;
            lost[n].src   = r->slots[i].src;
            lost[n].dst   = r->slots[i].dst;
            n++;
        }
    }
    for (size_t i = 1; i < n; i++) {           /* insertion sort by (src, dst, epoch) */
        dcf_qkd_lost_t v = lost[i];
        size_t j = i;
        while (j > 0 && ((lost[j - 1].src > v.src)
                         || (lost[j - 1].src == v.src && lost[j - 1].dst > v.dst)
                         || (lost[j - 1].src == v.src && lost[j - 1].dst == v.dst
                             && lost[j - 1].epoch > v.epoch))) {
            lost[j] = lost[j - 1];
            j--;
        }
        lost[j] = v;
    }
    int32_t keep = r->accept_dst;
    dcf_qkd_reasm_init(r, keep);
    return n;
}
