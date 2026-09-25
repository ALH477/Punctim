# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF-Snake raw-L2 Ethernet transport (cat5e) — Python reference + CI test double.

Carries a batch of opaque 17-byte DeModFrames over raw Ethernet (AF_PACKET, a custom
EtherType per plane, no IP/UDP) — a transport *beneath* the wire quantum, so the 246-vector
wire certificate is untouched.  Frames are batched as 32-byte SuperPacks into one Ethernet
payload to cut the datagram count.  Byte-identical to hydramodem/dcf-tools/snake_l2.h.

Ethernet payload layout:  [n_frames u16 BE][ SuperPack * ceil(n/2) ]
  Each SuperPack packs two DeModFrames; an odd trailing frame is paired with a canonical
  zero DATA filler frame that the receiver discards using n_frames.

- ``L2EthTransport`` uses a real AF_PACKET socket (needs CAP_NET_RAW; raises on EPERM).
- ``L2LoopbackTransport`` is a privilege-free double over ``LoopbackMedium`` that exercises
  the SuperPack batching/unbatching in CI without CAP_NET_RAW.
"""
import os
import socket
import sys

for _mcp in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "MCP")):
    if os.path.isdir(_mcp):
        sys.path.insert(0, _mcp)
import wirelab_core as wire       # noqa: E402,F401  (certified DeModFrame codec)
import superpack                  # noqa: E402,F401  (certified SuperPack container)
import mediumlab_core as _medium  # noqa: E402  (certified medium codecs: l2eth family)

from .transport import Transport, LoopbackTransport

# Custom EtherTypes (IEEE local/experimental range; no clash with real AVB 0x22F0).
ETHERTYPE_RECORD = 0x88B5         # wire A: quanta record plane
ETHERTYPE_CUE = 0x88B6            # wire B: PCM cue plane

SUPER_LEN = 32
FRAME_LEN = 17
BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"

# The batch codec is the certified DCF-Medium `l2eth` family (python/MCP/mediumlab_core.py,
# Documentation/medium_vectors.json); re-exported here under the historical names.
L2_HDR = _medium.L2_HDR           # the n_frames u16
# The canonical zero filler frame (a valid DATA DeModFrame with all application fields 0);
# byte-identical to the C snake_l2.h filler, so a batch decodes the same in both languages.
_FILLER = _medium.L2_FILLER
l2_capacity = _medium.l2_capacity
batch = _medium.l2_batch          # frames -> [n u16 BE][SuperPack * ceil(n/2)]
unbatch = _medium.l2_unbatch      # payload -> frames (bit-exact); ValueError if truncated


# ── real AF_PACKET transport (needs CAP_NET_RAW) ──────────────────────────────
class L2EthTransport(Transport):
    """Raw-L2 Ethernet transport over one interface + EtherType.  Each ``send`` ships one
    frame as a 1-frame batch; ``send_batch`` ships many frames in one Ethernet payload."""

    def __init__(self, name, ifname, ethertype=ETHERTYPE_RECORD, mtu=1500,
                 dst_mac=BROADCAST_MAC, **kw):
        super().__init__(name, **kw)
        self._ifname = ifname
        self._ethertype = ethertype
        self._mtu = mtu
        self._dst = bytes(dst_mac)
        self._sock = None
        self._rx_running = False
        self._rx_thread = None
        self.invalid_datagrams = 0

    def _open(self):
        s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(self._ethertype))
        s.bind((self._ifname, self._ethertype))     # raises PermissionError without CAP_NET_RAW
        return s

    def _transmit(self, frame, dest):
        if self._sock is None:
            self._sock = self._open()
        self._sock.sendto(batch([frame]), (self._ifname, self._ethertype, 0, 0,
                                            dest or self._dst))

    def send_batch(self, frames, dest=None):
        if self._sock is None:
            self._sock = self._open()
        self._sock.sendto(batch(frames), (self._ifname, self._ethertype, 0, 0,
                                          dest or self._dst))

    def _start_recv(self):
        import threading
        self._sock = self._sock or self._open()
        self._rx_running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, name=f"rx-{self.name}",
                                           daemon=True)
        self._rx_thread.start()

    def _rx_loop(self):
        self._sock.settimeout(0.3)
        while self._rx_running:
            try:
                buf, _ = self._sock.recvfrom(self._mtu)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                frames = unbatch(buf)
            except ValueError:
                self.invalid_datagrams += 1
                continue
            for f in frames:
                self._deliver(f, {"via": "l2eth"})

    def _stop_recv(self):
        self._rx_running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()
            self._sock = None


# ── privilege-free double (CI) ────────────────────────────────────────────────
class L2LoopbackTransport(LoopbackTransport):
    """A LoopbackTransport that round-trips every frame through the SuperPack batch codec,
    so CI exercises batch/unbatch without CAP_NET_RAW.  Models the raw-L2 wire in-process."""

    def _transmit(self, frame, dest):
        # batch a single frame, then unbatch on the way onto the shared medium — proves the
        # SuperPack container is transparent to the frame bytes.
        (recovered,) = unbatch(batch([bytes(frame)]))
        self._medium.broadcast(self, recovered)

    def send_batch(self, frames, dest=None):
        """Ship many frames as ONE modelled Ethernet payload (batch -> unbatch -> wire)."""
        for f in unbatch(batch([bytes(x) for x in frames])):
            self._medium.broadcast(self, f)
