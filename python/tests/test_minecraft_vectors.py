# SPDX-License-Identifier: LGPL-3.0-only
"""DCF-Minecraft: the reference register/event model (python/MCP/mclab_core.py) against the
committed certificate (Documentation/minecraft_vectors.json == python/MCP copy)."""
import json
import os
import sys
import unittest

PYDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(PYDIR)
sys.path.insert(0, os.path.join(PYDIR, "MCP"))

import mclab_core as M  # noqa: E402
import gamelab_core as G  # noqa: E402
from wirelab_core import decode  # noqa: E402

DOC = os.path.join(ROOT, "Documentation", "minecraft_vectors.json")
PKG = os.path.join(PYDIR, "MCP", "minecraft_vectors.json")


def load():
    with open(DOC) as fh:
        return json.load(fh)


class TestCertificate(unittest.TestCase):
    def setUp(self):
        self.v = load()

    def test_copies_identical(self):
        with open(DOC, "rb") as a, open(PKG, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_anchors(self):
        an = self.v["anchors"]
        g = bytes.fromhex(an["golden_frame"])
        self.assertEqual(g, M.GOLDEN)
        self.assertEqual(M.frame_to_nibbles(g), an["golden_nibbles"])
        self.assertEqual(M.frame_to_words(g), an["golden_words"])
        self.assertEqual(M.frame_to_items(g), an["golden_items"])
        self.assertEqual(an["crc_123456789"], 0x29B1)

    def test_constants_and_channels(self):
        c = self.v["constants"]
        self.assertEqual(c["barrel_capacity"], M.BARREL_CAPACITY)
        self.assertEqual(c["nibbles"], M.NIBBLES)
        self.assertEqual(self.v["channels"], {"mc-world": M.CH_WORLD, "mc-chat": M.CH_CHAT,
                                              "duet": M.channel_id("duet")})
        self.assertEqual(self.v["signal_table"], M.SIGNAL_TABLE)

    def test_register_vectors(self):
        for case in self.v["register"]:
            f = bytes.fromhex(case["frame"])
            self.assertEqual(M.frame_to_nibbles(f), case["nibbles"])
            self.assertEqual(M.frame_to_words(f), case["words"])
            self.assertEqual(M.frame_to_items(f), case["items"])
            self.assertEqual(M.nibbles_to_frame(case["nibbles"]), f)
            self.assertEqual(M.words_to_frame(case["words"]), f)
            self.assertEqual([M.items_to_signal(i) for i in case["items"]], case["nibbles"])

    def test_event_vectors(self):
        for case in self.v["events"]:
            body = bytes.fromhex(case["body"])
            self.assertEqual(M.event_pack(case["event"]).hex(), case["body"], case["name"])
            self.assertEqual(M.event_unpack(body), case["event"], case["name"])
            frames = M.event_packetize(case["event"], case["packet_id"], case["ts_us"], case["src"])
            self.assertEqual([f.hex() for f in frames], case["frames"], case["name"])
            r = G.GameReassembler()
            got = []
            for f in frames:
                got += r.push(f)
            self.assertEqual(got, [("packet", case["packet_id"], case["ts_us"], G.GMSG_EVENT,
                                    body, G.FLAG_RELIABLE)])

    def test_geometry(self):
        geo = self.v["geometry"]
        o = tuple(geo["origin"])
        for lane in geo["lanes"]:
            for kind in ("out", "cmp", "mid", "in"):
                self.assertEqual(list(M.lane_pos(o, lane["i"], kind)), lane[kind])
        for k, p in geo["ctrl"].items():
            self.assertEqual(list(M.lane_pos(o, 0, k)), [o[0] + p[0], o[1] + p[1], o[2] + p[2]])


if __name__ == "__main__":
    unittest.main()
