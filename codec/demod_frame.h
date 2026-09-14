// SPDX-License-Identifier: LGPL-3.0-only
/*
 * transport/dcf_frame.h — DeMoD 17-byte transport frame codec
 * DeMoD LLC | GPL-3.0
 *
 * Single-header, zero-dependency C99 implementation.
 * Drop-in for any C/C++ target including embedded (RP2040, STM32, bare-metal).
 *
 * Wire layout (17 bytes = 136 bits, all multi-byte fields big-endian):
 *
 *   Byte  Field        Width  Notes
 *   ────  ───────────  ─────  ─────────────────────────────────────────────────
 *    0    sync         1 B    Fixed 0xD3 — first validity gate
 *    1    flags        1 B    [7:4] version (0–15) | [3:0] frame type (0–15)
 *    2-3  seq          2 B    Rolling sequence counter, big-endian uint16
 *    4-5  src_id       2 B    Source node ID, big-endian uint16
 *    6-7  dst_id       2 B    Destination node ID (0xFFFF = broadcast)
 *    8-11 payload      4 B    Application data
 *   12-14 timestamp    3 B    24-bit µs offset, big-endian, wraps ~16.7 s
 *   15-16 crc16        2 B    CRC-CCITT(poly=0x1021, init=0xFFFF) over [0..14]
 *
 * Frame types:
 *   0 = DATA    — application payload
 *   1 = ACK     — acknowledgement
 *   2 = BEACON  — clock sync / broadcast beacon
 *   3 = CTRL    — control / fragmented audio
 *
 * CRC covers bytes [0..14] (everything except the CRC field itself).
 * A frame is valid iff sync == 0xD3 AND crc16([0..14]) == frame[15..16].
 *
 * Cross-language parity:
 *   Haskell  DCF.Transport.Frame    encodeFrame / decodeFrame
 *   C        transport/dcf_frame.h  dcf_frame_encode / dcf_frame_decode
 *   Rust     transport/dcf_frame.rs Frame::encode / Frame::decode
 *   Lisp     punctim.lisp         encode-dcf-frame / decode-dcf-frame
 */

#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* ── Constants ────────────────────────────────────────────────────────────── */

#define DCF_FRAME_SIZE       17u   /* total wire size in bytes                */
#define DCF_FRAME_CRC_COVER  15u   /* bytes covered by CRC: [0..14]           */
#define DCF_SYNC_BYTE        0xD3u /* fixed preamble / first validity gate    */
#define DCF_BROADCAST        0xFFFFu

/* Bit-field positions inside flags byte */
#define DCF_VERSION_SHIFT    4u
#define DCF_VERSION_MASK     0xF0u
#define DCF_TYPE_MASK        0x0Fu

/* ── Frame type enum ─────────────────────────────────────────────────────── */

typedef enum {
    DCF_TYPE_DATA   = 0,
    DCF_TYPE_ACK    = 1,
    DCF_TYPE_BEACON = 2,
    DCF_TYPE_CTRL   = 3,
} dcf_frame_type_t;

/* ── Decoded frame (host-order, field-by-field) ──────────────────────────── */

typedef struct {
    uint8_t          version;       /* 4-bit, 0–15                            */
    dcf_frame_type_t type;          /* 4-bit nibble                           */
    uint16_t         seq;           /* rolling counter                        */
    uint16_t         src_id;        /* source node                            */
    uint16_t         dst_id;        /* dest node (DCF_BROADCAST = all)        */
    uint8_t          payload[4];    /* application bytes                      */
    uint32_t         timestamp_us;  /* 24-bit µs, stored as uint32 (top byte 0) */
} dcf_frame_t;

/* ── CRC-CCITT ───────────────────────────────────────────────────────────── */
/*
 * Poly 0x1021, init 0xFFFF.
 * Identical algorithm used in Haskell (crc16ccitt), Rust (crc16_ccitt),
 * and Lisp (crc16-ccitt).  All implementations must produce the same output
 * for the same input bytes — this is the cross-language compatibility gate.
 */
static inline uint16_t dcf_crc16(const uint8_t *data, size_t len) {
    uint16_t crc = 0xFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x8000u)
                ? (uint16_t)(((uint16_t)(crc << 1)) ^ 0x1021u)
                : (uint16_t)((uint16_t)(crc << 1));
    }
    return crc;
}

/* ── Encode ──────────────────────────────────────────────────────────────── */
/*
 * Serialise a dcf_frame_t into exactly DCF_FRAME_SIZE bytes.
 * buf must point to at least 17 bytes of writable memory.
 * Returns buf for convenience.
 *
 * All multi-byte fields are written big-endian regardless of host byte order.
 * The CRC is computed over buf[0..14] and written to buf[15..16].
 */
