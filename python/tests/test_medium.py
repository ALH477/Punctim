# SPDX-License-Identifier: LGPL-3.0-only
"""DCF-Medium tests: the reference medium codecs (python/MCP/mediumlab_core.py) against the
committed certificate (python/MCP/medium_vectors.json — so this file doubles as the Python
cert), the StreamScanner chunk invariance, the URI grammar (dcf.medium.parse_uri), the
transports behind it, and `punctim io` round trips between real processes (hex -> .dcf ->
stdio -> hex, UDP proto/bare over 127.0.0.1, loop:, l2eth:impl=loop).

Stdlib only — runs without numpy:  cd python && python3 -m unittest tests.test_medium -v
Spec: Documentation/DCF_MEDIUM_SPEC.md."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PYDIR = os.path.dirname(HERE)
sys.path.insert(0, PYDIR)
sys.path.insert(0, os.path.join(PYDIR, "MCP"))

import mediumlab_core as M  # noqa: E402
import wirelab_core as wire  # noqa: E402
from dcf import medium  # noqa: E402
from dcf import transport as T  # noqa: E402

PUNCTIM = os.path.join(PYDIR, "punctim.py")
VECTORS = os.path.join(PYDIR, "MCP", "medium_vectors.json")
with open(VECTORS) as _fh:
    V = json.load(_fh)
FAM = V["families"]


def fx(hexes):
    return [bytes.fromhex(h) for h in hexes]


def corpus(n=40):
    """A deterministic set of valid frames (all 4 types, varied fields)."""
    return [wire.encode(i % 4, (i * 0x0101) & 0xFFFF, 0x00A1 + i, (0xFFFF - i) & 0xFFFF,
                        bytes([(i * 7 + k) & 0xFF for k in range(4)]), (i * 0x010203) & 0xFFFFFF)
            for i in range(n)]


def punctim(*args, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PUNCTIM, *args], input=stdin,
                          capture_output=True, timeout=timeout)


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_udp_bound(port, proc, timeout=10.0):
    """Wait until some socket is bound to UDP `port` (Linux /proc/net/udp), else sleep."""
    tag = f":{port:04X} "
    end = time.monotonic() + timeout
    if not os.path.exists("/proc/net/udp"):
        time.sleep(1.0)
        return
    while time.monotonic() < end and proc.poll() is None:
        with open("/proc/net/udp") as fh:
            if any(tag in line.split("rem_address")[0] or f"0100007F{tag}" in line
                   for line in fh.read().splitlines()[1:]):
                return
        time.sleep(0.02)


# ══ the codecs vs the certificate ═════════════════════════════════════════════
class TestMediumVectors(unittest.TestCase):
    def test_certify_all_families(self):
        for fam, ok, n, msg in medium.certify_vectors(V):
            self.assertTrue(ok, f"{fam}: {msg}")
            self.assertGreater(n, 0, fam)

    def test_documentation_copy_identical(self):
        doc = os.path.join(PYDIR, "..", "Documentation", "medium_vectors.json")
        if not os.path.exists(doc):
            self.skipTest("no Documentation/ tree")
        with open(doc, "rb") as a, open(VECTORS, "rb") as b:
            self.assertEqual(a.read(), b.read(), "Documentation/ and python/MCP/ copies differ")

    def test_anchors(self):
        a = V["anchors"]
        self.assertEqual((a["crc_123456789"], a["crc_zero15"]), (0x29B1, 0x4EC3))
        self.assertEqual((a["frame_len"], a["proto_header_len"], a["msg_frame"]), (17, 17, 12))
        self.assertEqual((a["super_len"], a["l2_hdr"]), (32, 2))
        self.assertEqual(bytes.fromhex(a["l2_filler"]), M.L2_FILLER)
        self.assertEqual((a["hydra_sync_word"], a["afsk_sync"]), (0x2DD4, 0x7E))
        for b in V["basis"]:
            self.assertEqual(wire.encode(b["type"], b["seq"], b["src"], b["dst"],
                                         bytes.fromhex(b["payload"]), b["ts"]).hex(), b["hex"])

    def test_stream_family(self):
        for c in FAM["stream"]["cases"]:
            got = M.stream_decode(bytes.fromhex(c["input"]))
            self.assertEqual(got, (fx(c["frames"]), c["skipped_bytes"], c["tail_bytes"]),
                             c["name"])

    def test_hex_family(self):
        for c in FAM["hex"]["cases"]:
            self.assertEqual(M.hex_encode(fx(c["frames"])), c["text"], c["name"])
            self.assertEqual(M.hex_decode(c["decode_input"]),
                             (fx(c["decoded"]), c["bad_lines"]), c["name"])

    def test_udp_proto_family(self):
        self.assertEqual(FAM["udp_proto"]["types"]["FRAME"], 12)
        for c in FAM["udp_proto"]["cases"]:
            dg = bytes.fromhex(c["datagram"])
            self.assertEqual(M.proto_encode(c["type"], c["seq"], c["ts"],
                                            bytes.fromhex(c["payload"])), dg, c["name"])
            self.assertEqual(M.proto_frame_decode(dg) is not None, c["accept_as_frame"])
        golden = [c for c in FAM["udp_proto"]["cases"] if c["name"] == "go_golden"][0]
        self.assertEqual(golden["datagram"], "010000002a010203040506070800000003010203")

    def test_udp_proto_matches_dcf_proto(self):
        from dcf.proto import ProtoMessage
        f = fx([V["basis"][1]["hex"]])[0]
        self.assertEqual(ProtoMessage(12, 5, f, timestamp=0).serialize(),
                         M.proto_frame_encode(f, 5, 0))
        with self.assertRaises(ValueError):
            M.proto_decode(bytes(16))

    def test_udp_bare_family(self):
        for c in FAM["udp_bare"]["cases"]:
            self.assertEqual(M.bare_encode(fx(c["frames"])), fx(c["datagrams"]), c["name"])

    def test_l2eth_family_and_reexport(self):
        from dcf import l2eth
        self.assertIs(l2eth.batch, M.l2_batch)
        self.assertIs(l2eth.unbatch, M.l2_unbatch)
        self.assertEqual(l2eth._FILLER, M.L2_FILLER)
        for c in FAM["l2eth"]["cases"]:
            self.assertEqual(l2eth.batch(fx(c["frames"])).hex(), c["payload"], c["name"])

    def test_hydra_family_and_anchors(self):
        cases = FAM["hydra_symbols"]["cases"]
        self.assertEqual(len(cases), 74)
        for c in cases:
            p = M.hydra_profile(c["profile"], fec=c["fec"], interleave=c["interleave"],
                                n_tones=c["n_tones"])
            self.assertEqual(M.hydra_symbols_encode(p, bytes.fromhex(c["frame"])),
                             c["symbols"], c["name"])
        want = {("default", "none"): 192, ("default", "rep3"): 496, ("default", "conv"): 356,
                ("aux", "none"): 184, ("aux", "rep3"): 488, ("aux", "conv"): 348}
        for (prof, fec), tot in want.items():
            p = M.hydra_profile(prof, fec=fec)
            self.assertEqual(p["total_syms"], tot)
        self.assertEqual(M.hydra_profile("default")["fec_mode"], M.FEC_CONV)   # not "FEC off"
        self.assertEqual(M.hydra_profile("default")["interleave"], 1)

    def test_hydra_conv_corrects_errors(self):
        f = fx([V["basis"][2]["hex"]])[0]
        bits = M.hydra_bytes_to_bits(f + wire.crc16_ccitt(f).to_bytes(2, "big"))
        coded = M.hydra_conv_encode(bits)
        self.assertEqual(len(coded), 316)
        for pos in ([5], [0, 100], [17, 150, 290], [3, 90, 180, 300]):
            bad = list(coded)
            for q in pos:
                bad[q] ^= 1
            self.assertEqual(M.hydra_conv_decode_hard(bad), bits, pos)

    def test_afsk_family(self):
        for c in FAM["afsk_bits"]["cases"]:
            self.assertEqual(M.afsk_bits_encode(bytes.fromhex(c["frame"]), c["profile"],
                                                c["fec"]), c["bits"], c["name"])


# ══ StreamScanner ═════════════════════════════════════════════════════════════
class TestStreamScanner(unittest.TestCase):
    def _chunked(self, buf, sizes):
        sc = M.StreamScanner()
        out, i, k = [], 0, 0
        while i < len(buf):
            n = sizes[k % len(sizes)]
            out += sc.feed(buf[i:i + n])
            i += n
            k += 1
        return out, sc.skipped_bytes, len(sc.flush())

    def test_every_chunk_size_matches_batch_decode(self):
        for c in FAM["stream"]["cases"]:
            buf = bytes.fromhex(c["input"])
            want = (fx(c["frames"]), c["skipped_bytes"], c["tail_bytes"])
            for n in range(1, 40):
                self.assertEqual(self._chunked(buf, [n]), want, (c["name"], n))
            self.assertEqual(self._chunked(buf, [1, 16, 2, 17, 5, 33]), want, c["name"])

    def test_frame_split_across_every_boundary(self):
        f1, f2 = corpus(2)
        buf = b"\x00" + f1 + f2
        for cut in range(len(buf) + 1):
            sc = M.StreamScanner()
            got = sc.feed(buf[:cut]) + sc.feed(buf[cut:])
            self.assertEqual(got, [f1, f2], cut)
            self.assertEqual(sc.skipped_bytes, 1)
            self.assertLessEqual(sc.pending, 16)

    def test_carry_never_exceeds_16(self):
        sc = M.StreamScanner()
        for b in bytes(range(256)) * 3:
            sc.feed(bytes([b]))
            self.assertLessEqual(sc.pending, 16)

    def test_garbage_injected_corpus(self):
        fs = corpus(30)
        buf = bytearray()
        for i, f in enumerate(fs):
            buf += bytes([0xD3, 0x10 | (i % 16)] * (i % 3)) + f
        buf += b"\xd3\x13\x00"
        got, skipped, tail = M.stream_decode(bytes(buf))
        self.assertEqual(got, fs)
        self.assertEqual(tail, 3)
        self.assertEqual(skipped, sum(2 * (i % 3) for i in range(30)))


# ══ URI grammar ═══════════════════════════════════════════════════════════════
class TestParseUri(unittest.TestCase):
    def test_cases(self):
        P = medium.parse_uri
        self.assertEqual(P("stdio:"), ("stdio", {}))
        self.assertEqual(P("stdio"), ("stdio", {}))
        self.assertEqual(P("hex:"), ("hex", {}))
        self.assertEqual(P("HEX:path=/tmp/a=b.hex"), ("hex", {"path": "/tmp/a=b.hex"}))
        self.assertEqual(P("file:path=a.dcf,append=1,follow=0"),
                         ("file", {"path": "a.dcf", "append": "1", "follow": "0"}))
        self.assertEqual(P("udp:dialect=bare,bind=127.0.0.1:9100,peer=a:1|b:2,name=up"),
                         ("udp", {"dialect": "bare", "bind": "127.0.0.1:9100",
                                  "peer": "a:1|b:2", "name": "up"}))
        self.assertEqual(P("udp:peer=x:1,peer=y:2")[1]["peer"], "y:2")      # last wins
        self.assertEqual(P("hydra:in=/r,out=/t,profile=aux,fec=rep3,interleave=0")[1]["fec"],
                         "rep3")
        self.assertEqual(P("l2eth:if=eth0,ethertype=0x88B6")[1]["ethertype"], "0x88B6")
        self.assertEqual(P("loop:id=bus"), ("loop", {"id": "bus"}))
        self.assertEqual(P("udp:,bind=h:1,"), ("udp", {"bind": "h:1"}))       # empty items
        self.assertEqual(medium.multi("a:1|b:2||"), ["a:1", "b:2"])
        for bad in ("", "nosuch:x=1", "udp:bogus=1", "udp:bind", "file:in"):
            with self.assertRaises(medium.UsageError, msg=bad):
                P(bad)

    def test_finite(self):
        self.assertTrue(medium.finite("stdio:"))
        self.assertTrue(medium.finite("hex:"))
        self.assertTrue(medium.finite("file:path=a.dcf"))
        self.assertFalse(medium.finite("file:path=a.dcf,follow=1"))
        self.assertFalse(medium.finite("hex:follow=1"))
        for s in ("udp:bind=h:1", "l2eth:", "loop:", "hydra:in=d", "afsk:in=d"):
            self.assertFalse(medium.finite(s), s)

    def test_bridge_delegates_legacy_strings(self):
        from dcf.bridge import _make_transport
        t = _make_transport("udp:bind=127.0.0.1:0,peer=1@127.0.0.1:9")
        self.assertIsInstance(t, T.UdpTransport)
        self.assertEqual(t._peers, [("127.0.0.1", 9)])
        self.assertEqual(t.dialect, "proto")
        d = tempfile.mkdtemp()
        f = _make_transport(f"file:in={d}/a.dcf,out={d}/b.dcf")
        self.assertIsInstance(f, T.FileTransport)
        self.assertTrue(f._follow and f._append)            # bridge keeps tail + append
        self.assertIsInstance(_make_transport("hex:"), T.HexTransport)
        self.assertIsInstance(_make_transport("stdio:"), T.StdioTransport)
        self.assertIsInstance(_make_transport("loop:id=x"), T.LoopbackTransport)
        from dcf import l2eth
        self.assertIsInstance(_make_transport("l2eth:"), l2eth.L2LoopbackTransport)
        with self.assertRaises(SystemExit):
            _make_transport("warp:x=1")


# ══ transports ════════════════════════════════════════════════════════════════
class TestTransports(unittest.TestCase):
    def test_file_resync_and_no_follow(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "g.dcf")
        c = [x for x in FAM["stream"]["cases"] if x["name"] == "garbage_between"][0]
        with open(p, "wb") as fh:
            fh.write(bytes.fromhex(c["input"]))
        got = []
        t = T.FileTransport("rx", in_path=p, follow=False)
        t.start(lambda f, m: got.append(f))
        self.assertTrue(t.eof.wait(5))
        t.stop()
        self.assertEqual(got, fx(c["frames"]))
        self.assertEqual(t.skipped_bytes, c["skipped_bytes"])

    def test_file_truncate_vs_append(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "o.dcf")
        f1, f2 = corpus(2)
        for append, want in ((False, f2), (True, f2 + f2)):
            t = T.FileTransport("tx", out_path=p, append=append)
            t.send_now(f2)
            t.stop()
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), want, append)

    def test_hex_transport_file(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.hex")
        fs = corpus(5)
        t = T.HexTransport("tx", path=p, mode="w")
        for f in fs:
            t.send_now(f)
        t.stop()
        with open(p) as fh:
            self.assertEqual(fh.read(), M.hex_encode(fs))
        got = []
        r = T.HexTransport("rx", path=p, mode="r")
        r.start(lambda f, m: got.append(f))
        self.assertTrue(r.eof.wait(5))
        r.stop()
        self.assertEqual(got, fs)

    def test_loop_registry(self):
        a = T.LoopbackTransport("a", T.loop_medium("t-reg"))
        b = T.LoopbackTransport("b", T.loop_medium("t-reg"))
        got = []
        b.start(lambda f, m: got.append(f))
        a.send_now(corpus(1)[0])
        b.stop()
        self.assertEqual(got, corpus(1))
        self.assertIs(T.loop_medium("t-reg"), T.loop_medium("t-reg"))

    def _udp_pair(self, dialect, **kw):
        rx = T.UdpTransport("rx", bind=("127.0.0.1", 0), dialect=dialect)
        got = []
        rx.start(lambda f, m: got.append(f))
        tx = T.UdpTransport("tx", bind=("127.0.0.1", 0), peers=[("127.0.0.1", rx.port)],
                            dialect=dialect, **kw)
        return rx, tx, got

    def test_udp_bare_pairs_and_flushes_lone_frame(self):
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(2.0)
        fs = corpus(3)
        tx = T.UdpTransport("tx", bind=("127.0.0.1", 0), peers=[sink.getsockname()],
                            dialect="bare", flush_ms=30)
        tx.start(lambda f, m: None)
        for f in fs:
            tx.send(f)
        dgs = [sink.recvfrom(100)[0] for _ in range(2)]   # the lone 3rd frame after 30 ms
        tx.stop()
        sink.close()
        self.assertEqual(dgs, M.bare_encode(fs))
        self.assertEqual([len(x) for x in dgs], [32, 17])

    def test_udp_dialects_roundtrip_in_process(self):
        for dialect in ("proto", "bare"):
            rx, tx, got = self._udp_pair(dialect)
            fs = corpus(9)
            for f in fs:
                tx.send_now(f)
            tx.flush()
            end = time.monotonic() + 3
            while len(got) < len(fs) and time.monotonic() < end:
                time.sleep(0.01)
            tx.stop()
            rx.stop()
            self.assertEqual(got, fs, dialect)

    def test_udp_proto_rx_ignores_adapter_types_counts_garbage(self):
        rx = T.UdpTransport("rx", bind=("127.0.0.1", 0))
        got = []
        rx.start(lambda f, m: got.append(f))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        f = corpus(1)[0]
        for dg in (M.proto_encode(7, 1, 0, b""), b"\x01\x02", M.proto_frame_encode(f, 2)):
            s.sendto(dg, ("127.0.0.1", rx.port))
        end = time.monotonic() + 3
        while not got and time.monotonic() < end:
            time.sleep(0.01)
        rx.stop()
        s.close()
        self.assertEqual(got, [f])
        self.assertEqual(rx.invalid_datagrams, 1)

    def test_udp_node_accepts_msg_frame(self):
        from dcf.udp_node import DcfNode
        n = DcfNode("127.0.0.1", 0, "9")
        got = []
        n.on_frame = lambda f, addr: got.append(f)
        n.start()
        f = corpus(1)[0]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(M.proto_frame_encode(f, 1), ("127.0.0.1", n._sock.getsockname()[1]))
        end = time.monotonic() + 3
        while not got and time.monotonic() < end:
            time.sleep(0.01)
        n.stop()
        s.close()
        self.assertEqual(got, [f])

    def test_hydra_tool_caps_and_unsupported(self):
        d = tempfile.mkdtemp()
        tool = os.path.join(d, "frame_tx_old")
        with open(tool, "w") as fh:
            fh.write("#!/bin/sh\necho 'usage: frame_tx <hex> out.wav [--none|--rep3|--conv]' >&2\n"
                     "exit 2\n")
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)
        self.assertEqual(T.hydra_tool_caps(tool), set())
        t = T.HydraTransport("h", tx_bin=tool, rx_bin=tool, profile="aux", out_dir=d)
        self.assertEqual(t._prof[:6], T.HYDRA_AUX_FLAGS)
        with self.assertRaises(T.MediumUnsupported):
            T.HydraTransport("h", tx_bin=tool, rx_bin=tool, interleave=0, out_dir=d)
        new = os.path.join(d, "frame_tx_new")
        with open(new, "w") as fh:
            fh.write("#!/bin/sh\necho 'usage: frame_tx <hex> out.wav [--profile default|aux]"
                     " [--interleave 0|1] [--preamble N]' >&2\nexit 2\n")
        os.chmod(new, os.stat(new).st_mode | stat.S_IEXEC)
        t = T.HydraTransport("h", tx_bin=new, rx_bin=new, profile="aux", interleave=0,
                             out_dir=d)
        self.assertEqual(t._prof, ["--profile", "aux", "--interleave", "0"])

    def test_missing_optional_media_are_unsupported(self):
        if not (T.hydramodem_available()):
            with self.assertRaises(T.MediumUnsupported):
                medium.make_transport("hydra:out=" + tempfile.mkdtemp(), "out")
        try:
            import numpy  # noqa: F401
        except ImportError:
            with self.assertRaises(T.MediumUnsupported):
                medium.make_transport("afsk:out=" + tempfile.mkdtemp(), "out")


# ══ punctim (subprocess) ══════════════════════════════════════════════════════
class TestPunctimCli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.fs = corpus(40)
        self.hex_in = os.path.join(self.d, "in.hex")
        with open(self.hex_in, "w") as fh:
            fh.write(M.hex_encode(self.fs))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_version_encode_decode(self):
        r = punctim("version")
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout.decode(), r"^punctim \S+ \(python\)\n$")
        j = json.loads(punctim("version", "--json").stdout)
        self.assertEqual((j["name"], j["impl"]), ("punctim", "python"))
        r = punctim("encode", "--type", "3", "--seq", "0x1234", "--src", "1", "--dst", "0xFFFF",
                    "--payload", "deadbeef", "--ts", "0xAB12CD")
        self.assertEqual(r.stdout, b"d31312340001ffffdeadbeefab12cd24c0\n")
        r = punctim("encode", "--type", "0", "--seq", "1", "--src", "2", "--dst", "3",
                    "--text", "hi")
        self.assertEqual(r.stdout.decode().strip(),
                         wire.encode(0, 1, 2, 3, b"hi\x00\x00", 0).hex())
        self.assertEqual(punctim("encode", "--type", "0", "--seq", "1", "--src", "2",
                                 "--dst", "3", "--text", "toolong").returncode, 2)
        r = punctim("decode", "--json", "d31312340001ffffdeadbeefab12cd24c0")
        rec = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0)
        want = wire.decode(bytes.fromhex("d31312340001ffffdeadbeefab12cd24c0"))
        for k, v in want.items():
            self.assertEqual(rec[k], v, k)
        self.assertEqual((rec["valid"], rec["syndrome"]), (True, 0))
        self.assertEqual(punctim("decode", "d31312340001ffffdeadbeefab12cd24c1").returncode, 5)
        r = punctim("decode", "--stdin", "--json", stdin=M.hex_encode(self.fs[:3]).encode())
        self.assertEqual([json.loads(x)["hex"] for x in r.stdout.splitlines()],
                         [f.hex() for f in self.fs[:3]])

    def test_certify(self):
        r = punctim("certify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(b"ALL MEDIUM VECTORS PASS", r.stdout)
        r = punctim("certify", "--family", "stream", "hex")
        self.assertEqual(r.returncode, 0)
        bad = json.loads(json.dumps(V))
        bad["families"]["hex"]["cases"][0]["text"] = "00\n"
        with open(os.path.join(self.d, "medium_vectors.json"), "w") as fh:
            json.dump(bad, fh)
        r = punctim("certify", "--vectors", self.d)
        self.assertEqual(r.returncode, 4)
        self.assertIn(b"FAIL hex", r.stdout)

    def test_exit_codes(self):
        self.assertEqual(punctim("io", "--in", "warp:", "--out", "hex:").returncode, 2)
        self.assertEqual(punctim("io", "--in", "hex:", "--out", "udp:").returncode, 2)
        self.assertEqual(punctim("io", "--in", "file:path=/nonexistent/x.dcf",
                                 "--out", "hex:").returncode, 1)
        self.assertEqual(punctim("io", "--in", "hex:", "--out", "hex:", "--expect", "1",
                                 stdin=b"").returncode, 6)
        if not T.hydramodem_available():
            r = punctim("io", "--in", "hex:", "--out", "hydra:out=" + self.d, stdin=b"")
            self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(punctim("sim").returncode, 2)
        self.assertEqual(punctim("bogus").returncode, 2)

    def test_hex_file_stdio_hex_is_byte_identical(self):
        dcf = os.path.join(self.d, "a.dcf")
        r = punctim("io", "--in", "hex:path=" + self.hex_in, "--out", "file:path=" + dcf,
                    "--stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(r.stderr.decode().strip().splitlines()[-1])
        self.assertEqual(list(st), list(medium.STATS_KEYS))
        self.assertEqual((st["frames_in"], st["frames_out"], st["invalid_frames"]), (40, 40, 0))
        with open(dcf, "rb") as fh:
            self.assertEqual(fh.read(), b"".join(self.fs))
        with open(dcf, "rb") as fh:
            r1 = subprocess.run([sys.executable, PUNCTIM, "io", "--in", "stdio:", "--out",
                                 "stdio:"], stdin=fh, capture_output=True, timeout=60)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = punctim("io", "--in", "stdio:", "--out", "hex:", stdin=r1.stdout)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        with open(self.hex_in, "rb") as fh:
            self.assertEqual(r2.stdout, fh.read())

    def test_garbage_twin_stats(self):
        c = [x for x in FAM["stream"]["cases"] if x["name"] == "garbage_prefix"][0]
        g = os.path.join(self.d, "g.dcf")
        with open(g, "wb") as fh:
            fh.write(bytes.fromhex(c["input"]))
        r = punctim("io", "--in", "file:path=" + g, "--out", "hex:", "--stats")
        st = json.loads(r.stderr.decode().strip().splitlines()[-1])
        self.assertEqual(st["skipped_bytes"], c["skipped_bytes"])
        self.assertEqual(r.stdout.decode(), M.hex_encode(fx(c["frames"])))
        # hex bad lines + ungated lines are counted, never written
        h = [x for x in FAM["hex"]["cases"] if x["name"] == "ungated_line"][0]
        r = punctim("io", "--in", "hex:", "--out", "hex:", "--stats",
                    stdin=h["decode_input"].encode())
        st = json.loads(r.stderr.decode().strip().splitlines()[-1])
        self.assertEqual((st["frames_in"], st["frames_out"], st["invalid_frames"]), (2, 1, 1))
        r = punctim("io", "--in", "hex:", "--out", "hex:", "--no-validate",
                    stdin=h["decode_input"].encode())
        self.assertEqual(r.stdout.decode(), M.hex_encode(fx(h["decoded"])))

    def _udp_cli(self, dialect):
        port = free_udp_port()
        rx = subprocess.Popen([sys.executable, PUNCTIM, "io", "--in",
                               f"udp:dialect={dialect},bind=127.0.0.1:{port}", "--out", "hex:",
                               "--expect", str(len(self.fs)), "--seconds", "20", "--stats"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_udp_bound(port, rx)
            tx = punctim("io", "--in", "hex:path=" + self.hex_in, "--out",
                         f"udp:dialect={dialect},peer=127.0.0.1:{port}",
                         "--expect", str(len(self.fs)))
            self.assertEqual(tx.returncode, 0, tx.stderr)
            out, err = rx.communicate(timeout=40)
        finally:
            if rx.poll() is None:
                rx.kill()
        self.assertEqual(rx.returncode, 0, err)
        self.assertEqual(out.decode(), M.hex_encode(self.fs))

    def test_udp_proto_between_processes(self):
        self._udp_cli("proto")

    def test_udp_bare_between_processes(self):
        self._udp_cli("bare")

    def test_udp_writer_datagrams_are_deterministic(self):
        for dialect, want in (("proto", [M.proto_frame_encode(f, i + 1, 0)
                                         for i, f in enumerate(self.fs)]),
                              ("bare", M.bare_encode(self.fs))):
            sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sink.bind(("127.0.0.1", 0))
            sink.settimeout(5.0)
            port = sink.getsockname()[1]
            r = punctim("io", "--in", "hex:path=" + self.hex_in, "--out",
                        f"udp:dialect={dialect},peer=127.0.0.1:{port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            got = [sink.recvfrom(100)[0] for _ in want]
            sink.close()
            self.assertEqual(got, want, dialect)

    def test_loop_and_l2eth_loop(self):
        for uri in ("loop:id=cli", "l2eth:impl=loop"):
            r = punctim("io", "--in", "hex:path=" + self.hex_in, "--out", uri, "--stats")
            self.assertEqual(r.returncode, 0, r.stderr)
            st = json.loads(r.stderr.decode().strip().splitlines()[-1])
            self.assertEqual(st["frames_out"], 40, uri)

    def test_loop_and_l2eth_loop_in_process(self):
        for uri in ("loop:id=inproc", "l2eth:impl=loop,id=inproc", "l2eth:id=inproc2"):
            out = os.path.join(self.d, "o.hex")
            res = {}
            th = threading.Thread(target=lambda: res.update(medium.run_io(
                uri, "hex:path=" + out, expect=len(self.fs), seconds=10)))
            th.start()
            mid = ("l2eth/" + uri.split("id=")[1]) if uri.startswith("l2eth") else "inproc"
            end = time.monotonic() + 5
            while not any(p._on_frame for p in T.loop_medium(mid).ports) and \
                    time.monotonic() < end:           # the reader has started
                time.sleep(0.01)
            w = medium.open_writer(uri)
            for f in self.fs:
                w.write(f)
            w.close()
            th.join(15)
            self.assertEqual(res["frames_out"], len(self.fs), uri)
            with open(out) as fh:
                self.assertEqual(fh.read(), M.hex_encode(self.fs), uri)


if __name__ == "__main__":
    unittest.main()
