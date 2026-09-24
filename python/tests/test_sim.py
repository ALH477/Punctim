# SPDX-License-Identifier: LGPL-3.0-only
"""punctim sim tests: the simulator's EXACT rows against the certified medium codecs
(python/MCP/mediumlab_core.py, cross-checked with medium_vectors.json), its MODEL tables
against their sources (lua/dcf_profile.lua M.media / M.codec_bytes / M.MIN_LIVE_VOICE_BPS,
dcf/sense/model.py), the voice floor, the TDMA plan, the Pipe arithmetic, and the CLI
(--json, --spec, exit codes).

Stdlib only -- runs without numpy:  cd python && python3 -m unittest tests.test_sim -v"""
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PYDIR = os.path.dirname(HERE)
ROOT = os.path.dirname(PYDIR)
sys.path.insert(0, PYDIR)
sys.path.insert(0, os.path.join(PYDIR, "MCP"))

import mediumlab_core as M  # noqa: E402
import wirelab_core as wire  # noqa: E402
from dcf.sim import main, build_plan, normalize, to_json  # noqa: E402
from dcf.sim import media, traffic  # noqa: E402
from dcf.sense import model as sense_model  # noqa: E402

PUNCTIM = os.path.join(PYDIR, "punctim.py")
EXAMPLE = os.path.join(ROOT, "tests", "sim_example.json")
LUA_PROFILE = os.path.join(ROOT, "lua", "dcf_profile.lua")
with open(os.path.join(PYDIR, "MCP", "medium_vectors.json")) as _fh:
    VEC = json.load(_fh)["families"]


