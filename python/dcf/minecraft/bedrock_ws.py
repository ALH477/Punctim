# SPDX-License-Identifier: LGPL-3.0-only
"""A bridge for vanilla Bedrock's built-in `/connect ws://host:port` (a.k.a. `/wsserver`).

The Bedrock CLIENT opens a WebSocket to us; we subscribe to `PlayerMessage` events and push
`commandRequest`s back — the client's own protocol crossing the host boundary, no mod and no
server needed.  A command block running `say DCF <34 hex>` reaches us as a PlayerMessage of
type "say"; a `DCF TX w0..w5` line (a Bedrock behaviour-pack port of the datapack) works too.
Inbound frames become `scoreboard players set w0..w5` + `function <ns>/rx_commit` commands run
as the connected player (cheats / operator required).

Stdlib RFC 6455 server: HTTP/1.1 Upgrade handshake, masked client frames, text (0x1),
ping/pong (0x9/0xA), close (0x8).  No TLS, no extensions ("Require Encrypted Websockets" must
be off on the client).  Wire shapes seen in the wild are tolerated in both the
`body.properties.{Message,Sender,MessageType}` and the flat `body.{message,sender,type}` form.
Not byte-certified: the frames it carries are; the JSON envelope is the client's."""
import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import uuid

from .. import transport as T

_MCP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "MCP")
if _MCP not in sys.path:
    sys.path.insert(0, _MCP)
import mclab_core as M  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HEX34 = re.compile(r"\bDCF ([0-9a-fA-F]{34})\b")
EVENTS_DEFAULT = ("PlayerMessage",)
EVENTS_EXTRA = ("BlockPlaced", "BlockBroken")


def _msg(purpose, body, message_type="commandRequest"):
    return json.dumps({"header": {"version": 1, "requestId": str(uuid.uuid4()),
                                  "messageType": message_type, "messagePurpose": purpose},
                       "body": body}, separators=(",", ":"))


def subscribe(event_name):
    return _msg("subscribe", {"eventName": event_name})


def command_request(command_line):
    return _msg("commandRequest", {"version": 1, "commandLine": command_line,
                                   "origin": {"type": "player"}})


def register_commands(frame, ns="dcf"):
    """The Bedrock commands that write `frame` into the world's register."""
    cmds = [f"scoreboard players set w{k} {M.OBJ_REG} {w}" for k, w in enumerate(M.frame_to_words(frame))]
    cmds.append(f"function {ns}/rx_commit")
    return cmds


def parse_event(text):
    """WS text -> (kind, payload): ("frame", bytes) for a DCF line, ("response", dict) for a
    commandResponse, ("event", dict) otherwise, or None when it is not JSON."""
    try:
        d = json.loads(text)
    except ValueError:
        return None
    header = d.get("header", {}) or {}
    body = d.get("body", {}) or {}
    purpose = header.get("messagePurpose")
    if purpose == "commandResponse":
        return "response", body
    if purpose != "event":
        return "event", d
    props = body.get("properties", body)
    message = props.get("Message", props.get("message", ""))
    if isinstance(message, str):
        m = HEX34.search(message)
        if m:
            return "frame", bytes.fromhex(m.group(1))
        fr = M.parse_chat_tx(message)
        if fr is not None:
            return "frame", fr
    return "event", d


# ── RFC 6455 framing ──────────────────────────────────────────────────────────
def ws_encode(opcode, payload):
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf


def ws_recv(sock):
    """One frame -> (opcode, payload). Client frames must be masked (RFC 6455 §5.1)."""
    b0, b1 = _recvn(sock, 2)
    opcode, masked, n = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack(">H", _recvn(sock, 2))
    elif n == 127:
        (n,) = struct.unpack(">Q", _recvn(sock, 8))
    key = _recvn(sock, 4) if masked else b""
    data = _recvn(sock, n)
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return opcode, data


def ws_handshake(sock):
    """Read the HTTP Upgrade request, answer 101. Returns the request path."""
    req = b""
    while b"\r\n\r\n" not in req:
        c = sock.recv(4096)
        if not c:
            raise ConnectionError("closed during handshake")
        req += c
    head, _, _ = req.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    path = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "/"
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
    key = headers.get("sec-websocket-key")
    if not key or "websocket" not in headers.get("upgrade", "").lower():
        sock.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        raise ConnectionError("not a websocket upgrade")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
    return path


# ── the transport ────────────────────────────────────────────────────────────
class BedrockWsTransport(T.Transport):
    def __init__(self, name, bind=("0.0.0.0", 19134), namespace="dcf", events=False, log=None):
        super().__init__(name)
        self.bind, self.ns = bind, namespace
        self.events = EVENTS_DEFAULT + (EVENTS_EXTRA if events else ())
        self.log = log or (lambda s: sys.stderr.write(f"[bedrock] {s}\n"))
        self._clients = {}                   # sock -> lock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = None
        self.invalid_datagrams = 0
        self.responses = []                  # last commandResponse bodies (bounded)

    @property
    def port(self):
        return self._srv.getsockname()[1] if self._srv else None

    @property
    def clients(self):
        with self._lock:
            return len(self._clients)

    # ── lifecycle ────────────────────────────────────────────────────────
    def _start_recv(self):
        self._stop.clear()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(self.bind)
        s.listen(4)
        s.settimeout(0.2)
        self._srv = s
        threading.Thread(target=self._accept_loop, name="bedrock-ws", daemon=True).start()

    def _stop_recv(self):
        self._stop.set()
        with self._lock:
            socks = list(self._clients)
        for c in socks:
            try:
                c.sendall(ws_encode(0x8, b""))
                c.close()
            except OSError:
                pass
        if self._srv:
            self._srv.close()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._client, args=(conn, addr), daemon=True).start()

    def _client(self, sock, addr):
        try:
            sock.settimeout(None)
            ws_handshake(sock)
            lock = threading.Lock()
            with self._lock:
                self._clients[sock] = lock
            self.log(f"client {addr[0]}:{addr[1]} connected; subscribing {', '.join(self.events)}")
            for ev in self.events:
                self._send(sock, subscribe(ev))
            while not self._stop.is_set():
                opcode, data = ws_recv(sock)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send_raw(sock, ws_encode(0xA, data))
                    continue
                if opcode != 0x1:
                    continue
                parsed = parse_event(data.decode("utf-8", "replace"))
                if parsed is None:
                    self.invalid_datagrams += 1
                    continue
                kind, payload = parsed
                if kind == "frame":
                    self._deliver(payload, {"peer": f"{addr[0]}:{addr[1]}", "egress": "bedrock-ws"})
                elif kind == "response":
                    self.responses.append(payload)
                    del self.responses[:-64]
                    if payload.get("statusCode", 0) != 0:
                        self.log(f"command failed: {payload.get('statusMessage', payload)}")
        except (OSError, ConnectionError) as e:
            self.log(f"client {addr[0]}:{addr[1]} gone: {e}")
        finally:
            with self._lock:
                self._clients.pop(sock, None)
            try:
                sock.close()
            except OSError:
                pass

    def _send_raw(self, sock, data):
        lock = self._clients.get(sock)
        if lock is None:
            return
        with lock:
            sock.sendall(data)

    def _send(self, sock, text):
        self._send_raw(sock, ws_encode(0x1, text.encode("utf-8")))

    # ── ingress: peer -> every connected Bedrock world ─────────────────────
    def _transmit(self, frame, dest):
        with self._lock:
            socks = list(self._clients)
        for sock in socks:
            for cmd in register_commands(frame, self.ns):
                try:
                    self._send(sock, command_request(cmd))
                except OSError:
                    pass
