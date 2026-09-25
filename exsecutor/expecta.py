#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
#
# RELICENSED SOURCE — an exception to LICENSE.EXCEPTION.
#
# This file originates in the Exsecutor repository (github.com/ALH477/exsecutor,
# GPL-3.0-or-later plus the GPLv3 §7 additional permissions in its
# LICENSE.EXCEPTION) as tests/conformance/entry23/expecta.py. DeMoD LLC is the
# sole copyright holder of both Exsecutor and Punctim and, on that basis,
# additionally licenses THIS COPY under LGPL-3.0-only so that it can live inside
# Punctim's linkable tree. A sole copyright holder may license their own work
# under more than one licence; this is that, not a conversion of the original.
#
# Why a new grant was needed. LICENSE.EXCEPTION carries two §7 permissions:
# Exception A covers compiler OUTPUT, Exception B covers designated runtime files
# linked into a program. Neither reaches compiler-INPUT source such as this. So
# this is a third, narrower carve-out — an exception to the exception — scoped
# exactly to the files in this directory.
#
# It changes nothing upstream: the Exsecutor original remains GPL-3.0-or-later,
# and no other Exsecutor source is relicensed by implication. See LICENSING.md
# and Documentation/DCF_EXSECUTOR.md.
#
# CHANGED FROM UPSTREAM: VECTORS points at this repo's own
# Documentation/golden_vectors.json (overridable with $PUNCTIM_GOLDEN_VECTORS)
# rather than Exsecutor's vendored copy, so the in-tree codec is certified
# against the live certificate and cannot drift from it.
"""entry23/expecta.py -- the expected certificate stream for spec §14 entry 23.

Verification only: never on the build path, never shipped, and it imports
nothing from HydraMesh or from prototypes/ (prototypes/README.md: a prototype
is never a test dependency). Its one input is the vendored certificate,
vendor/hydramesh-wire/golden_vectors.json, read as data.

It builds the 2,502 bytes entry23/probatio.exsc must write, laid out as
docs/design/wire-codec.md section 6 says:

  §  bytes          from
  1  109 x 17       encode_basis[k].frame, verbatim
  2  137 x 2        syndrome_basis[k].syndrome, big-endian, verbatim
  3  2 + 2 + 17     anchors.crc_123456789, anchors.crc_zero15 (big-endian),
                    anchors.exampleFrame_full
  4  109 + 109      the reference decode's verdict on each section-1 frame;
                    then 1 where that decode returns the fields the frame's
                    input index says were encoded, else 0
  5  136            the reference decode's verdict on the example frame with
                    wire bit i flipped, i = 0..135

Sections 1-3 are the JSON's own bytes. Sections 4 and 5 are DERIVED here with
a decode written from the reference implementation's order (HydraMesh
python/MCP/wirelab_core.py `decode`: sync, then version nibble, then CRC) --
not from the vendored spec, whose validity rule is an unordered "iff" and so
certifies the verdict but not which condition a bad frame fails.

Usage:
  expecta.py                write the expected stream to standard output
  expecta.py --compara OUT  compare OUT, section by section, and report;
                            exit 0 iff every section matches
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# In Punctim the certificate is not vendored — it is the live one this repo
# ships and regenerates, so the codec is certified against the same file every
# other binding is, and cannot silently drift from it.
VECTORS = os.environ.get(
    "PUNCTIM_GOLDEN_VECTORS",
    os.path.join(HERE, "..", "Documentation", "golden_vectors.json"))

SYNC, VERSION, FRAME_LEN = 0xD3, 1, 17
# The 108 free input bits, LSB-first within each field, fields in wire order
# (verify_laws.py's FIELD_BITS). `payload` is the u32 whose big-endian bytes
# are frame bytes 8..11.
FIELD_BITS = [("frame_type", 4), ("seq", 16), ("src", 16), ("dst", 16),
              ("payload", 32), ("ts_us", 24)]


def crc16_ccitt_false(data):
    """CRC-16/CCITT-FALSE: init 0xFFFF, poly 0x1021, no reflection, no xorout."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def verdict(word):
    """0 valid, 1 bad sync, 2 bad version, 3 bad CRC -- wirelab_core's order."""
    assert len(word) == FRAME_LEN
    if word[0] != SYNC:
        return 1
    if word[1] >> 4 != VERSION:
        return 2
    if crc16_ccitt_false(word[:15]) != int.from_bytes(word[15:17], "big"):
        return 3
    return 0


def fields(word):
    """The six free fields of a frame, as integers."""
    return {
        "frame_type": word[1] & 0x0F,
        "seq": int.from_bytes(word[2:4], "big"),
        "src": int.from_bytes(word[4:6], "big"),
        "dst": int.from_bytes(word[6:8], "big"),
        "payload": int.from_bytes(word[8:12], "big"),
        "ts_us": int.from_bytes(word[12:15], "big"),
    }


def fields_from_bits(n):
    """The encode input whose 108 free bits are the integer n."""
    f, shift = {}, 0
    for name, width in FIELD_BITS:
        f[name] = (n >> shift) & ((1 << width) - 1)
        shift += width
    return f


