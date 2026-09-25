# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""Shared on-air framing for the DCF acoustic modem — the single source of truth that
makes the **numpy** modem (`acoustic.py`) and the real-time **Faust** modem (`main.py`)
interoperable. Both produce/consume the *same* bit stream and the *same* tones/baud, so a
frame sent by either is decodable by the other.

On-air bit layout (MSB-first, identical to the deployed Faust modem):

    [ preamble: `preamble_bits` alternating 0,1,0,1… ]
    [ sync: 0x7E = 01111110 ]
    [ payload ]
    [ postamble: 16 alternating bits ]

Two payload modes (both modems must agree per link):
  * `fec=False` (default, interop baseline): `frame(17B) + crc8`  — exactly what the Faust
    modem has always sent (CRC-8 poly 0x31, non-reflected, check value 0xA2 — not the
    reflected CRC-8/MAXIM, whose check value is 0xA1).
  * `fec=True`  (robust): `rs_encode(frame)` — the certified Reed-Solomon codeword, so the
    receiver corrects (not just detects) the byte-errors a handheld radio injects.

Tones/baud live in `PROFILES`; the bit layer here is tone-agnostic.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP"))
import feclab_core as fec

SYNC_WORD = 0x7E
POSTAMBLE_BITS = 16
DEFAULT_NPARITY = fec.RS_DEFAULT_NPARITY    # 16
MSGLEN = 17                                 # a DeModFrame is 17 bytes

# Per-medium tone plan (Hz) + symbol rate. baud=300 matches the deployed Faust modem;
# "handheld" pulls both tones mid-band (off the 300/3000 Hz rolloffs) and uses a long
# keyup preamble so the receiver AGC settles + squelch opens before sync.
PROFILES = {
    "standard": {"mark": 1200.0, "space": 2200.0, "baud": 300, "preamble_bits": 80},
    "handheld": {"mark": 1200.0, "space": 1800.0, "baud": 300, "preamble_bits": 240},
    "aux-cable": {"mark": 1000.0, "space": 1500.0, "baud": 1200, "preamble_bits": 16},
}


def crc8(data):
    """Poly-0x31 CRC-8 (MSB-first, init 0x00, non-reflected) — byte-identical to the
    deployed Faust modem's frame check (which labels it "MAXIM"; it is not reflected)."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def to_bits(data):
    """bytes -> list of bits, MSB-first."""
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


def from_bits(bits):
    """list of bits -> bytes, MSB-first (drops a trailing partial byte)."""
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | (bits[i + j] & 1)
        out.append(byte)
    return bytes(out)


def preamble(n):
    """`n` alternating bits starting with 0 (0,1,0,1…) — the Faust modem's convention."""
    return [i % 2 for i in range(n)]


def encode_bits(frame_bytes, fec_mode=False, nparity=DEFAULT_NPARITY, preamble_bits=80):
    """A 17-byte DeModFrame -> the full on-air bit list (preamble + sync + payload +
    postamble). `fec_mode=False` reproduces the deployed Faust modem byte-for-byte."""
    frame_bytes = bytes(frame_bytes)
    if fec_mode:
        payload = fec.rs_encode(frame_bytes, nparity)
    else:
        payload = frame_bytes + bytes([crc8(frame_bytes)])
    bits = preamble(preamble_bits)
    bits += to_bits(bytes([SYNC_WORD]))
    bits += to_bits(payload)
    bits += preamble(POSTAMBLE_BITS)
    return bits


def find_sync(bits):
    """Index of the first bit *after* the 0x7E sync word, or -1. Bit-level search, so the
    preamble length need not be a whole number of bytes."""
    pat = to_bits(bytes([SYNC_WORD]))
    for i in range(len(bits) - len(pat) + 1):
        if bits[i:i + len(pat)] == pat:
            return i + len(pat)
    return -1


def decode_bits(bits, fec_mode=False, nparity=DEFAULT_NPARITY, msglen=MSGLEN):
    """On-air bits -> (frame_bytes, n_corrected) or None. With `fec_mode` the payload is an
    RS codeword (errors corrected); otherwise it is frame+crc8 (errors only detected)."""
    pos = find_sync(bits)
    if pos < 0:
        return None
    data = from_bits(bits[pos:])
    if fec_mode:
        code = data[:msglen + nparity]
        if len(code) < msglen + nparity:
            return None
        try:
            msg, n = fec.rs_decode(code, nparity, msglen)
            return bytes(msg), n
        except fec.FecError:
            return None
    if len(data) < msglen + 1:
        return None
    frame, rx_crc = data[:msglen], data[msglen]
    if crc8(frame) != rx_crc:
        return None
    return bytes(frame), 0
