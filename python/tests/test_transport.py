# SPDX-License-Identifier: LGPL-3.0-only
"""Tests for the DCF transport layer (dcf.transport): the per-transport buffer that
decouples wildly different link rates, and frame round-trips over each medium (loopback,
UDP, audio-WAV, SDR-.cf32, file spool). See Documentation/DCF_FIELD_USE.md."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "MCP"))

try:
    import numpy  # noqa: F401  (audio/SDR transports need it)
    from dcf import transport as T
    import wirelab_core as wire
    HAVE = True
except Exception:
    HAVE = False

if HAVE:
    FRAME = wire.encode(3, 1, 0x00A1, 0xFFFF, b"SOS!", 0)

    class _Recording(T.Transport):
        def __init__(self, name, **kw):
            super().__init__(name, **kw)
            self.tx = []
        def _transmit(self, frame, dest):
            self.tx.append(frame)


@unittest.skipUnless(HAVE, "numpy required for the transport layer")
class TestBuffer(unittest.TestCase):
    """The buffer between transports — the heart of bridging mismatched bandwidths."""

    def test_bounded_drop_oldest(self):
        q = T.OutboundQueue(maxlen=8)
        for i in range(100):
            q.put(("bulk", i))
        self.assertEqual(len(q), 8)
        self.assertEqual(q.dropped, 92)
        first = q.get()
        self.assertEqual(first[1], 92)            # oldest survivors, FIFO

    def test_priority_survives_saturation(self):
        q = T.OutboundQueue(maxlen=4)
        q.put(("CTRL", 0), priority=True)
        for i in range(50):
            q.put(("bulk", i))                    # flood of bulk
        drained = [q.get() for _ in range(len(q))]
        self.assertEqual(drained[0][0], "CTRL")   # priority drained first
        self.assertIn("CTRL", [x[0] for x in drained])  # and never dropped

    def test_send_into_full_queue_sheds(self):
        # a slow link's queue caps memory under a fast burst (sender not started).
        t = _Recording("slow", queue_max=4)
        for _ in range(50):
            t.send(FRAME)
        self.assertEqual(t.backlog, 4)
        self.assertEqual(t.dropped, 46)
        self.assertEqual(t.sent, 0)               # nothing transmitted yet (no drain)


@unittest.skipUnless(HAVE, "numpy required for the transport layer")
class TestTransports(unittest.TestCase):
    def test_loopback_relay(self):
        med = T.LoopbackMedium()
        a, b = T.LoopbackTransport("a", med), T.LoopbackTransport("b", med)
        got = []
        a.start(lambda f, m: None)
        b.start(lambda f, m: got.append((f, m["transport"])))
        try:
            a.send(FRAME)
            self._wait(lambda: got)
            self.assertEqual(got[0][0], FRAME)
        finally:
            a.stop(); b.stop()

    def test_udp_roundtrip(self):
        rx = T.UdpTransport("rx", bind=("127.0.0.1", 0))
        got = []
        rx.start(lambda f, m: got.append(f))
        tx = T.UdpTransport("tx", bind=("127.0.0.1", 0))
        tx.start(lambda f, m: None)
        tx.add_peer("127.0.0.1", rx.port)
        try:
            tx.send(FRAME)
            self._wait(lambda: got)
            self.assertEqual(got[0], FRAME)
        finally:
            tx.stop(); rx.stop()

    def test_audio_dir_roundtrip(self):
        self._dir_roundtrip(T.AudioTransport, {})

    def test_sdr_dir_roundtrip(self):
        self._dir_roundtrip(T.SdrTransport, {})

    def test_file_spool_roundtrip(self):
        d = tempfile.mkdtemp()
        spool = os.path.join(d, "s.dcf")
        rx = T.FileTransport("rx", in_path=spool)
        got = []
        rx.start(lambda f, m: got.append(f))
        tx = T.FileTransport("tx", out_path=spool)
        tx.start(lambda f, m: None)
        try:
            tx.send(FRAME)
            self._wait(lambda: got, 3.0)
            self.assertEqual(got[0], FRAME)
        finally:
            tx.stop(); rx.stop()

    def test_hydra_dir_roundtrip(self):
        # HydraModem PHY via the frame_tx/frame_rx subprocess tools (optional build).
        if not T.hydramodem_available():
            self.skipTest("HydraModem frame_tx/frame_rx not built/on PATH")
        self._dir_roundtrip(T.HydraTransport, {}, timeout=20.0)

    def test_hydra_cffi_dir_roundtrip(self):
        # HydraModem PHY via the in-process ctypes binding (no subprocess).
        if not T.hydramodem_cffi_available():
            self.skipTest("libhydramodem (ctypes) not available")
        self._dir_roundtrip(T.HydraCffiTransport, {}, timeout=10.0)

    def test_hydra_music_dir_roundtrip(self):
        # The musical tone-table profiles (hydramodem/docs/MUSIC.md), both PHY paths.
        # chime is the shortest musical frame (1.78 s of audio).
        if T.hydramodem_available():
            self._dir_roundtrip(T.HydraTransport, {"profile": "chime"}, timeout=30.0)
        if T.hydramodem_cffi_available():
            self._dir_roundtrip(T.HydraCffiTransport, {"profile": "chime"}, timeout=30.0)
        if not (T.hydramodem_available() or T.hydramodem_cffi_available()):
            self.skipTest("HydraModem not built")

    def test_janus_dir_roundtrip(self):
        # STANAG-4748 via the GPL janus-c reference (optional dep). The 17-byte
        # frame rides as JANUS cargo; subprocess encode+decode is slow, so allow
        # a generous timeout.
        if not T.janus_available():
            self.skipTest("janus-c (janus-tx/janus-rx) not installed")
        self._dir_roundtrip(T.JanusTransport, {}, timeout=40.0)

    def _dir_roundtrip(self, cls, kw, timeout=3.0):
        d = tempfile.mkdtemp()
        link = os.path.join(d, "link")
        rx = cls("rx", in_dir=link, **kw)
        got = []
        rx.start(lambda f, m: got.append(f))
        tx = cls("tx", out_dir=link, **kw)
        tx.start(lambda f, m: None)
        try:
            tx.send(FRAME)
            self._wait(lambda: got, timeout)
            self.assertEqual(got[0], FRAME)
        finally:
            tx.stop(); rx.stop()

    def _wait(self, cond, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not cond():
            time.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
