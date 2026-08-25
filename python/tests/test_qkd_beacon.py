# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF-QKD key-ID beacon L2 laws, over the canonical python/MCP/qkdlab_core.py.

The byte-level contract is Documentation/qkd_vectors.json (certified against C and
Rust); these are the behavioural laws around it.  See Documentation/DCF_QKD_SPEC.md.
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "MCP"))

import qkdlab_core as qc  # noqa: E402
from wirelab_core import decode  # noqa: E402

KID_A = "574bace1-4c27-49a1-babd-663fdb624d00"
KID_B = "d54b214e-60eb-4f1c-afb0-62a7b23fc20e"


class TestFraming(unittest.TestCase):
    """The arithmetic that makes this adapter descriptor-free."""

    def test_a_key_id_is_always_exactly_four_frames(self):
        self.assertEqual(qc.KEY_ID_BYTES, qc.FRAGS * 4)
        for kid in (KID_A, KID_B, "00000000-0000-0000-0000-000000000000"):
            frames = qc.packetize(kid, 0, 0, 1, 2)
            self.assertEqual(len(frames), qc.FRAGS)
            self.assertTrue(all(len(f) == 17 for f in frames))

    def test_frames_are_valid_ctrl_demodframes(self):
        for i, f in enumerate(qc.packetize(KID_A, 5, 0x010203, 0x00A1, 0x0002)):
            d = decode(f)                       # raises on sync/version/CRC failure
            self.assertEqual(f[0], 0xD3)
            self.assertEqual(d["frame_type"], qc.FCTRL)
            self.assertEqual(d["seq"] >> qc.FRAG_BITS, 5)
            self.assertEqual(d["seq"] & qc.FRAG_MASK, i)
            self.assertEqual(d["ts_us"], 0x010203)

    def test_payloads_concatenate_to_the_uuid_with_no_padding(self):
        frames = qc.packetize(KID_A, 0, 0, 1, 2)
        joined = b"".join(bytes.fromhex(decode(f)["payload"]) for f in frames)
        self.assertEqual(joined, uuid.UUID(KID_A).bytes)

    def test_accepts_raw_16_bytes_as_well_as_a_uuid_string(self):
        raw = uuid.UUID(KID_A).bytes
        self.assertEqual(qc.packetize(raw, 0, 0, 1, 2), qc.packetize(KID_A, 0, 0, 1, 2))

    def test_epoch_rail_is_enforced(self):
        qc.packetize(KID_A, qc.MAX_EPOCH, 0, 1, 2)     # the rail itself is legal
        for bad in (-1, qc.MAX_EPOCH + 1):
            with self.assertRaises(ValueError):
                qc.packetize(KID_A, bad, 0, 1, 2)

    def test_malformed_key_ids_are_rejected(self):
        for bad in ("not-a-uuid", b"\x00" * 15, b"\x00" * 17, None):
            with self.assertRaises(ValueError):
                qc.packetize(bad, 0, 0, 1, 2)