def load():
    with open(VECTORS, "rb") as fh:
        cert = json.load(fh)
    enc, syn, anc = cert["encode_basis"], cert["syndrome_basis"], cert["anchors"]
    # The order the driver writes in is the order the JSON lists; check that
    # it is, rather than assume it.
    assert len(enc) == 109 and len(syn) == 137, (len(enc), len(syn))
    assert "input_bit" not in enc[0]
    assert [v["input_bit"] for v in enc[1:]] == list(range(108))
    assert "bit" not in syn[0]
    assert [v["bit"] for v in syn[1:]] == list(range(136))
    return enc, syn, anc


def sections():
    """[(number, name, [(label, bytes), ...]), ...] -- the expected stream."""
    enc, syn, anc = load()
    frames = [bytes.fromhex(v["frame"]) for v in enc]
    assert all(len(w) == FRAME_LEN for w in frames)
    inputs = [0] + [1 << i for i in range(108)]

    s1 = [("vector %d" % k, w) for k, w in enumerate(frames)]
    s2 = [("vector %d" % k, v["syndrome"].to_bytes(2, "big"))
          for k, v in enumerate(syn)]

    example = bytes.fromhex(anc["exampleFrame_full"])
    s3 = [("CRC(\"123456789\")", int(anc["crc_123456789"], 16).to_bytes(2, "big")),
          ("CRC(0^15)", int(anc["crc_zero15"], 16).to_bytes(2, "big")),
          ("example frame", example)]

    s4 = [("lege(frame %d)" % k, bytes([verdict(w)])) for k, w in enumerate(frames)]
    s4 += [("round trip of frame %d" % k,
            bytes([1 if fields(w) == fields_from_bits(n) else 0]))
           for k, (w, n) in enumerate(zip(frames, inputs))]

    s5 = []
    for i in range(136):
        w = bytearray(example)
        w[i // 8] ^= 0x80 >> (i % 8)
        s5.append(("lege(example, bit %d flipped)" % i, bytes([verdict(bytes(w))])))

    # The reference decode is this file's own code, so hold it to what it
    # must say before anything is judged against it: its CRC gives the three
    # CRC anchors, every basis frame decodes valid and round-trips, and one-bit
    # corruption of the example is caught by sync on bits 0-7, by the version
    # nibble on bits 8-11, and by the CRC everywhere else (wire-codec.md
    # section 6). A wrong reference fails here, not as a codec mismatch.
    assert crc16_ccitt_false(b"123456789") == int(anc["crc_123456789"], 16)
    assert crc16_ccitt_false(bytes(15)) == int(anc["crc_zero15"], 16)
    assert crc16_ccitt_false(example[:15]) == int.from_bytes(example[15:], "big")
    assert b"".join(b for _, b in s4) == bytes(109) + bytes([1]) * 109
    assert b"".join(b for _, b in s5) == bytes([1] * 8 + [2] * 4 + [3] * 124)

    out = [(1, "encode basis", s1), (2, "syndrome basis", s2),
           (3, "anchors", s3), (4, "laws: decode verdicts, round trip", s4),
           (5, "laws: decode order under one-bit corruption", s5)]
    total = sum(len(b) for _, _, recs in out for _, b in recs)
    assert total == 2502, total
    return out


def compara(path):
    with open(path, "rb") as fh:
        got = fh.read()
    secs = sections()
    want_len = sum(len(b) for _, _, recs in secs for _, b in recs)
    ok = True
    first = None
    shown = 0
    counts = {}
    pos = 0
    for num, name, recs in secs:
        good = 0
        for idx, (label, want) in enumerate(recs):
            have = got[pos:pos + len(want)]
            pos += len(want)
            if have == want:
                good += 1
                continue
            ok = False
            if first is None:
                first = (num, idx)
            if shown < 8:  # enough to see a pattern; the counts say the rest
                shown += 1
                d = next(j for j in range(len(want))
                         if j >= len(have) or have[j] != want[j])
                print("MISMATCH section %d (%s), %s: expected %s, got %s; "
                      "first differing byte %d: expected %02x, got %s"
                      % (num, name, label, want.hex(), have.hex() or "(nothing)",
                         d, want[d],
                         "%02x" % have[d] if d < len(have) else "(nothing)"))
        counts[num] = (good, len(recs))
        print("section %d (%s): %d/%d" % (num, name, good, len(recs)))
    if len(got) != want_len:
        print("MISMATCH stream is %d bytes, expected %d" % (len(got), want_len))
        ok = False
    print("certificate: %d/246 vectors (encode basis %d/%d, syndrome basis %d/%d)"
          % ((counts[1][0] + counts[2][0],) + counts[1] + counts[2]))
    print("anchors: %d/%d (section 3)" % counts[3])
    print("laws: %d/%d (section 4), %d/%d (section 5)" % (counts[4] + counts[5]))
    if first is not None:
        # read by tests/run.sh's mutant check: the first record that differs
        print("FIRST-FAIL section=%d index=%d" % first)
    return 0 if ok else 1


def main(argv):
    if len(argv) == 1:
        stream = b"".join(b for _, _, recs in sections() for _, b in recs)
        sys.stdout.buffer.write(stream)
        return 0
    if len(argv) == 3 and argv[1] == "--compara":
        return compara(argv[2])
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
