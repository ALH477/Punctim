# SPDX-License-Identifier: LGPL-3.0-only
"""Source RCON client (the protocol Minecraft's `enable-rcon` speaks), stdlib only.

Packet: [len i32 LE][id i32 LE][type i32 LE][body ASCII/UTF-8][\\0][\\0]; len counts
everything after itself.  type 3 = auth (reply type 2, id -1 on failure), 2 = exec command
(reply type 0, possibly split over several packets — a trailing empty exec with a sentinel id
marks the end)."""
import socket
import struct
import threading
import time

AUTH, EXEC, RESPONSE = 3, 2, 0


class RconError(RuntimeError):
    pass


class RconClient:
    def __init__(self, host, port, password, timeout=5.0):
        self.host, self.port, self.password, self.timeout = host, int(port), password, timeout
        self._sock = None
        self._id = 0
        self._lock = threading.Lock()

    # ── wire ───────────────────────────────────────────────────────────────
    def _send(self, ptype, body):
        self._id = (self._id % 0x7FFFFFF0) + 1
        data = body.encode("utf-8") + b"\x00\x00"
        pkt = struct.pack("<iii", 8 + len(data), self._id, ptype) + data
        self._sock.sendall(pkt)
        return self._id

    def _recv(self):
        head = self._recvn(4)
        (length,) = struct.unpack("<i", head)
        body = self._recvn(length)
        pid, ptype = struct.unpack("<ii", body[:8])
        return pid, ptype, body[8:-2].decode("utf-8", "replace")

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    # ── session ─────────────────────────────────────────────────────────────
    def connect(self):
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock = s
        pid = self._send(AUTH, self.password)
        rid, rtype, _ = self._recv()
        if rtype == RESPONSE:              # some servers send an empty RESPONSE first
            rid, rtype, _ = self._recv()
        if rid == -1 or rid != pid:
            self.close()
            raise RconError("authentication refused")

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def connected(self):
        return self._sock is not None

    def exec(self, command, retries=2):
        """Run one command; returns the server's reply text (may be empty)."""
        with self._lock:
            last = None
            for attempt in range(retries + 1):
                try:
                    if self._sock is None:
                        self.connect()
                    pid = self._send(EXEC, command)
                    end = self._send(EXEC, "")        # sentinel: reply ordering marks the end
                    out = []
                    while True:
                        rid, rtype, text = self._recv()
                        if rid == end:
                            break
                        if rid == pid:
                            out.append(text)
                    return "".join(out)
                except (OSError, RconError) as e:
                    last = e
                    self.close()
                    time.sleep(0.2 * (attempt + 1))
            raise RconError(f"rcon {self.host}:{self.port}: {last}")
