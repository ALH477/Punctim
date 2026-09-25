# SPDX-License-Identifier: LGPL-3.0-only
"""DCF-Minecraft datapack generator: structural lint + an executable check of the scoreboard
arithmetic (a tiny interpreter for the mcfunction subset the pack/unpack functions use)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

PYDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(PYDIR)
sys.path.insert(0, os.path.join(PYDIR, "MCP"))
sys.path.insert(0, os.path.join(ROOT, "minecraft", "datapack"))

import mclab_core as M  # noqa: E402
import gen_datapack as GEN  # noqa: E402

ORIGIN = (10, 70, -20)


class MiniScoreboard:
    """Executes the subset: scoreboard players set|add|remove|operation, function, and
    `execute if score <h> <obj> matches <n> run ...`."""

    def __init__(self, functions, ns):
        self.fn, self.ns, self.s = functions, ns, {}

    def get(self, h, o):
        return self.s.get((h, o), 0)

    def run(self, name):
        for line in self.fn[name]:
            self.exec(line)

    def exec(self, line):
        if not line or line.startswith("#"):
            return
        t = line.split()
        if t[0] == "function":
            ns, fn = t[1].split(":")
            assert ns == self.ns
            return self.run(fn)
        if t[0] == "execute":
            i = 1
            while t[i] != "run":
                if t[i] == "if" and t[i + 1] == "score" and t[i + 4] == "matches":
                    if self.get(t[i + 2], t[i + 3]) != int(t[i + 5]):
                        return
                    i += 6
                else:
                    return  # block predicates etc. are out of scope for the interpreter
            return self.exec(" ".join(t[i + 1:]))
        if t[0] == "scoreboard" and t[1] == "players":
            if t[2] in ("set", "add", "remove"):
                h, o, v = t[3], t[4], int(t[5])
                cur = self.get(h, o)
                self.s[(h, o)] = {"set": v, "add": cur + v, "remove": cur - v}[t[2]]
                return
            if t[2] == "operation":
                a, ao, op, b, bo = t[3], t[4], t[5], t[6], t[7]
                x, y = self.get(a, ao), self.get(b, bo)
                if op == "=":
                    r = y
                elif op == "+=":
                    r = x + y
                elif op == "*=":
                    r = x * y
                elif op == "/=":
                    r = x // y          # Minecraft uses floorDiv
                elif op == "%=":
                    r = x % y           # floorMod
                else:
                    raise AssertionError(op)
                self.s[(a, ao)] = r
                return
        # tellraw / setblock / data merge / gamerule / schedule: no-ops here


def generate(tmp, **kw):
    dp = GEN.Datapack(os.path.join(tmp, "dp"), kw.get("ns", "dcf"), ORIGIN,
                      kw.get("pack_format", 48), False, kw.get("autotest", False))
    dp.build_all()
    dp.write()
    return dp


def read_functions(dp):
    out = {}
    for name in dp.functions:
        with open(os.path.join(dp.fn_dir, name + ".mcfunction")) as fh:
            out[name] = [l.rstrip("\n") for l in fh]
    return out


class TestDatapack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dp = generate(cls.tmp)
        cls.fn = read_functions(cls.dp)

    def test_layout(self):
        base = self.dp.out
        with open(os.path.join(base, "pack.mcmeta")) as fh:
            meta = json.load(fh)["pack"]
        self.assertEqual(meta["pack_format"], 48)
        self.assertEqual(meta["min_format"], 48)
        for tag in ("load", "tick"):
            with open(os.path.join(base, "data", "minecraft", "tags", "function", tag + ".json")) as fh:
                self.assertEqual(json.load(fh), {"values": [f"dcf:{tag}"]})
        for name in ("load", "tick", "build", "build_loopback", "sample_in", "pack", "unpack", "rx_write",
                     "rx_commit", "latch", "tx", "tx_from_bytes", "tx_from_words", "tx_done", "clear",
                     "selftest", "selftest_stage2", "selftest_check", "pulse_ack", "pulse_nak", "help"):
            self.assertIn(name, self.fn, name)
        for lines in self.fn.values():
            for l in lines:
                self.assertNotIn("\t", l)

    def test_sample_in_predicates(self):
        """Every IN lane has all 15 non-zero power predicates at its exact cell."""
        for i in range(M.NIBBLES):
            p = GEN.pos(M.lane_pos(ORIGIN, i, "in"))
            for P in range(1, 16):
                line = (f"execute if block {p} minecraft:redstone_wire[power={P}] "
                        f"run scoreboard players set n{i} dcf_reg {P}")
                self.assertIn(line, self.fn["sample_in"], line)
        self.assertEqual(sum(1 for l in self.fn["sample_in"] if l.startswith("execute")), 34 * 15)

    def test_rx_write_counts_match_table(self):
        """Each OUT lane's 16 `data merge` variants carry exactly signal_to_items(P) items."""
        rx = re.compile(r"execute if score n(\d+) dcf_reg matches (\d+) run data merge block (\S+ \S+ \S+) (.*)")
        seen = set()
        for l in self.fn["rx_write"]:
            m = rx.match(l)
            if not m:
                continue
            i, P, p, nbt = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
            self.assertEqual(p, GEN.pos(M.lane_pos(ORIGIN, i, "out")))
            counts = [int(c) for c in re.findall(r"count:(\d+)", nbt)]
            self.assertEqual(sum(counts), M.signal_to_items(P), (i, P))
            self.assertTrue(all(c <= 64 for c in counts) and len(counts) <= M.BARREL_SLOTS)
            seen.add((i, P))
        self.assertEqual(len(seen), 34 * 16)

    def test_pack_unpack_arithmetic(self):
        """nibbles -> words -> nibbles through the generated scoreboard maths == mclab_core."""
        sb = MiniScoreboard(self.fn, "dcf")
        sb.run("load")
        for frame in (M.GOLDEN, bytes(range(17)), bytes([0xFF] * 17), bytes([0xD3, 0x10] + [0] * 15)):
            for i, n in enumerate(M.frame_to_nibbles(frame)):
                sb.s[(f"n{i}", "dcf_reg")] = n
            sb.run("pack")
            self.assertEqual([sb.get(f"w{k}", "dcf_reg") for k in range(6)], M.frame_to_words(frame))
            for i in range(M.NIBBLES):
                sb.s[(f"n{i}", "dcf_reg")] = 0
            sb.run("unpack")
            self.assertEqual([sb.get(f"n{i}", "dcf_reg") for i in range(M.NIBBLES)], M.frame_to_nibbles(frame))
            self.assertEqual([sb.get(f"b{j}", "dcf_reg") for j in range(17)], list(frame))

    def test_tx_and_selftest_words(self):
        sb = MiniScoreboard(self.fn, "dcf")
        sb.run("load")
        sb.run("selftest")      # phase 1: build (block ops are no-ops here)
        sb.run("selftest_stage2")   # phase 2 sets w0..w5 to the golden words
        self.assertEqual([sb.get(f"w{k}", "dcf_reg") for k in range(6)], M.frame_to_words(M.GOLDEN))
        sb.run("tx_from_words")
        self.assertEqual(sb.get("tx_pending", "dcf_ctl"), 1)
        self.assertEqual(sb.get("tx_seq", "dcf_ctl"), 1)
        sb.run("tx_done")
        self.assertEqual(sb.get("tx_pending", "dcf_ctl"), 0)
        # the announce line names all six words in order
        ann = [l for l in self.fn["latch"] if l.startswith("tellraw")][0]
        self.assertEqual(re.findall(r'"name":"(w\d)"', ann), [f"w{k}" for k in range(6)])
        self.assertIn('"DCF TX "', ann)

    def test_geometry_and_control_cells(self):
        strobe = GEN.pos(M.lane_pos(ORIGIN, 0, "strobe_in"))
        self.assertTrue(any(f"unless block {strobe} minecraft:redstone_wire[power=0]" in l for l in self.fn["tick"]))
        for i in range(M.NIBBLES):
            self.assertIn(f"setblock {GEN.pos(M.lane_pos(ORIGIN, i, 'out'))} minecraft:barrel[facing=up]", self.fn["build"])
            self.assertIn(f"setblock {GEN.pos(M.lane_pos(ORIGIN, i, 'in'))} minecraft:redstone_wire", self.fn["build"])
            self.assertIn(f"setblock {GEN.pos(M.lane_pos(ORIGIN, i, 'cmp'))} minecraft:comparator[facing=north]",
                          self.fn["build_loopback"])
        valid = GEN.pos(M.lane_pos(ORIGIN, 0, "valid_out"))
        self.assertIn(f"setblock {valid} minecraft:redstone_block", self.fn["rx_commit"])

    def test_cli(self):
        out = os.path.join(self.tmp, "cli")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "minecraft", "datapack", "gen_datapack.py"),
                            "--out", out, "--namespace", "punctim", "--origin", "1", "2", "3", "--autotest"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(out, "data", "minecraft", "tags", "function", "load.json")) as fh:
            self.assertEqual(json.load(fh), {"values": ["punctim:load"]})
        with open(os.path.join(out, "data", "punctim", "function", "load.mcfunction")) as fh:
            self.assertIn("function punctim:selftest", fh.read())


if __name__ == "__main__":
    unittest.main()