def run(*argv):
    """main(argv) in-process -> (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def plan(**spec):
    return build_plan(normalize(spec))


def ev(p, key):
    return dict((m.key, e) for m, e in p["evals"])[key]


def frames(n):
    return [wire.encode(i % 4, i, 0x10 + i, 0xFFFF, bytes([i, i + 1, i + 2, i + 3]), i)
            for i in range(n)]


def hydra_case(profile, fec):
    return next(c for c in VEC["hydra_symbols"]["cases"]
                if c["profile"] == profile and c["fec"] == fec and c["n_tones"] == 2)


def afsk_case(profile, fec):
    return next(c for c in VEC["afsk_bits"]["cases"]
                if c["profile"] == profile and c["fec"] == fec)


class TestExactMedia(unittest.TestCase):
    """EXACT airtime = the certified symbol/bit count / baud."""

    def check_hydra(self, profile, fec, cross_syms, cross_s):
        p = M.hydra_profile(profile, fec=fec)
        want = p["total_syms"] / p["baud"]
        self.assertEqual(p["total_syms"], hydra_case(profile, fec)["total_syms"])
        self.assertEqual(p["total_syms"], len(M.hydra_symbols_encode(p, M.L2_FILLER)))
        med = media.parse_medium(f"hydra:profile={profile},fec={fec}")
        self.assertEqual(med.count, p["total_syms"])
        self.assertAlmostEqual(med.t_frame, want, places=12)
        # cross-checks against the documented figures
        self.assertEqual(med.count, cross_syms)
        self.assertAlmostEqual(med.t_frame, cross_s, places=9)
        j = to_json(plan(medium=f"hydra:profile={profile},fec={fec}"))
        row = j["exact"]["media"][f"hydra:profile={profile},fec={fec}"]
        self.assertEqual(row["units_per_frame"], p["total_syms"])
        self.assertAlmostEqual(row["airtime_per_frame_s"], want, places=9)

    def test_hydra_default_conv(self):
        self.check_hydra("default", "conv", 356, 0.356)

    def test_hydra_default_none(self):
        self.check_hydra("default", "none", 192, 0.192)

    def test_hydra_aux_conv(self):
        self.check_hydra("aux", "conv", 348, 0.2900)

    def test_afsk_handheld_crc8(self):
        bits = M.afsk_bits_encode(M.L2_FILLER, "handheld", False)
        baud = M.AFSK_PROFILES["handheld"]["baud"]
        self.assertEqual(len(bits), afsk_case("handheld", False)["n_bits"])
        med = media.parse_medium("afsk:profile=handheld,fec=0")
        self.assertEqual(med.count, len(bits))
        self.assertAlmostEqual(med.t_frame, len(bits) / baud, places=12)
        self.assertEqual(med.count, 408)                         # cross-check
        self.assertAlmostEqual(med.t_frame, 1.36, places=9)

    def test_afsk_aux_cable(self):
        med = media.parse_medium("afsk:profile=aux-cable")
        self.assertEqual(med.count, afsk_case("aux-cable", False)["n_bits"])
        self.assertAlmostEqual(med.t_frame, 184 / 1200, places=12)

    def test_datagram_costs_match_the_encoders(self):
        for n in range(0, 9):
            fr = frames(n)
            self.assertEqual(media.bare_datagrams(n), [len(d) for d in M.bare_encode(fr)])
            self.assertEqual(media.bare_datagrams(n, pair=False),
                             [len(d) for d in M.bare_encode(fr, pair=False)])
            if n:
                self.assertEqual(media.l2_payloads(n), [len(M.l2_batch(fr))])
            self.assertEqual(sum(len(M.proto_frame_encode(f, i + 1)) for i, f in enumerate(fr)),
                             n * media.PROTO_FRAME_LEN)
        cap = M.l2_capacity(1500)
        self.assertEqual(media.l2_payloads(cap + 1),
                         [len(M.l2_batch(frames(cap))), len(M.l2_batch(frames(1)))])
        udp = media.parse_medium("udp:proto")
        self.assertEqual(udp.link_bytes([3]), 3 * (34 + 28))
        bare = media.parse_medium("udp:bare")
        self.assertEqual(bare.link_bytes([3]), (32 + 28) + (17 + 28))
        l2 = media.parse_medium("l2eth")
        self.assertEqual(l2.link_bytes([1]), 46 + 14)            # padded to the Ethernet minimum
        self.assertEqual(l2.link_bytes([32]), 2 + 16 * 32 + 14)

    def test_measured_airtime_is_exact_plus_constant_lead_tail(self):
        """sense/model.py's measured WAV airtime = the exact symbol time + ~40 ms of lead/tail
        silence, for every FEC mode: the measured and certified figures agree."""
        for fec, air in sense_model.AIRTIME_S.items():
            med = media.parse_medium(f"hydra:fec={fec}")
            self.assertAlmostEqual(air - med.t_frame, 0.040, places=9, msg=fec)


def _lua_table(src, name):
    m = re.search(r"^M\." + name + r"\s*=\s*\{(.*?)^\}", src, re.S | re.M)
    assert m, name
    return m.group(1)


class TestModelMirrorsLua(unittest.TestCase):
    """The MODEL tables are mirrors of lua/dcf_profile.lua -- parsed, not copied."""

    @classmethod
    def setUpClass(cls):
        with open(LUA_PROFILE) as fh:
            cls.src = fh.read()

    def test_media_table_equals_lua(self):
        body = _lua_table(self.src, "media")
        rows = re.findall(r'^\s*(\w+)\s*=\s*\{\s*bps\s*=\s*([0-9.eE+]+)\s*,\s*ip\s*=\s*'
                          r'(true|false)\s*,\s*name\s*=\s*"([^"]*)"\s*\}', body, re.M)
        lua = {k: {"bps": float(b), "ip": ip == "true", "name": n} for k, b, ip, n in rows}
        self.assertEqual(len(lua), 8)
        self.assertEqual(list(lua), list(media.MEDIA))
        for k, v in lua.items():
            self.assertEqual(float(media.MEDIA[k]["bps"]), v["bps"], k)
            self.assertIs(media.MEDIA[k]["ip"], v["ip"], k)
            self.assertEqual(media.MEDIA[k]["name"], v["name"], k)

    def test_codec_bytes_equal_lua(self):
        body = _lua_table(self.src, "codec_bytes")
        lua = {k: int(v) for k, v in re.findall(r'\["([\w-]+)"\]\s*=\s*(\d+)', body)}
        self.assertEqual(lua, traffic.CODEC_BYTES)

    def test_min_live_voice_bps(self):
        m = re.search(r"^M\.MIN_LIVE_VOICE_BPS\s*=\s*([0-9*\s]+?)\s*(--|$)", self.src, re.M)
        self.assertTrue(m)
        self.assertEqual(math.prod(int(x) for x in m.group(1).split("*")),
                         media.MIN_LIVE_VOICE_BPS)
        self.assertEqual(media.MIN_LIVE_VOICE_BPS, 13600)


class TestVoiceFloor(unittest.TestCase):
    def test_hydra_fails_udp_proto_passes(self):
        p = plan(medium="hydra:profile=default,fec=conv", candidates=["udp:proto"],
                 traffic={"audio": {"blocks_per_s": 50, "payload_b": 124}})
        h = ev(p, "hydra:profile=default,fec=conv")
        u = ev(p, "udp:proto")
        self.assertFalse(h["voice_floor_met"])
        self.assertAlmostEqual(h["capacity_bps"], 136 / 0.356, places=6)
        self.assertTrue(any("live-voice floor" in r for r in h["reasons"]))
        self.assertTrue(u["voice_floor_met"])
        self.assertTrue(u["pass"])
        self.assertEqual(p["recommendation"]["medium"], "udp:proto")
        self.assertIn("udp:proto", p["recommendation"]["passing"])

    def test_floor_not_judged_without_audio(self):
        p = plan(medium="hydra", traffic={"text": {"bytes_per_s": 1}})
        self.assertNotIn("voice_floor_met", ev(p, "hydra"))


class TestFragmentation(unittest.TestCase):
    def test_one_plus_ceil_len_over_4(self):
        p = plan(medium="hydra", traffic={"audio": {"payload_b": 124}, "text": {"bytes_per_s": 20},
                                          "sstv": {"bytes": 8188}, "sense": {},
                                          "game": {"payload_b": 14}})
        by = {a.name: a for a in p["adapters"]}
        self.assertEqual(by["audio"].frames_per_msg, 1 + math.ceil(124 / 4))
        self.assertEqual(by["text"].frames_per_msg, 1 + math.ceil(20 / 4))
        self.assertEqual(by["sstv"].frames_per_msg, 1 + math.ceil(8188 / 4))
        self.assertEqual(by["game"].frames_per_msg, 1 + math.ceil(14 / 4))
        self.assertEqual(by["sense"].frames_per_msg, 1)
        self.assertEqual(by["audio"].frame_type, "CTRL")

    def test_long_text_chains(self):
        p = plan(medium="hydra", traffic={"text": {"bytes_per_s": 5000, "msg_bytes": 5000}})
        a = p["adapters"][0]
        self.assertEqual(a.units, [1 + 4092 // 4, 1 + math.ceil((5000 - 4092) / 4)])


class TestTdma(unittest.TestCase):
    def test_hydra_conv_4_nodes_fits(self):
        p = plan(nodes=4, medium="hydra:profile=default,fec=conv", mac="tdma", guard_ms=50,
                 traffic={"sense": {"interval_s": 60}})
        t = ev(p, "hydra:profile=default,fec=conv")["tdma"]
        air = M.hydra_profile("default", fec="conv")["total_syms"] / 1000.0
        self.assertAlmostEqual(t["slot_s"], air + 2 * 0.050, places=12)
        self.assertAlmostEqual(t["window_s"], air, places=12)
        self.assertAlmostEqual(t["cycle_s"], 4 * (air + 0.1), places=12)
        self.assertTrue(t["fits"])

    def test_afsk_handheld_200_nodes_does_not_fit(self):
        key = "afsk:profile=handheld,fec=0"
        p = plan(nodes=200, medium=key, mac="tdma", guard_ms=50,
                 traffic={"sense": {"interval_s": 60}})
        e = ev(p, key)
        self.assertAlmostEqual(e["tdma"]["cycle_s"], 200 * (1.36 + 0.1), places=9)
        self.assertFalse(e["tdma"]["fits"])
        self.assertFalse(e["pass"])

    def test_fdma_multiplies_capacity(self):
        p = plan(nodes=500, medium="hydra", mac="fdma", traffic={"sense": {"interval_s": 60}})
        e = ev(p, "hydra")
        self.assertEqual(e["fdma"]["max_channels"], 11)          # sense/mac.Fdma Nyquist rule
        self.assertEqual(e["tdma"]["slots_per_cycle"], math.ceil(500 / 11))
        self.assertTrue(e["tdma"]["fits"])


class TestQueueAndPipe(unittest.TestCase):
    def test_queue_time_to_overflow(self):
        p = plan(medium="hydra", traffic={"audio": {"payload_b": 124}})
        q = ev(p, "hydra")["queue"]
        fin, fout = 50 * 32, 1 / 0.356
        self.assertAlmostEqual(q["in_frames_per_s"], fin, places=9)
        self.assertAlmostEqual(q["out_frames_per_s"], fout, places=9)
        self.assertAlmostEqual(q["overflow_s"], 256 / (fin - fout), places=9)

    def test_pipe_rounds_and_data_lane_match_the_sender(self):
        from dcf.pipe.protocol import PipeSender, PROFILES
        for prof, n in (("hydramodem", 2000), ("lan", 5000), ("hydramodem", 256)):
            pp = traffic.pipe_plan({"bytes": n, "profile": prof}, "lan")
            c = PROFILES[prof]
            s = PipeSender(1, bytes(range(256)) * (n // 256) + bytes(n % 256), c["chunk_size"],
                           c["nparity"])
            self.assertEqual(pp["chunks"], s.n)
            self.assertEqual(pp["rounds"], math.ceil(s.n / c["credit_window"]))
            self.assertEqual(pp["data_lane_bytes"],
                             sum(len(s._chunk_payload(i)) + 6 for i in range(s.n)))

    def test_pipe_on_hydra_is_frame_time(self):
        p = plan(medium="hydra", traffic={"pipe": {"bytes": 1024, "profile": "hydramodem"}})
        pe = ev(p, "hydra")["pipe"]
        self.assertEqual(pe["frames"], 4 * math.ceil((256 + 6) / 4))
        self.assertAlmostEqual(pe["seconds"], pe["frames"] * 0.356, places=9)


class TestCli(unittest.TestCase):
    def test_json_has_three_keys(self):
        rc, out, _ = run("--spec", EXAMPLE, "--json")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(set(d), {"exact", "model", "recommendation"})
        self.assertEqual(d["exact"]["min_live_voice_bps"], 13600)
        self.assertEqual(d["exact"]["queue_max"], 256)
        r = d["recommendation"]
        self.assertIn(r["medium"], ("udp:proto", "udp:bare", "l2eth"))
        self.assertEqual(r["hardware_class"], "mcu")

    def test_text_report_tags(self):
        rc, out, _ = run("--spec", EXAMPLE)
        self.assertEqual(rc, 0)
        self.assertIn("== EXACT", out)
        self.assertIn("== MODEL", out)
        self.assertIn("RECOMMEND", out)
        self.assertRegex(out, r"EXACT\s+hydra:profile=default,fec=conv\s+356 symbols")

    def test_absent_traffic_rows_are_omitted(self):
        rc, out, _ = run("--medium", "hydra", "--text-bps", "4")
        self.assertEqual(rc, 0)
        self.assertNotIn("audio", out.split("== MODEL")[0])
        self.assertNotIn("DCF-Pipe", out)

    def test_bad_input_exits_2(self):
        for argv in ([], ["--json"], ["--medium", "bogus:"], ["--nodes", "0"],
                     ["--mac", "nope"], ["--audio-payload", "125"],
                     ["--medium", "file:path=x"]):
            rc, _, err = run(*argv)
            self.assertEqual(rc, 2, argv)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"nodes": 4, "typo": 1}')
        try:
            self.assertEqual(run("--spec", fh.name)[0], 2)
        finally:
            os.unlink(fh.name)
        self.assertEqual(run("--spec", os.path.join(ROOT, "no", "such.json"))[0], 1)

    def test_candidates_keep_uri_commas(self):
        from dcf.sim.media import split_candidates
        self.assertEqual(split_candidates("udp:proto,hydra:profile=aux,fec=conv,l2eth,janus"),
                         ["udp:proto", "hydra:profile=aux,fec=conv", "l2eth", "janus"])
        self.assertEqual(split_candidates("hydra:fec=conv;udp:bare,link=wifi"),
                         ["hydra:fec=conv", "udp:bare,link=wifi"])

    def test_punctim_hook_spec_example_exits_0(self):
        r = subprocess.run([sys.executable, PUNCTIM, "sim", "--spec", EXAMPLE],
                           capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn(b"RECOMMEND", r.stdout)
        r = subprocess.run([sys.executable, PUNCTIM, "sim", "--spec", EXAMPLE, "--json"],
                           capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(set(json.loads(r.stdout)), {"exact", "model", "recommendation"})


if __name__ == "__main__":
    unittest.main()
