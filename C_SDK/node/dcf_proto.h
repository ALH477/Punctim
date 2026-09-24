/* SPDX-License-Identifier: LGPL-3.0-only
 *
 * dcf_proto.h — the binary ProtoMessage UDP transport envelope, byte-identical
 * to the Go (go/node/proto.go) and Rust (rust/src/lib.rs) reference SDKs, so the
 * C node meshes with them over IP. Header-only (mirrors the codec/demod_*.h style).
 *
 * Wire layout (big-endian):
 *   [0]      msg_type     u8
 *   [1:5]    sequence     u32
 *   [5:13]   timestamp    u64  (micros since the Unix epoch)
 *   [13:17]  payload_len  u32
 *   [17:...] payload
 *
 * Golden vector (see test_proto_certify.c):
 *   {type=1, seq=42, ts=0x0102030405060708, payload=01 02 03}
 *   -> 010000002a010203040506070800000003010203
 */
#ifndef DCF_PROTO_H
#define DCF_PROTO_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* Message-type registry — verbatim from rust `mod msg_type` / go proto.go. */
#define DCF_MSG_POSITION   1u
#define DCF_MSG_AUDIO      2u
#define DCF_MSG_GAME_EVENT 3u
#define DCF_MSG_STATE_SYNC 4u
#define DCF_MSG_RELIABLE   5u
#define DCF_MSG_ACK        6u
#define DCF_MSG_PING       7u
#define DCF_MSG_PONG       8u
#define DCF_MSG_GAME_DCF   9u
#define DCF_MSG_TEXT_DCF   10u  /* Go extension; carries one DCF-Text DeModFrame */
#define DCF_MSG_MESH       11u  /* DCF-Mesh control (REPORT/ROLE); see demod_mesh.h */
#define DCF_MSG_FRAME      12u  /* DCF-Medium udp:dialect=proto — one raw 17-byte DeModFrame
                                   (payload_len == 17); see codec/demod_medium.h */

#define DCF_PROTO_HEADER_LEN 17u

/* Serialise into out[out_cap]; returns total bytes, or 0 if out_cap is too small. */
static inline size_t dcf_proto_serialize(uint8_t msg_type, uint32_t seq, uint64_t ts,
                                         const uint8_t *payload, uint32_t payload_len,
                                         uint8_t *out, size_t out_cap) {
    if (out_cap < (size_t)DCF_PROTO_HEADER_LEN + payload_len) return 0;
    out[0] = msg_type;
    out[1] = (uint8_t)(seq >> 24); out[2] = (uint8_t)(seq >> 16);
    out[3] = (uint8_t)(seq >> 8);  out[4] = (uint8_t)seq;
    for (int i = 0; i < 8; i++) out[5 + i] = (uint8_t)(ts >> (56 - 8 * i));
    out[13] = (uint8_t)(payload_len >> 24); out[14] = (uint8_t)(payload_len >> 16);
    out[15] = (uint8_t)(payload_len >> 8);  out[16] = (uint8_t)payload_len;
    if (payload_len && payload) memcpy(out + DCF_PROTO_HEADER_LEN, payload, payload_len);
    return (size_t)DCF_PROTO_HEADER_LEN + payload_len;
}

/* Parse a ProtoMessage; on success sets out_* and points *payload into data. */
static inline bool dcf_proto_deserialize(const uint8_t *data, size_t len,
                                         uint8_t *msg_type, uint32_t *seq, uint64_t *ts,
                                         const uint8_t **payload, uint32_t *payload_len) {
    if (len < DCF_PROTO_HEADER_LEN) return false;
    uint32_t pl = ((uint32_t)data[13] << 24) | ((uint32_t)data[14] << 16) |
                  ((uint32_t)data[15] << 8) | (uint32_t)data[16];
    if (len < (size_t)DCF_PROTO_HEADER_LEN + pl) return false;
    *msg_type = data[0];
    *seq = ((uint32_t)data[1] << 24) | ((uint32_t)data[2] << 16) |
           ((uint32_t)data[3] << 8) | (uint32_t)data[4];
    uint64_t t = 0;
    for (int i = 0; i < 8; i++) t = (t << 8) | data[5 + i];
    *ts = t;
    *payload_len = pl;
    *payload = data + DCF_PROTO_HEADER_LEN;
    return true;
}

#endif /* DCF_PROTO_H */
