// SPDX-License-Identifier: LGPL-3.0-only
/*
 * demod_hydrapack.h — HydraPack: universal serialization for Punctim (C)
 * DeMoD LLC | LGPL-3.0
 *
 * HydraPack is the single point at which an abstract value becomes either a
 * short burst of 4-byte quanta (for the quantum / adapter path) or a contiguous
 * byte buffer (for the DCF-Pipe data plane).  It never invents a new wire format:
 * the 17-byte DeModFrame remains the only certified quantum, and Pipe control
 * messages remain ordinary frame payloads.
 *
 * Two emission planes (pure, deterministic given schema + value):
 *   Quantum — packed_size <= threshold (default 120 B)
 *     -> ordered list of [4] arrays, ready for adapter framing / SuperPack.
 *   Pipe    — packed_size >  threshold
 *     -> contiguous byte buffer + (schema_id, version, FNV-1a checksum).
 *
 * Bit-packing is big-endian (MSB-first), zero-padded to a byte boundary.
 * Byte-certified across C/Rust/Python by Documentation/hydrapack_vectors.json.
 * The 246-vector wire certificate is untouched.
 *
 * Quantum descriptor (4 bytes, byte-aligned, big-endian, frag 0 of multi-quantum):
 *   B0  schema_id_hi
 *   B1  schema_id_lo
 *   B2  (schema_version << 4) | flags   (4-bit version nibble, 4-bit opaque flags)
 *   B3  payload_byte_len                (the packed-data byte length, 0..255)
 *
 * OpenPipe (17 bytes): 14-byte OPEN (from demod_pipe.h) + 3 bytes:
 *   B14  schema_id_hi
 *   B15  schema_id_lo
 *   B16  (schema_version << 4) | flags
 */
#ifndef DCF_DEMOD_HYDRAPACK_H
#define DCF_DEMOD_HYDRAPACK_H

#include "demod_pipe.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DCF_HP_VERSION         1u
#define DCF_HP_THRESHOLD       120u
#define DCF_HP_QUANTUM_LEN     4u
#define DCF_HP_DESC_MAX        255u
#define DCF_HP_OPENPIPE_LEN    17u
#define DCF_HP_MAX_FIELDS      16u
#define DCF_HP_MAX_SUB         8u

/* ── Field kinds ──────────────────────────────────────────────────────────── */
enum {
    DCF_HP_U      = 0,
    DCF_HP_I      = 1,
    DCF_HP_BOOL   = 2,
    DCF_HP_ENUM   = 3,
    DCF_HP_BITS   = 4,
    DCF_HP_STRUCT = 5,
};

typedef struct {
    const char    *name;
    uint8_t        kind;
    uint8_t        width;
    const void    *sub_fields;   /* points to dcf_hp_field_t[N] when kind == STRUCT */
    uint8_t        n_sub;
} dcf_hp_field_t;

typedef struct {
    uint16_t             schema_id;
    uint8_t               version;
    const dcf_hp_field_t *fields;
    uint8_t               n_fields;
} dcf_hp_schema_t;

/* ── Value model (flattened int + bool arrays, struct sub-fields inlined) ─── */
typedef struct {
    int32_t  ints[DCF_HP_MAX_FIELDS];
    uint8_t  bools[DCF_HP_MAX_FIELDS];   /* 0 or 1 */
    uint8_t  n;                           /* number of leaf fields */
} dcf_hp_value_t;

/* ── Bit packer (big-endian, MSB-first) ───────────────────────────────────── */
typedef struct {
    uint8_t  *buf;
    size_t    cap;
    size_t    bitpos;
} dcf_hp_bitw_t;

typedef struct {
    const uint8_t *buf;
    size_t          len;
    size_t          bitpos;
} dcf_hp_bitr_t;

static inline void dcf_hp_bitw_init(dcf_hp_bitw_t *w, uint8_t *buf, size_t cap) {
    w->buf = buf; w->cap = cap; w->bitpos = 0;
    if (cap) memset(buf, 0, cap);
}

