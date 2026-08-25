# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF-QKD key-ID beacon runtime: announce and receive ETSI 014 key_IDs over any DCF
transport (loopback, UDP, HydraModem, JANUS, ...).

The L2 framing itself lives in python/MCP/qkdlab_core.py — the canonical, byte-certified
reference shared with C and Rust.  This module only moves those frames across a
`dcf.transport.Transport` and tracks the epoch counter.

The beacon carries ONLY the key_ID.  Key material never touches a DeModFrame.
See Documentation/DCF_QKD_SPEC.md.
"""
import os
import sys
import threading
import time

for _mcp in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "MCP"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP")):
    if os.path.isdir(_mcp):
        sys.path.insert(0, _mcp)
from qkdlab_core import (  # noqa: E402
    packetize, KeyIdReassembler, channel_id, key_id_bytes,
    FRAGS, MAX_EPOCH, BROADCAST,
)

__all__ = ["KeyIdBeacon", "channel_id", "key_id_bytes", "FRAGS", "MAX_EPOCH", "BROADCAST"]


def _now_ts24():
    """24-bit microsecond timestamp, as the rest of the tree stamps frames."""
    return int(time.monotonic() * 1_000_000) & 0xFFFFFF


class KeyIdBeacon:
    """Send and receive key-ID beacons on one rendezvous channel.

    `on_key_id(key_id, meta)` is invoked for every fully reassembled key_ID, where meta
    carries {"epoch", "ts_us", "src", "dst"}.  Receiving is optional: a master SAE that
    only announces need never call start().
    """

    def __init__(self, transport, node_id, channel=BROADCAST, on_key_id=None,
                 max_pending=64):
        self.transport = transport
        self.node_id = int(node_id)
        self.channel = int(channel)
        self.on_key_id = on_key_id
        self._reasm = KeyIdReassembler(accept_dst=self.channel, max_pending=max_pending)
        self._lock = threading.Lock()
        self._epoch = 0
        self.announced = 0
        self.resolved = 0
        self.lost = 0

    # ── master side ──────────────────────────────────────────────────────────
    def next_epoch(self):
        """Allocate the next beacon epoch, wrapping at the 14-bit rail."""
        with self._lock:
            e = self._epoch
            self._epoch = (self._epoch + 1) % (MAX_EPOCH + 1)
            return e

    def announce(self, key_id, epoch=None, ts_us=None):
        """Beacon one key_ID as FRAGS ordinary CTRL frames.  Returns the epoch used."""
        epoch = self.next_epoch() if epoch is None else int(epoch)
        ts_us = _now_ts24() if ts_us is None else int(ts_us)
        frames = packetize(key_id, epoch, ts_us, self.node_id, self.channel)
        assert len(frames) == FRAGS
        for f in frames:
            self.transport.send(f)
        self.announced += 1
        return epoch

    # ── slave side ───────────────────────────────────────────────────────────
    def start(self):
        """Begin receiving beacons from the transport.  Returns self."""
        self.transport.start(self._on_frame)
        return self

    def stop(self):
        self.transport.stop()

    def _on_frame(self, frame, meta=None):
        for ev in self._reasm.push(frame):
            if ev[0] == "key_id":
                _, epoch, ts_us, src, dst, key_id = ev
                self.resolved += 1
                if self.on_key_id:
                    self.on_key_id(key_id, {"epoch": epoch, "ts_us": ts_us,
                                            "src": src, "dst": dst})
            else:
                self.lost += 1

    def finalize(self):
        """Drain incomplete beacons, counting them lost.  Returns the lost events."""
        events = self._reasm.finalize()
        self.lost += len(events)
        return events

    @property
    def pending(self):
        return self._reasm.pending()