class TestReassembly(unittest.TestCase):
    def _drain(self, r, frames):
        out = []
        for f in frames:
            out += r.push(f)
        return out

    def test_order_independent(self):
        frames = qc.packetize(KID_A, 5, 7, 1, 2)
        for order in ([0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]):
            r = qc.KeyIdReassembler()
            out = self._drain(r, [frames[i] for i in order])
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0], ("key_id", 5, 7, 1, 2, KID_A))
            self.assertEqual(r.pending(), 0)

    def test_duplicates_do_not_double_emit(self):
        frames = qc.packetize(KID_A, 5, 7, 1, 2)
        r = qc.KeyIdReassembler()
        out = self._drain(r, frames + frames[:1])
        self.assertEqual(len(out), 1)

    def test_dropped_fragment_is_reported_lost(self):
        frames = qc.packetize(KID_A, 11, 1, 1, 2)
        r = qc.KeyIdReassembler()
        self.assertEqual(self._drain(r, frames[:3]), [])
        self.assertEqual(r.finalize(), [("lost", 11, 1, 2)])

    def test_interleaved_epochs_stay_separate(self):
        a = qc.packetize(KID_A, 1, 5, 1, 2)
        b = qc.packetize(KID_B, 2, 5, 1, 2)
        r = qc.KeyIdReassembler()
        out = self._drain(r, [f for pair in zip(a, b) for f in pair])
        self.assertEqual(sorted(e[5] for e in out), sorted([KID_A, KID_B]))

    def test_same_epoch_from_two_srcs_stays_separate(self):
        """Keying on (src, dst, epoch) — two SAEs may beacon the same epoch at once."""
        a = qc.packetize(KID_A, 7, 5, 1, 2)
        b = qc.packetize(KID_B, 7, 5, 9, 2)
        r = qc.KeyIdReassembler()
        out = self._drain(r, [f for pair in zip(a, b) for f in pair])
        self.assertEqual(sorted(e[5] for e in out), sorted([KID_A, KID_B]))

    def test_corrupt_frame_is_dropped_not_fatal(self):
        f = bytearray(qc.packetize(KID_A, 0, 0, 1, 2)[0])
        f[9] ^= 0x01                                   # flip one payload bit
        r = qc.KeyIdReassembler()
        self.assertEqual(r.push(bytes(f)), [])         # CRC rejects it, no exception
        self.assertEqual(r.pending(), 0)

    def test_foreign_dst_ignored_broadcast_accepted(self):
        r = qc.KeyIdReassembler(accept_dst=0x9999)
        self.assertEqual(self._drain(r, qc.packetize(KID_A, 1, 0, 1, 0x1234)), [])
        self.assertEqual(r.pending(), 0)
        out = self._drain(r, qc.packetize(KID_A, 2, 0, 1, qc.BROADCAST))
        self.assertEqual(len(out), 1)

    def test_non_ctrl_frames_are_ignored(self):
        """Text/game/sstv ride DATA(0); a beacon reassembler must never eat them."""
        from wirelab_core import encode
        r = qc.KeyIdReassembler()
        self.assertEqual(r.push(encode(0, 0, 1, 2, b"\x00\x01\x02\x03", 0)), [])
        self.assertEqual(r.pending(), 0)

    def test_in_flight_set_is_bounded(self):
        """A lossy link must not leak memory: oldest incomplete beacon is evicted."""
        r = qc.KeyIdReassembler(max_pending=2)
        for ep in (0, 1):
            self.assertEqual(r.push(qc.packetize(KID_A, ep, 0, 1, 2)[0]), [])
        self.assertEqual(r.push(qc.packetize(KID_A, 2, 0, 1, 2)[0]),
                         [("lost", 0, 1, 2)])
        self.assertEqual(r.pending(), 2)


class TestChannel(unittest.TestCase):
    def test_rendezvous_anchor(self):
        self.assertEqual(qc.channel_id("123456789"), 0x29B1)   # repo-wide CRC anchor
        self.assertEqual(qc.channel_id(None), qc.BROADCAST)
        self.assertEqual(qc.channel_id(""), qc.BROADCAST)


class TestVectorsCommitted(unittest.TestCase):
    def test_documentation_and_mcp_copies_are_identical(self):
        """CI byte-diffs these; catch drift locally too."""
        import json
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "MCP", "qkd_vectors.json"), "rb") as fh:
            a = fh.read()
        with open(os.path.join(here, "..", "..", "Documentation", "qkd_vectors.json"), "rb") as fh:
            b = fh.read()
        self.assertEqual(a, b, "python/MCP and Documentation vector copies have drifted")
        v = json.loads(a)
        self.assertEqual(v["constants"]["frags"], qc.FRAGS)
        self.assertEqual(v["constants"]["max_epoch"], qc.MAX_EPOCH)
        self.assertEqual(v["constants"]["frame_type_ctrl"], qc.FCTRL)


if __name__ == "__main__":
    unittest.main()