static inline void dcf_hp_bitw_write(dcf_hp_bitw_t *w, int64_t value, uint8_t nbits) {
    if (nbits == 0) return;
    for (int b = nbits - 1; b >= 0; b--) {
        uint64_t bit = ((uint64_t)value >> b) & 1;
        size_t byte_idx = w->bitpos >> 3;
        uint8_t bit_idx = 7 - (uint8_t)(w->bitpos & 7);
        if (byte_idx < w->cap)
            w->buf[byte_idx] |= (uint8_t)(bit << bit_idx);
        w->bitpos++;
    }
}

static inline size_t dcf_hp_bitw_bytes(const dcf_hp_bitw_t *w) {
    return (w->bitpos + 7) / 8;
}

static inline void dcf_hp_bitr_init(dcf_hp_bitr_t *r, const uint8_t *buf, size_t len) {
    r->buf = buf; r->len = len; r->bitpos = 0;
}

static inline int64_t dcf_hp_bitr_read(dcf_hp_bitr_t *r, uint8_t nbits, bool signed_) {
    if (nbits == 0) return 0;
    int64_t value = 0;
    for (int b = nbits - 1; b >= 0; b--) {
        size_t byte_idx = r->bitpos >> 3;
        uint8_t bit_idx = 7 - (uint8_t)(r->bitpos & 7);
        int bit = (byte_idx < r->len) ? ((r->buf[byte_idx] >> bit_idx) & 1) : 0;
        value = (value << 1) | bit;
        r->bitpos++;
    }
    if (signed_ && nbits > 0 && (value >> (nbits - 1)) & 1)
        value -= (int64_t)1 << nbits;
    return value;
}

/* ── Helpers: count leaves, pack/unpack a field ───────────────────────────── */
static inline uint8_t dcf_hp__leaves(const dcf_hp_field_t *f) {
    if (f->kind == DCF_HP_STRUCT) {
        const dcf_hp_field_t *subs = (const dcf_hp_field_t *)f->sub_fields;
        uint8_t n = 0;
        for (uint8_t i = 0; i < f->n_sub; i++)
            n += dcf_hp__leaves(&subs[i]);
        return n;
    }
    return 1;
}

static inline uint16_t dcf_hp__field_bits(const dcf_hp_field_t *f) {
    if (f->kind == DCF_HP_BOOL) return 1;
    if (f->kind == DCF_HP_STRUCT) {
        const dcf_hp_field_t *subs = (const dcf_hp_field_t *)f->sub_fields;
        uint16_t total = 0;
        for (uint8_t i = 0; i < f->n_sub; i++)
            total += dcf_hp__field_bits(&subs[i]);
        return total;
    }
    return f->width;
}

static inline uint16_t dcf_hp_packed_bits(const dcf_hp_schema_t *s) {
    uint16_t total = 0;
    for (uint8_t i = 0; i < s->n_fields; i++)
        total += dcf_hp__field_bits(&s->fields[i]);
    return total;
}

static inline size_t dcf_hp_packed_size(const dcf_hp_schema_t *s) {
    return (dcf_hp_packed_bits(s) + 7) / 8;
}

static inline void dcf_hp__pack_field(const dcf_hp_field_t *f,
                                       const dcf_hp_value_t *v, size_t *idx, size_t *bidx,
                                       dcf_hp_bitw_t *w) {
    if (f->kind == DCF_HP_STRUCT) {
        const dcf_hp_field_t *subs = (const dcf_hp_field_t *)f->sub_fields;
        for (uint8_t i = 0; i < f->n_sub; i++)
            dcf_hp__pack_field(&subs[i], v, idx, bidx, w);
    } else if (f->kind == DCF_HP_BOOL) {
        dcf_hp_bitw_write(w, v->bools[*bidx], 1);
        (*bidx)++; (*idx)++;
    } else {
        dcf_hp_bitw_write(w, v->ints[*idx], f->width);
        (*idx)++;
    }
}

