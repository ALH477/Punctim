# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF-QKD bridge: the ETSI GS QKD 014 flow end to end over a DCF transport.

Runs against the mock KME pair — stdlib only, no network, no photonic hardware.
The mock's keys come from os.urandom: nothing here is quantum, and the point of
these tests is the *classical control plane*.

The load-bearing test is TestExportInvariant: the wire carries key_IDs, never key
material.  That is the rule the export posture in Documentation/DCF_QKD_SPEC.md
rests on, so it is enforced here rather than merely asserted in prose.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "MCP"))

from dcf.qkd.beacon import KeyIdBeacon, channel_id  # noqa: E402
from dcf.qkd.etsi014 import Etsi014Client, KmeError  # noqa: E402
from dcf.qkd.mock_kme import KeyStore, serve  # noqa: E402
from dcf.qkd.sae import MasterSae, SlaveSae  # noqa: E402
from dcf.transport import LoopbackMedium, LoopbackTransport  # noqa: E402
from qkdlab_core import FRAGS  # noqa: E402

MASTER_SAE = "SAE-MASTER-01"
SLAVE_SAE = "SAE-SLAVE-01"
CHANNEL = channel_id("qkd-test")
TIMEOUT = 5.0


def _wait(pred, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class _Bridge:
    """A master SAE, a slave SAE, two mock KMEs on one keystore, and a passive tap."""

    def __init__(self):
        self.store = KeyStore()
        # Ephemeral ports: these tests must not collide with each other or with a
        # developer's running mock KME.
        self.servers = [serve("KME-A", "KME-B", 0, self.store, quiet=True),
                        serve("KME-B", "KME-A", 0, self.store, quiet=True)]
        port_a, port_b = (s.server_address[1] for s in self.servers)
        mc = Etsi014Client(f"http://127.0.0.1:{port_a}", MASTER_SAE,
                           extra_headers={"X-SAE-ID": MASTER_SAE})
        self.sc = Etsi014Client(f"http://127.0.0.1:{port_b}", SLAVE_SAE,
                                extra_headers={"X-SAE-ID": SLAVE_SAE})
        self.medium = LoopbackMedium()
        self.t_master = LoopbackTransport("master", self.medium)
        self.t_slave = LoopbackTransport("slave", self.medium)
        self.t_tap = LoopbackTransport("tap", self.medium)
        self.wire = []
        self.t_tap.start(lambda frame, meta=None: self.wire.append(bytes(frame)))
        self.master = MasterSae(mc, SLAVE_SAE, self.t_master, 0x0001, CHANNEL)
        self.slave = SlaveSae(self.sc, MASTER_SAE, self.t_slave, 0x0002, CHANNEL)
        self.master.beacon.start()
        self.slave.start()

    def close(self):
        self.slave.stop()
        self.master.beacon.stop()
        self.t_tap.stop()
        for s in self.servers:
            s.shutdown()
            s.server_close()          # release the listening socket, not just the loop


class _BridgeCase(unittest.TestCase):
    def setUp(self):
        self.b = _Bridge()
        self.addCleanup(self.b.close)


class TestEndToEnd(_BridgeCase):
    def test_key_material_agrees_byte_for_byte(self):
        """The whole point: both SAEs end up holding the same key, via a key_ID beacon."""
        keys = self.b.master.request_keys(number=3, size=256)
        self.assertEqual(len(keys), 3)
        for k in keys:
            self.b.master.announce(k.key_ID)
        self.assertTrue(_wait(lambda: len(self.b.slave.keys) == 3),
                        f"slave resolved {len(self.b.slave.keys)}/3; errors={self.b.slave.errors}")
        self.assertEqual(self.b.slave.errors, [])
        for kid, kb in self.b.master.keys.items():
            self.assertEqual(self.b.slave.keys.get(kid), kb, f"key {kid} differs")

    def test_beacon_is_exactly_four_frames_per_key_id(self):
        k = self.b.master.request_keys(number=1)[0]
        before = len(self.b.wire)
        self.b.master.announce(k.key_ID)
        self.assertTrue(_wait(lambda: len(self.b.wire) - before >= FRAGS))
        time.sleep(0.05)
        self.assertEqual(len(self.b.wire) - before, FRAGS)

    def test_no_dangling_fragments(self):
        for k in self.b.master.request_keys(number=2):
            self.b.master.announce(k.key_ID)
        self.assertTrue(_wait(lambda: len(self.b.slave.keys) == 2))
        self.assertEqual(self.b.slave.beacon.pending, 0)
        self.assertEqual(self.b.slave.beacon.finalize(), [])


class TestNegative(_BridgeCase):
    def test_replayed_key_id_is_refused(self):
        """ETSI 014 single-delivery: a key is consumed once the slave fetches it."""
        k = self.b.master.request_keys(number=1)[0]
        self.b.master.announce(k.key_ID)
        self.assertTrue(_wait(lambda: k.key_ID in self.b.slave.keys))
        with self.assertRaises(KmeError) as cm:
            self.b.sc.get_key_with_ids(MASTER_SAE, [k.key_ID])
        self.assertIn("401", str(cm.exception))

    def test_unknown_key_id_is_refused(self):
        with self.assertRaises(KmeError) as cm:
            self.b.sc.get_key_with_ids(MASTER_SAE,
                                       ["00000000-0000-0000-0000-000000000000"])
        self.assertIn("404", str(cm.exception))

    def test_corrupt_beacon_frame_never_resolves_a_key(self):
        """A flipped bit must fail CRC and be dropped — not resolve the wrong key_ID."""
        from qkdlab_core import packetize
        k = self.b.master.request_keys(number=1)[0]
        frames = packetize(k.key_ID, 100, 0, 0x0001, CHANNEL)
        corrupt = bytearray(frames[1])
        corrupt[9] ^= 0x01
        for f in [frames[0], bytes(corrupt), frames[2], frames[3]]:
            self.b.t_master.send(f)
        time.sleep(0.2)
        self.assertNotIn(k.key_ID, self.b.slave.keys)


class TestExportInvariant(_BridgeCase):
    """The normative rule: the wire carries key_IDs, never key material.

    This is what keeps DCF's plaintext, encryption-free wire posture intact while
    still interoperating with real QKD hardware.  See the export section of
    Documentation/DCF_QKD_SPEC.md.
    """

    def test_no_key_material_ever_reaches_the_wire(self):
        keys = self.b.master.request_keys(number=4, size=256)
        for k in keys:
            self.b.master.announce(k.key_ID)
        self.assertTrue(_wait(lambda: len(self.b.slave.keys) == 4))
        blob = b"".join(self.b.wire)
        self.assertTrue(blob, "the tap captured nothing — the test would vacuously pass")
        for k in keys:
            self.assertNotIn(k.key, blob, f"key material for {k.key_ID} leaked onto the wire")
            # not even a 4-byte window of it (one DeModFrame payload's worth)
            for off in range(0, len(k.key) - 3):
                self.assertNotIn(k.key[off:off + 4], blob,
                                 f"a 4-byte window of key {k.key_ID} leaked onto the wire")

    def test_the_wire_carries_the_key_id_and_nothing_else(self):
        """Every captured payload byte belongs to some announced key_ID."""
        import uuid
        keys = self.b.master.request_keys(number=2)
        for k in keys:
            self.b.master.announce(k.key_ID)
        self.assertTrue(_wait(lambda: len(self.b.slave.keys) == 2))
        allowed = b"".join(uuid.UUID(k.key_ID).bytes for k in keys)
        for f in self.b.wire:
            self.assertEqual(len(f), 17)
            self.assertEqual(f[0], 0xD3)              # a real DeModFrame
            self.assertEqual(f[1] & 0x0F, 3)          # CTRL(3)
            self.assertIn(f[8:12], allowed, "a payload byte-group is not from a key_ID")


class TestBeaconStandalone(unittest.TestCase):
    """The beacon works with no KME at all — it is just a key_ID carrier."""

    def test_beacon_round_trips_over_loopback(self):
        medium = LoopbackMedium()
        got = []
        tx = KeyIdBeacon(LoopbackTransport("tx", medium), 0x00A1, CHANNEL)
        rx = KeyIdBeacon(LoopbackTransport("rx", medium), 0x00A2, CHANNEL,
                         on_key_id=lambda kid, meta: got.append((kid, meta)))
        tx.start()
        rx.start()
        self.addCleanup(tx.stop)
        self.addCleanup(rx.stop)
        kid = "574bace1-4c27-49a1-babd-663fdb624d00"
        epoch = tx.announce(kid)
        self.assertTrue(_wait(lambda: got))
        self.assertEqual(got[0][0], kid)
        self.assertEqual(got[0][1]["epoch"], epoch)
        self.assertEqual(got[0][1]["src"], 0x00A1)

    def test_epochs_advance_and_wrap_at_the_rail(self):
        from qkdlab_core import MAX_EPOCH
        medium = LoopbackMedium()
        b = KeyIdBeacon(LoopbackTransport("tx", medium), 1, CHANNEL)
        self.assertEqual([b.next_epoch() for _ in range(3)], [0, 1, 2])
        b._epoch = MAX_EPOCH
        self.assertEqual(b.next_epoch(), MAX_EPOCH)
        self.assertEqual(b.next_epoch(), 0)


if __name__ == "__main__":
    unittest.main()
