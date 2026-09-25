# SPDX-License-Identifier: LGPL-3.0-only
"""`punctim mc` end to end: the FakeServer world bridged to one proto peer and one bare peer."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

PYDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PYDIR)
sys.path.insert(0, os.path.join(PYDIR, "MCP"))
sys.path.insert(0, os.path.join(PYDIR, "tests"))

import mclab_core as M  # noqa: E402
import mediumlab_core as ML  # noqa: E402
import wirelab_core as wire  # noqa: E402
from test_minecraft_sidecar import FakeServer, PASSWORD  # noqa: E402

GOLDEN = M.GOLDEN


class TestPunctimMc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.srv = FakeServer(self.tmp)
        self.pw = os.path.join(self.tmp, "rcon.pw")
        with open(self.pw, "w") as fh:
            fh.write(PASSWORD)
        self.proto = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.proto.bind(("127.0.0.1", 0))
        self.proto.settimeout(0.5)
        self.bare = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.bare.bind(("127.0.0.1", 0))
        self.bare.settimeout(0.5)
        self.sidecar_port = self._free_port()

    def tearDown(self):
        self.srv.close()
        self.proto.close()
        self.bare.close()

    @staticmethod
    def _free_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def test_bridge_both_dialects_and_ingress(self):
        cmd = [sys.executable, os.path.join(PYDIR, "punctim.py"), "mc",
               "--rcon", f"127.0.0.1:{self.srv.port}", "--password-file", self.pw,
               "--bind", f"127.0.0.1:{self.sidecar_port}", "--poll-hz", "20",
               "--peer", f"127.0.0.1:{self.proto.getsockname()[1]}/proto",
               "--peer", f"127.0.0.1:{self.bare.getsockname()[1]}/bare",
               "--seconds", "6", "--stats"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.8)
            # egress: the world latches the golden frame -> both peers, each in its dialect
            self.srv.barrels = M.frame_to_items(GOLDEN)
            self.srv.strobe()
            dg_proto, _ = self._recv(self.proto)
            self.assertEqual(len(dg_proto), 34)
            self.assertEqual(ML.proto_frame_decode(dg_proto), GOLDEN)
            dg_bare, _ = self._recv(self.bare)
            self.assertEqual(dg_bare, GOLDEN)            # lone frame, flushed raw after 20 ms
            deadline = time.time() + 3
            while time.time() < deadline and self.srv.scores.get(("tx_pending", M.OBJ_CTL)) != 0:
                time.sleep(0.05)
            self.assertEqual(self.srv.scores[("ack", M.OBJ_CTL)], 1)
            # ingress: a proto datagram to the sidecar's socket -> rx_commit -> barrels. A
            # DIFFERENT frame: the bridge dedups on the 17 bytes for loop-freedom, so the
            # golden frame it just relayed outward would (rightly) be dropped as an echo.
            inbound = wire.encode(0, 7, 0x00B1, M.CH_WORLD, b"\x01\x02\x03\x04", 0x000042)
            self.srv.barrels = [0] * M.NIBBLES
            self.proto.sendto(ML.proto_frame_encode(inbound, 1), ("127.0.0.1", self.sidecar_port))
            deadline = time.time() + 3
            while time.time() < deadline and self.srv.barrels != M.frame_to_items(inbound):
                time.sleep(0.05)
            self.assertEqual(self.srv.barrels, M.frame_to_items(inbound))
            # and the echo IS dropped: the golden frame again changes nothing
            self.proto.sendto(ML.proto_frame_encode(GOLDEN, 2), ("127.0.0.1", self.sidecar_port))
            time.sleep(0.5)
            self.assertEqual(self.srv.barrels, M.frame_to_items(inbound))
        finally:
            out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err)
        stats = json.loads(err.strip().splitlines()[-1])
        self.assertGreaterEqual(stats["relayed"], 2, stats)
        self.assertIn("mc", stats["transports"])

    def _recv(self, sock):
        deadline = time.time() + 4
        while time.time() < deadline:
            try:
                return sock.recvfrom(2048)
            except socket.timeout:
                continue
        self.fail("no datagram")

    def test_usage_exit_codes(self):
        r = subprocess.run([sys.executable, os.path.join(PYDIR, "punctim.py"), "mc"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, os.path.join(PYDIR, "punctim.py"), "mc", "--log", "/dev/null"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)          # nothing to bridge to


if __name__ == "__main__":
    unittest.main()