static inline uint8_t *dcf_frame_encode(const dcf_frame_t *f, uint8_t *buf) {
    /* [0] sync */
    buf[0] = DCF_SYNC_BYTE;

    /* [1] flags: version[7:4] | type[3:0] */
    buf[1] = (uint8_t)(((f->version & 0x0Fu) << DCF_VERSION_SHIFT)
                      | (f->type    & 0x0Fu));

    /* [2-3] seq, big-endian */
    buf[2] = (uint8_t)((f->seq >> 8) & 0xFFu);
    buf[3] = (uint8_t)( f->seq       & 0xFFu);

    /* [4-5] src_id, big-endian */
    buf[4] = (uint8_t)((f->src_id >> 8) & 0xFFu);
    buf[5] = (uint8_t)( f->src_id       & 0xFFu);

    /* [6-7] dst_id, big-endian */
    buf[6] = (uint8_t)((f->dst_id >> 8) & 0xFFu);
    buf[7] = (uint8_t)( f->dst_id       & 0xFFu);

    /* [8-11] payload, verbatim */
    memcpy(buf + 8, f->payload, 4);

    /* [12-14] timestamp, 24-bit big-endian */
    buf[12] = (uint8_t)((f->timestamp_us >> 16) & 0xFFu);
    buf[13] = (uint8_t)((f->timestamp_us >>  8) & 0xFFu);
    buf[14] = (uint8_t)( f->timestamp_us         & 0xFFu);

    /* [15-16] CRC-CCITT over [0..14], big-endian */
    uint16_t crc = dcf_crc16(buf, DCF_FRAME_CRC_COVER);
    buf[15] = (uint8_t)((crc >> 8) & 0xFFu);
    buf[16] = (uint8_t)( crc       & 0xFFu);

    return buf;
}

/* ── Decode ──────────────────────────────────────────────────────────────── */
/*
 * Parse DCF_FRAME_SIZE bytes into a dcf_frame_t.
 * Returns true on success, false if sync byte or CRC is wrong.
 * *out is written only on success; it is not touched on failure.
 */
static inline bool dcf_frame_decode(const uint8_t *buf, dcf_frame_t *out) {
    /* First gate: sync byte */
    if (buf[0] != DCF_SYNC_BYTE) return false;

    /* Second gate: CRC over [0..14] must match [15..16] */
    uint16_t crc_calc   = dcf_crc16(buf, DCF_FRAME_CRC_COVER);
    uint16_t crc_stored = (uint16_t)(((uint16_t)buf[15] << 8) | buf[16]);
    if (crc_calc != crc_stored) return false;

    out->version      = (buf[1] & DCF_VERSION_MASK) >> DCF_VERSION_SHIFT;
    out->type         = (dcf_frame_type_t)(buf[1] & DCF_TYPE_MASK);
    out->seq          = (uint16_t)(((uint16_t)buf[2] << 8) | buf[3]);
    out->src_id       = (uint16_t)(((uint16_t)buf[4] << 8) | buf[5]);
    out->dst_id       = (uint16_t)(((uint16_t)buf[6] << 8) | buf[7]);
    memcpy(out->payload, buf + 8, 4);
    out->timestamp_us = ((uint32_t)buf[12] << 16)
                      | ((uint32_t)buf[13] <<  8)
                      |  (uint32_t)buf[14];
    return true;
}

/* ── Validate (non-destructive, no decode) ───────────────────────────────── */
/*
 * Returns true iff buf[0..16] is a well-formed frame.
 * Safe to call on any 17-byte buffer — does not access out-of-bounds memory.
 */
static inline bool dcf_frame_valid(const uint8_t *buf) {
    if (buf[0] != DCF_SYNC_BYTE) return false;
    uint16_t crc_calc   = dcf_crc16(buf, DCF_FRAME_CRC_COVER);
    uint16_t crc_stored = (uint16_t)(((uint16_t)buf[15] << 8) | buf[16]);
    return crc_calc == crc_stored;
}

/* ── Convenience initialiser ─────────────────────────────────────────────── */
/*
 * Zero-initialise a dcf_frame_t and set the structural fields.
 * Caller must set payload[] and timestamp_us, then call dcf_frame_encode().
 */
static inline void dcf_frame_init(dcf_frame_t  *f,
                                   uint8_t       version,
                                   dcf_frame_type_t type,
                                   uint16_t      seq,
                                   uint16_t      src_id,
                                   uint16_t      dst_id) {
    memset(f, 0, sizeof(*f));
    f->version = version;
    f->type    = type;
    f->seq     = seq;
    f->src_id  = src_id;
    f->dst_id  = dst_id;
}

/* ── Reference test vectors ─────────────────────────────────────────────── */
/*
 * These byte sequences are used as cross-language test fixtures.
 * The same vectors appear in Haskell FrameSpec.hs (exampleFrame),
 * the Rust #[cfg(test)] block, and the Lisp fiveam dcf-frame-roundtrip-test.
 *
 * Vector A — DATA frame, seq=0x0001, src=0x0001, dst=0xFFFF,
 *             payload=0xDEADBEEF, ts=0x000000, version=1
 *
 *   Byte:  D3 10 00 01 00 01 FF FF  DE AD BE EF 00 00 00  XX XX
 *          ──  ──  ─────  ─────  ─────  ──────────────  ──────  ─────
 *          sync flg  seq   src   dst    payload         ts      crc
 *
 *   Expected CRC (computed offline): verify with dcf_crc16(buf, 15).
 *
 * To compute the CRC for documentation purposes:
 *   uint8_t body[15] = {
 *       0xD3, 0x10, 0x00, 0x01, 0x00, 0x01, 0xFF, 0xFF,
 *       0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x00, 0x00
 *   };
 *   uint16_t crc = dcf_crc16(body, 15);  // → deterministic value
 */
