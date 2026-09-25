/* SPDX-License-Identifier: LGPL-3.0-only
 *
 * demod_superpack.h — DCF SuperPack: a 32-byte container that losslessly carries
 * TWO 17-byte DeModFrame quanta under a single joint CRC-16.
 *
 * Two raw frames cost 2*17 = 34 bytes. When frames are emitted in pairs, the
 * second header is largely recoverable from context (both inner sync bytes are
 * the constant 0xD3; each inner CRC is a pure function of its own 15 leading
 * bytes). SuperPack drops those 6 redundant bytes and spends 4 back on one outer
 * sync, a type/version tag, and ONE joint CRC over the whole container — net
 * 34 -> 32 bytes plus a strictly stronger integrity check.
 *
 * Why it is the lower-latency option: a SuperPack puts a frame pair on the wire
 * as a SINGLE datagram instead of two — one packet, one IP/UDP header, one
 * syscall — so paired traffic crosses the network with strictly lower per-pair
 * overhead and latency than emitting the two frames separately.
 *
 * Unpack reconstructs each inner frame bit-exact, so the outputs are ordinary
 * valid DeModFrames and the 246-vector wire certificate is untouched. SuperPack
 * is a container adapter, never a change to the quantum.
 *
 * Layout (big-endian), 32 bytes:
 *   [0]      sync   = 0xD3
 *   [1]      sflags = version[7:4]=1 | type[3:0]=SUPER (0x5)  => 0x15
 *   [2..15]  frame A core = A's bytes [1..14]
 *   [16..29] frame B core = B's bytes [1..14]
 *   [30..31] CRC-16/CCITT-FALSE over bytes [0..29]
 */
#ifndef DCF_DEMOD_SUPERPACK_H
#define DCF_DEMOD_SUPERPACK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "demod_frame.h"

#define DCF_SUPER_TYPE      0x05u
#define DCF_SUPER_LEN       32u
#define DCF_SUPER_CORE_LEN  14u   /* a frame's bytes [1..14]: all but sync + crc */
#define DCF_SUPER_SFLAGS    (uint8_t)(((uint8_t)1u << DCF_VERSION_SHIFT) | DCF_SUPER_TYPE)

/* The 14-byte reconstructable core of a 17-byte frame, validating sync, version
 * nibble, and the inner CRC so a corrupt frame is never packed. Returns false on
 * any failure; on success writes DCF_SUPER_CORE_LEN bytes into core[]. */
static inline bool dcf_superpack_core(const uint8_t *frame, uint8_t *core) {
    if (frame[0] != DCF_SYNC_BYTE) return false;
    if ((uint8_t)((frame[1] & DCF_VERSION_MASK) >> DCF_VERSION_SHIFT) != 1u) return false;
    uint16_t stored = (uint16_t)((frame[15] << 8) | frame[16]);
    if (dcf_crc16(frame, DCF_FRAME_CRC_COVER) != stored) return false;
    memcpy(core, frame + 1, DCF_SUPER_CORE_LEN);
    return true;
}

/* Rebuild a full 17-byte DeModFrame from its 14-byte core (sync + recomputed crc). */
static inline void dcf_superpack_rebuild(const uint8_t *core, uint8_t *frame) {
    frame[0] = DCF_SYNC_BYTE;
    memcpy(frame + 1, core, DCF_SUPER_CORE_LEN);
    uint16_t crc = dcf_crc16(frame, DCF_FRAME_CRC_COVER);
    frame[15] = (uint8_t)(crc >> 8);
    frame[16] = (uint8_t)crc;
}

/* Combine two valid 17-byte frames into one 32-byte SuperPack (out[32]).
 * Returns false if either input is not a valid frame. */
static inline bool dcf_superpack_pack(const uint8_t *a, const uint8_t *b, uint8_t *out) {
    uint8_t core_a[DCF_SUPER_CORE_LEN], core_b[DCF_SUPER_CORE_LEN];
    if (!dcf_superpack_core(a, core_a)) return false;
    if (!dcf_superpack_core(b, core_b)) return false;
    out[0] = DCF_SYNC_BYTE;
    out[1] = DCF_SUPER_SFLAGS;
    memcpy(out + 2, core_a, DCF_SUPER_CORE_LEN);
    memcpy(out + 2 + DCF_SUPER_CORE_LEN, core_b, DCF_SUPER_CORE_LEN);
    uint16_t crc = dcf_crc16(out, 30);
    out[30] = (uint8_t)(crc >> 8);
    out[31] = (uint8_t)crc;
    return true;
}

/* True iff buf looks like a SuperPack (length + sync + version/type tag). */
static inline bool dcf_superpack_is(const uint8_t *buf, size_t len) {
    return len == DCF_SUPER_LEN && buf[0] == DCF_SYNC_BYTE && buf[1] == DCF_SUPER_SFLAGS;
}

/* Split a 32-byte SuperPack into two bit-exact 17-byte frames (a[17], b[17]), whatever
 * their type nibble (0..15 — the type is not part of the frame gate). Returns false on
 * any integrity failure: exactly the checks of python/MCP/superpack.py:unpack. */
static inline bool dcf_superpack_unpack(const uint8_t *in, uint8_t *a, uint8_t *b) {
    if (in[0] != DCF_SYNC_BYTE) return false;
    if ((uint8_t)((in[1] & DCF_VERSION_MASK) >> DCF_VERSION_SHIFT) != 1u) return false;
    if ((uint8_t)(in[1] & DCF_TYPE_MASK) != DCF_SUPER_TYPE) return false;
    uint16_t stored = (uint16_t)((in[30] << 8) | in[31]);
    if (dcf_crc16(in, 30) != stored) return false;
    dcf_superpack_rebuild(in + 2, a);
    dcf_superpack_rebuild(in + 2 + DCF_SUPER_CORE_LEN, b);
    /* Belt and braces: the rebuilt frames must themselves pass the frame gate — sync +
     * version nibble 1 + CRC (dcf_frame_valid alone skips the version nibble, so an
     * inner core with version != 1 would slip through where the references reject it). */
    uint8_t core[DCF_SUPER_CORE_LEN];
    return dcf_superpack_core(a, core) && dcf_superpack_core(b, core);
}

#endif /* DCF_DEMOD_SUPERPACK_H */