static inline void dcf_hp__unpack_field(const dcf_hp_field_t *f,
                                         dcf_hp_value_t *v, size_t *idx, size_t *bidx,
                                         dcf_hp_bitr_t *r) {
    if (f->kind == DCF_HP_STRUCT) {
        const dcf_hp_field_t *subs = (const dcf_hp_field_t *)f->sub_fields;
        for (uint8_t i = 0; i < f->n_sub; i++)
            dcf_hp__unpack_field(&subs[i], v, idx, bidx, r);
    } else if (f->kind == DCF_HP_BOOL) {
        v->bools[*bidx] = (uint8_t)dcf_hp_bitr_read(r, 1, false);
        (*bidx)++; (*idx)++;
    } else {
        v->ints[*idx] = (int32_t)dcf_hp_bitr_read(r, f->width, f->kind == DCF_HP_I);
        (*idx)++;
    }
}

/* ── Value pack/unpack ────────────────────────────────────────────────────── */
static inline size_t dcf_hp_pack_value(const dcf_hp_schema_t *s,
                                        const dcf_hp_value_t *v, uint8_t *out, size_t cap) {
    dcf_hp_bitw_t w;
    dcf_hp_bitw_init(&w, out, cap);
    size_t idx = 0, bidx = 0;
    for (uint8_t i = 0; i < s->n_fields; i++)
        dcf_hp__pack_field(&s->fields[i], v, &idx, &bidx, &w);
    return dcf_hp_bitw_bytes(&w);
}

static inline void dcf_hp_unpack_value(const dcf_hp_schema_t *s,
                                        const uint8_t *buf, size_t len,
                                        dcf_hp_value_t *out) {
    dcf_hp_bitr_t r;
    dcf_hp_bitr_init(&r, buf, len);
    memset(out, 0, sizeof(*out));
    out->n = 0;
    size_t idx = 0, bidx = 0;
    for (uint8_t i = 0; i < s->n_fields; i++)
        dcf_hp__unpack_field(&s->fields[i], out, &idx, &bidx, &r);
    out->n = (uint8_t)(idx > bidx ? idx : bidx);
}

/* ── Quantum path ─────────────────────────────────────────────────────────── */
/*
 * Pack a value into a list of 4-byte quanta.
 * `quanta` must hold at least (1 + ceil(packed_size/4)) rows of 4 bytes.
 * Returns false on invalid args or insufficient room; on success *out_n is the
 * number of quanta (descriptor first for multi-quantum, then data in order).
 */
static inline bool dcf_hp_pack_quantum(const dcf_hp_schema_t *s,
                                        const dcf_hp_value_t *v, uint8_t flags,
                                        bool force_descriptor,
                                        uint8_t quanta[][DCF_HP_QUANTUM_LEN],
                                        size_t max_quanta, size_t *out_n) {
    uint8_t buf[DCF_HP_DESC_MAX + 4];
    size_t packed_len = dcf_hp_pack_value(s, v, buf, sizeof(buf));
    if (packed_len == 0 && dcf_hp_packed_bits(s) > 0) return false;

    if (packed_len <= DCF_HP_QUANTUM_LEN && !force_descriptor) {
        if (max_quanta < 1) return false;
        memset(quanta[0], 0, DCF_HP_QUANTUM_LEN);
        memcpy(quanta[0], buf, packed_len);
        *out_n = 1;
        return true;
    }
    if (packed_len > DCF_HP_DESC_MAX) return false;

    size_t data_quanta = (packed_len + 3) / 4;
    size_t need = 1 + data_quanta;
    if (need > max_quanta) return false;

    /* descriptor */
    quanta[0][0] = (uint8_t)(s->schema_id >> 8);
    quanta[0][1] = (uint8_t)(s->schema_id & 0xFF);
    quanta[0][2] = (uint8_t)(((s->version & 0x0F) << 4) | (flags & 0x0F));
    quanta[0][3] = (uint8_t)packed_len;

    /* data quanta */
    for (size_t i = 0; i < data_quanta; i++) {
        memset(quanta[1 + i], 0, DCF_HP_QUANTUM_LEN);
        size_t off = i * DCF_HP_QUANTUM_LEN;
        size_t n = packed_len - off;
        if (n > DCF_HP_QUANTUM_LEN) n = DCF_HP_QUANTUM_LEN;
        memcpy(quanta[1 + i], buf + off, n);
    }
    *out_n = need;
    return true;
}

