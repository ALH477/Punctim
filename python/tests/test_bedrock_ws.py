# SPDX-License-Identifier: LGPL-3.0-only
"""The Bedrock /connect bridge against a stdlib fake Bedrock client (RFC 6455 client side)."""
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest

PYDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PYDIR)
sys.path.insert(0, os.path.join(PYDIR, "MCP"))

import mclab_core as M  # noqa: E402
from dcf.minecraft import bedrock_ws as B  # noqa: E402

GOLDEN = M.GOLDEN


class FakeBedrockClient:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                        ).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(4096)
        head, _, self.buf = resp.partition(b"\r\n\r\n")   # bytes after the 101 are WS frames
        assert head.startswith(b"HTTP/1.1 101"), head
        assert B.base64.b64encode(B.hashlib.sha1((key + B.GUID).encode()).digest()).decode().encode() in head

    def _recvn(self, n):
        while len(self.buf) < n:
            c = self.s.recv(n - len(self.buf))
            if not c:
                raise ConnectionError("closed")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text):          # client frames are masked
        data = text.encode()
        key = os.urandom(4)
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.s.sendall(head + key + masked)

    def recv(self):                     # server->client frames are unmasked
        b0, b1 = self._recvn(2)
        opcode, n = b0 & 0x0F, b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack(">H", self._recvn(2))
        elif n == 127:
            (n,) = struct.unpack(">Q", self._recvn(8))
        return opcode, self._recvn(n).decode()

    def recv_json(self):
        opcode, text = self.recv()
        assert opcode == 0x1, opcode
        return json.loads(text)

    def event(self, message, mtype="say", sender="CommandBlock"):
        self.send_text(json.dumps({"header": {"messagePurpose": "event", "eventName": "PlayerMessage"},
                                   "body": {"eventName": "PlayerMessage",
                                            "properties": {"Message": message, "Sender": sender,
                                                           "MessageType": mtype}}}))

    def close(self):
        self.s.close()


class TestBedrockWs(unittest.TestCase):
    def setUp(self):
        self.got = []
        self.t = B.BedrockWsTransport("bedrock", ("127.0.0.1", 0), log=lambda s: None)
        self.t.start(lambda f, m: self.got.append((f, m)))
        self.c = FakeBedrockClient(self.t.port)
        self.subs = [self.c.recv_json() for _ in range(1)]

    def tearDown(self):
        self.c.close()
        self.t.stop()

    def test_subscribes_player_message(self):
        s = self.subs[0]
        self.assertEqual(s["header"]["messagePurpose"], "subscribe")
        self.assertEqual(s["body"]["eventName"], "PlayerMessage")
        self.assertEqual(s["header"]["version"], 1)

    def test_say_line_becomes_frame(self):
        self.c.event(f"DCF {GOLDEN.hex()}")
        self.c.event("just chatting")                       # ignored
        self.c.event("DCF TX " + " ".join(map(str, M.frame_to_words(GOLDEN))), mtype="chat")
        deadline = time.time() + 3
        while len(self.got) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([f for f, _ in self.got], [GOLDEN, GOLDEN])
        self.assertEqual(self.got[0][1]["egress"], "bedrock-ws")

    def test_flat_event_shape_and_uppercase_hex(self):
        self.c.send_text(json.dumps({"header": {"messagePurpose": "event"},
                                     "body": {"message": "DCF " + GOLDEN.hex().upper(), "type": "say"}}))
        deadline = time.time() + 3
        while not self.got and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.got[0][0], GOLDEN)

    def test_inbound_frame_becomes_commands(self):
        deadline = time.time() + 3
        while self.t.clients < 1 and time.time() < deadline:
            time.sleep(0.02)
        self.t.send_now(GOLDEN)
        cmds = [self.c.recv_json() for _ in range(7)]
        self.assertTrue(all(c["header"]["messagePurpose"] == "commandRequest" for c in cmds))
        lines = [c["body"]["commandLine"] for c in cmds]
        self.assertEqual(lines, B.register_commands(GOLDEN))
        self.assertEqual(lines[0], f"scoreboard players set w0 dcf_reg {M.frame_to_words(GOLDEN)[0]}")
        self.assertEqual(lines[-1], "function dcf/rx_commit")
        # the client answers each with a commandResponse; a failure is recorded, not fatal
        self.c.send_text(json.dumps({"header": {"messagePurpose": "commandResponse", "requestId": cmds[0]["header"]["requestId"]},
                                     "body": {"statusCode": -2147483648, "statusMessage": "Unknown command"}}))
        time.sleep(0.2)
        self.assertEqual(self.t.responses[-1]["statusCode"], -2147483648)

    def test_ping_pong_and_garbage(self):
        self.c.s.sendall(bytes([0x89, 0x80]) + b"\x00\x00\x00\x00")      # masked empty ping
        opcode, data = self.c.recv()
        self.assertEqual(opcode, 0xA)
        self.c.send_text("not json")
        time.sleep(0.1)
        self.assertEqual(self.t.invalid_datagrams, 1)


if __name__ == "__main__":
    unittest.main()
