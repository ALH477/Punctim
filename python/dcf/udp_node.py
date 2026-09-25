# SPDX-License-Identifier: LGPL-3.0-only
"""Stdlib UDP DcfNode — the interoperable binary-ProtoMessage node for Python.

This is the Python counterpart of the Go/C/Rust nodes (NOT the gRPC ``dcf.client``):
it speaks the binary :class:`~dcf.proto.ProtoMessage` envelope over UDP, answers
PING with PONG, tracks per-peer RTT (EMA), and dispatches the DCF-Mesh REPORT/ROLE
control adapter into the attached :class:`~dcf.mesh_runtime.MeshRuntime`. So a Python
node meshes with the Go/C/Rust nodes.

Threading: one receive thread + (if a mesh runtime is attached) one tick thread.
Shared peer state is guarded by a lock.
"""

import logging
import socket
import threading

from .proto import (
    ProtoMessage,
    MSG_PING,
    MSG_PONG,
    MSG_MESH,
    MSG_AUDIO,
    MSG_FRAME,
)

FRAME_LEN = 17

log = logging.getLogger("dcf.udp")

# EMA smoothing factor for RTT — DEFAULT_RTT_ALPHA in the Rust SDK (rust/src/lib.rs).
RTT_ALPHA = 0.125


class DcfNode:
    """A UDP node bound to ``host:port`` that meshes with the other-language nodes."""

    def __init__(self, host="0.0.0.0", port=7777, node_id="0"):
        self.host = host
        self.port = port
        self.node_id = str(node_id)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.settimeout(0.5)

        self._lock = threading.Lock()
        self._peers = []                       # ordered peer ids (numeric strings)
        self._peer_map = {}                    # peer_id -> (host, port)
        self._peer_rtt = {}                    # peer_id -> smoothed RTT ms (float)
        self._seq = 0
        self._running = False
        self._rx_thread = None
        self.mesh = None                       # optional MeshRuntime
        # Optional MSG_AUDIO tee: on_audio(payload_17b, addr). Used by the radio
        # (dcf_node start --radio) to stream the audio this node receives.
        self.on_audio = None
        # Optional MSG_FRAME (12) sink: on_frame(frame_17b, addr) — a bare DeModFrame sent
        # by `punctim io --out udp:` / dcf-bridge (the DCF-Medium udp "proto" dialect).
        # Without a sink the frame is logged.
        self.on_frame = None

    # ── peer management ──────────────────────────────────────────────────────
    def add_peer(self, peer_id, host, port):
        peer_id = str(peer_id)
        with self._lock:
            if peer_id not in self._peer_map:
                self._peers.append(peer_id)
            self._peer_map[peer_id] = (host, int(port))
        log.info("Peer added: %s at %s:%d", peer_id, host, port)

    def list_peers(self):
        with self._lock:
            return list(self._peers)

    def peer_info(self, peer_id):
        with self._lock:
            return self._peer_map.get(str(peer_id))

    def peer_rtt_ms(self, peer_id):
        """Smoothed per-peer RTT in whole ms (0 if unknown)."""
        with self._lock:
            v = self._peer_rtt.get(str(peer_id), 0.0)
        return int(v + 0.5) if v > 0 else 0

    def peer_id_by_addr(self, addr):
        """Resolve a peer id from a source ``(ip, port)`` (match by port, then ip)."""
        ip, port = addr
        with self._lock:
            by_both = None
            by_port = None
            for pid, (h, p) in self._peer_map.items():
                if p == port:
                    if by_port is None:
                        by_port = pid
                    if h == ip or h in ("localhost", "0.0.0.0"):
                        by_both = pid
                        break
            return by_both or by_port

    def _record_rtt(self, peer_id, rtt_ms):
        with self._lock:
            prev = self._peer_rtt.get(peer_id, 0.0)
            self._peer_rtt[peer_id] = RTT_ALPHA * rtt_ms + (1.0 - RTT_ALPHA) * prev

    # ── send ─────────────────────────────────────────────────────────────────
    def _next_seq(self):
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            return self._seq

    def send_message(self, msg, host, port):
        self._sock.sendto(msg.serialize(), (host, int(port)))

    def send_to(self, msg_type, payload, host, port):
        self.send_message(ProtoMessage(msg_type, self._next_seq(), payload), host, port)

    def send_ping(self, peer_id):
        info = self.peer_info(peer_id)
        if info:
            self.send_to(MSG_PING, b"", info[0], info[1])

    def send_mesh(self, payload, host, port):
        self.send_to(MSG_MESH, payload, host, port)

    # ── lifecycle ──────────────────────────────────────────────────────────--
    def is_running(self):
        return self._running

    def start(self):
        if self._running:
            return
        self._running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, name="dcf-rx", daemon=True)
        self._rx_thread.start()
        if self.mesh is not None:
            self.mesh.start(self)

    def stop(self):
        self._running = False
        if self.mesh is not None:
            self.mesh.stop()
        if self._rx_thread:
            self._rx_thread.join(timeout=2)
        try:
            self._sock.close()
        except OSError:
            pass

    # ── receive + dispatch ─────────────────────────────────────────────────--
    def _rx_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            try:
                msg = ProtoMessage.deserialize(data)
            except ValueError as e:
                log.warning("drop undecodable datagram from %s: %s", addr, e)
                continue
            self._dispatch(msg, addr)

    def _dispatch(self, msg, addr):
        if msg.msg_type == MSG_PING:
            # Echo the originator's timestamp so it can compute RTT.
            pong = ProtoMessage(MSG_PONG, msg.sequence, b"", timestamp=msg.timestamp)
            self._sock.sendto(pong.serialize(), addr)
        elif msg.msg_type == MSG_PONG:
            from .proto import now_micros
            rtt = max(0.0, (now_micros() - msg.timestamp) / 1000.0)
            pid = self.peer_id_by_addr(addr)
            if pid:
                self._record_rtt(pid, rtt)
                if self.mesh is not None:
                    self.mesh.mark_answered(pid)
        elif msg.msg_type == MSG_MESH:
            if self.mesh is not None:
                self.mesh.handle_control(msg.payload, addr)
        elif msg.msg_type == MSG_AUDIO:
            if self.on_audio is not None:
                self.on_audio(msg.payload, addr)
        elif msg.msg_type == MSG_FRAME:
            if len(msg.payload) != FRAME_LEN:
                log.warning("drop MSG_FRAME with %d-byte payload from %s", len(msg.payload), addr)
            elif self.on_frame is not None:
                self.on_frame(msg.payload, addr)
            else:
                log.info("frame %s from %s:%d", msg.payload.hex(), addr[0], addr[1])
        else:
            log.debug("unhandled msg_type %d from %s", msg.msg_type, addr)