/* Unpack a single-quantum message (no descriptor). Caller provides the schema. */
static inline void dcf_hp_unpack_quantum_single(const uint8_t quanta[][DCF_HP_QUANTUM_LEN],
                                                 const dcf_hp_schema_t *s,
                                                 dcf_hp_value_t *out) {
    dcf_hp_unpack_value(s, quanta[0], DCF_HP_QUANTUM_LEN, out);
}

/* Unpack a multi-quantum message. Reads descriptor for schema_id/version/flags/len,
 * but the caller must pass the schema (looking up the registry is the caller's job).
 * Returns false if the descriptor's payload_len exceeds available data. */
static inline bool dcf_hp_unpack_quantum_multi(const uint8_t quanta[][DCF_HP_QUANTUM_LEN],
                                                size_t n_quanta,
                                                const dcf_hp_schema_t *s,
                                                dcf_hp_value_t *out,
                                                uint8_t *out_flags) {
    if (n_quanta < 1) return false;
    const uint8_t *desc = quanta[0];
    uint8_t payload_len = desc[3];
    if (out_flags) *out_flags = desc[2] & 0x0F;

    uint8_t buf[DCF_HP_DESC_MAX + 4];
    size_t off = 0;
    for (size_t i = 1; i < n_quanta && off < payload_len; i++) {
        size_t n = payload_len - off;
        if (n > DCF_HP_QUANTUM_LEN) n = DCF_HP_QUANTUM_LEN;
        memcpy(buf + off, quanta[i], n);
        off += n;
    }
    if (off < payload_len) return false;
    dcf_hp_unpack_value(s, buf, payload_len, out);
    return true;
}

/* ── Pipe path ────────────────────────────────────────────────────────────── */
static inline size_t dcf_hp_pack_pipe(const dcf_hp_schema_t *s,
                                       const dcf_hp_value_t *v,
                                       uint8_t *buf, size_t cap,
                                       uint32_t *out_checksum) {
    size_t len = dcf_hp_pack_value(s, v, buf, cap);
    if (out_checksum) *out_checksum = dcf_pipe_fnv1a32(buf, len);
    return len;
}

static inline void dcf_hp_unpack_pipe(const dcf_hp_schema_t *s,
                                       const uint8_t *buf, size_t len,
                                       dcf_hp_value_t *out) {
    dcf_hp_unpack_value(s, buf, len, out);
}

/* ── OpenPipe (17 bytes: 14-byte OPEN + 3-byte schema extension) ──────────── */
static inline size_t dcf_hp_pack_openpipe(uint8_t *out, uint16_t session_id,
                                           uint32_t total_len, uint16_t chunk_size,
                                           uint32_t checksum,
                                           uint16_t schema_id, uint8_t schema_version,
                                           uint8_t flags) {
    dcf_pipe_pack_open(out, session_id, total_len, chunk_size, checksum);
    out[14] = (uint8_t)(schema_id >> 8);
    out[15] = (uint8_t)(schema_id & 0xFF);
    out[16] = (uint8_t)(((schema_version & 0x0F) << 4) | (flags & 0x0F));
    return DCF_HP_OPENPIPE_LEN;
}

static inline bool dcf_hp_unpack_openpipe(const uint8_t *buf, size_t len,
                                           uint16_t *session_id, uint32_t *total_len,
                                           uint16_t *chunk_size, uint32_t *checksum,
                                           uint16_t *schema_id, uint8_t *schema_version,
                                           uint8_t *flags) {
    if (len < DCF_HP_OPENPIPE_LEN) return false;
    if (dcf_pipe_unpack_open(buf, 14, session_id, total_len, chunk_size, checksum) != 0)
        return false;
    *schema_id = (uint16_t)(((uint16_t)buf[14] << 8) | buf[15]);
    uint8_t vf = buf[16];
    *schema_version = (vf >> 4) & 0x0F;
    *flags = vf & 0x0F;
    return true;
}

/* ── Plane selection ──────────────────────────────────────────────────────── */
static inline const char *dcf_hp_plane_select(const dcf_hp_schema_t *s,
                                               size_t threshold) {
    return dcf_hp_packed_size(s) <= threshold ? "quantum" : "pipe";
}

#endif /* DCF_DEMOD_HYDRAPACK_H */