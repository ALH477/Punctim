# SPDX-License-Identifier: LGPL-3.0-only
"""DCF-Minecraft sidecar against a FakeServer that models the datapack register (scoreboard +
barrels + a comparator loopback) behind a real RCON listener, a console FIFO and a log file."""
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

PYDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PYDIR)
sys.path.insert(0, os.path.join(PYDIR, "MCP"))

import mclab_core as M  # noqa: E402
import wirelab_core as wire  # noqa: E402
from dcf import medium  # noqa: E402
from dcf.minecraft.logtail import LogTail  # noqa: E402
from dcf.minecraft.rcon import RconClient, RconError  # noqa: E402

GOLDEN = M.GOLDEN
PASSWORD = "hunter2"


class FakeServer:
    """Scoreboard + 34 barrels + a comparator loopback (barrel -> IN lane power); commands
    arrive over RCON or a FIFO; every reply is also appended to `latest.log` the way a real
    server logs console feedback; tellraw lines land in the log as chat."""

    def __init__(self, tmp, ns="dcf"):
        self.ns, self.tmp = ns, tmp
        self.scores = {(h, M.OBJ_CTL): 0 for h in M.CTL_HOLDERS}   # what dcf:load does
        self.barrels = [0] * M.NIBBLES
        self.log = os.path.join(tmp, "latest.log")
        open(self.log, "w").close()
        self.fifo = os.path.join(tmp, "console.fifo")
        os.mkfifo(self.fifo)
        self.lock = threading.Lock()
        self.commands = []
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self.threads = [threading.Thread(target=self._rcon_loop, daemon=True),
                        threading.Thread(target=self._fifo_loop, daemon=True)]
        for t in self.threads:
            t.start()

    def close(self):
        self._stop.set()
        self.sock.close()

    # ── the world ──────────────────────────────────────────────────────────
    def _log(self, text, chat=False):
        with open(self.log, "a") as fh:
            if chat:
                fh.write(f"[12:00:00] [Render thread/INFO]: [System] [CHAT] {text}\n")
            else:
                fh.write(f"[12:00:00] [Server thread/INFO]: {text}\n")

    def strobe(self):
        """A redstone pulse on STROBE_IN: comparators read the barrels, IN lanes latch."""
        with self.lock:
            nibbles = [M.items_to_signal(i) for i in self.barrels]
            frame = M.nibbles_to_frame(nibbles)
            for k, w in enumerate(M.frame_to_words(frame)):
                self.scores[(f"w{k}", M.OBJ_REG)] = w
            self.scores[("tx_pending", M.OBJ_CTL)] = 1
            self.scores[("tx_seq", M.OBJ_CTL)] = self.scores.get(("tx_seq", M.OBJ_CTL), 0) + 1
            self._log("DCF TX " + " ".join(str(self.scores[(f"w{k}", M.OBJ_REG)]) for k in range(6)), chat=True)

    def execute(self, cmd):
        with self.lock:
            self.commands.append(cmd)
            t = cmd.split()
            if not t:
                return ""
            if t[:2] == ["scoreboard", "players"]:
                if t[2] == "get":
                    v = self.scores.get((t[3], t[4]))
                    if v is None:
                        reply = f"Can't get value of {t[4]} for {t[3]}; none is set"
                    else:
                        reply = f"{t[3]} has {v} [{t[4]}]"
                elif t[2] == "set":
                    self.scores[(t[3], t[4])] = int(t[5])
                    reply = f"Set [{t[4]}] for {t[3]} to {t[5]}"
                else:
                    reply = "Unknown command"
            elif t[0] == "function" and t[1] == f"{self.ns}:rx_commit":
                words = [self.scores.get((f"w{k}", M.OBJ_REG), 0) for k in range(6)]
                frame = M.words_to_frame(words)
                self.barrels = M.frame_to_items(frame)
                self.scores[("rx_seq", M.OBJ_CTL)] = self.scores.get(("rx_seq", M.OBJ_CTL), 0) + 1
                self._log("DCF RX " + " ".join(map(str, words)), chat=True)
                reply = f"Executed 4 command(s) from function '{self.ns}:rx_commit'"
            elif t[0] == "function" and t[1] == f"{self.ns}:tx_done":
                self.scores[("tx_pending", M.OBJ_CTL)] = 0
                reply = f"Executed 1 command(s) from function '{self.ns}:tx_done'"
            else:
                reply = "Unknown command"
            self._log(reply)
            return reply

    # ── RCON ────────────────────────────────────────────────────────────────
    def _rcon_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._rcon_conn, args=(conn,), daemon=True).start()

    def _rcon_conn(self, conn):
        authed = False
        try:
            while not self._stop.is_set():
                head = self._recvn(conn, 4)
                if not head:
                    return
                (n,) = struct.unpack("<i", head)
                body = self._recvn(conn, n)
                pid, ptype = struct.unpack("<ii", body[:8])
                text = body[8:-2].decode()
                if ptype == 3:
                    authed = text == PASSWORD
                    self._send(conn, pid if authed else -1, 2, "")
                elif ptype == 2 and authed:
                    self._send(conn, pid, 0, self.execute(text) if text else "")
                else:
                    self._send(conn, -1, 2, "")
        except OSError:
            pass
        finally:
            conn.close()

    @staticmethod
    def _recvn(conn, n):
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                return b""
            buf += c
        return buf

    @staticmethod
    def _send(conn, pid, ptype, text):
        data = text.encode() + b"\x00\x00"
        conn.sendall(struct.pack("<iii", 8 + len(data), pid, ptype) + data)

    # ── FIFO ────────────────────────────────────────────────────────────────
    def _fifo_loop(self):
        # A persistent NON-BLOCKING read end: a writer's open() never blocks (a real server
        # keeps its stdin FIFO open the same way), and close() never has to hand-shake.
        import select
        fd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        buf = b""
        try:
            while not self._stop.is_set():
                r, _, _ = select.select([fd], [], [], 0.1)
                if not r:
                    continue
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:            # a writer connected between select and read
                    continue
                if not data:                       # no writer right now
                    time.sleep(0.05)
                    continue
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self.execute(line.decode().strip())
        finally:
            os.close(fd)


class TestSidecar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.srv = FakeServer(self.tmp)
        self.pw = os.path.join(self.tmp, "rcon.pw")
        with open(self.pw, "w") as fh:
            fh.write(PASSWORD + "\n")
        self.inhex = os.path.join(self.tmp, "in.hex")
        with open(self.inhex, "w") as fh:
            fh.write(GOLDEN.hex() + "\n")

    def tearDown(self):
        self.srv.close()

    def rcon_uri(self):
        return f"mc:rcon=127.0.0.1:{self.srv.port},pass_file={self.pw}"

    def fifo_uri(self):
        return f"mc:fifo={self.srv.fifo},log={self.srv.log}"

    def roundtrip(self, uri):
        # ingress: golden frame -> barrels hold the pinned item counts
        st = medium.run_io(f"hex:path={self.inhex}", uri)
        self.assertEqual(st["frames_out"], 1, st)
        self.assertEqual(self.srv.barrels, M.frame_to_items(GOLDEN))
        # egress: a strobe latches the comparators' reading -> the same frame comes out
        out = os.path.join(self.tmp, "out.hex")
        threading.Timer(0.3, self.srv.strobe).start()
        st = medium.run_io(uri, f"hex:path={out}", expect=1, seconds=6)
        self.assertEqual(st["frames_out"], 1, st)
        with open(out) as fh:
            self.assertEqual(fh.read().strip(), GOLDEN.hex())
        self.assertEqual(self.srv.scores[("ack", M.OBJ_CTL)], 1)
        self.assertEqual(self.srv.scores[("tx_pending", M.OBJ_CTL)], 0)

    def test_rcon_roundtrip(self):
        self.roundtrip(self.rcon_uri())

    def test_fifo_roundtrip(self):
        self.roundtrip(self.fifo_uri())

    def test_chat_egress_no_console(self):
        """A single-player world: no console at all, the DCF TX chat line is the egress."""
        self.srv.barrels = M.frame_to_items(GOLDEN)
        out = os.path.join(self.tmp, "out.hex")
        threading.Timer(0.3, self.srv.strobe).start()
        st = medium.run_io(f"mc:log={self.srv.log},egress=chat", f"hex:path={out}", expect=1, seconds=6)
        self.assertEqual(st["frames_out"], 1, st)
        with open(out) as fh:
            self.assertEqual(fh.read().strip(), GOLDEN.hex())
        self.assertEqual(self.srv.scores[("ack", M.OBJ_CTL)], 0)   # nothing could ack: fine
        self.assertEqual(self.srv.scores[("tx_pending", M.OBJ_CTL)], 1)  # and nothing cleared it

    def test_nak_on_bad_crc(self):
        bad = bytearray(GOLDEN)
        bad[9] ^= 0x01                                      # payload bit flipped, CRC stale
        self.srv.barrels = M.frame_to_items(bytes(bad))
        out = os.path.join(self.tmp, "out.hex")
        threading.Timer(0.3, self.srv.strobe).start()
        st = medium.run_io(self.rcon_uri(), f"hex:path={out}", seconds=2)
        self.assertEqual(st["frames_out"], 0)
        self.assertEqual(st["invalid_frames"], 1, st)        # gated by the pipeline, counted once
        self.assertEqual(self.srv.scores[("ack", M.OBJ_CTL)], 2)

    def test_usage_errors(self):
        with self.assertRaises(medium.UsageError):
            medium.open_reader("mc:")
        with self.assertRaises(medium.UsageError):
            medium.open_writer(f"mc:log={self.srv.log}")     # no console: cannot write
        with self.assertRaises(medium.UsageError):
            medium.parse_uri("mc:password=x")               # never on the URI

    def test_rcon_auth_refused(self):
        c = RconClient("127.0.0.1", self.srv.port, "wrong")
        with self.assertRaises(RconError):
            c.connect()

    def test_logtail_rotation(self):
        tail = LogTail(self.srv.log).start()
        seen = []
        tail.subscribe(lambda line, m: seen.append(m))
        time.sleep(0.1)
        self.srv._log("one")
        os.replace(self.srv.log, self.srv.log + ".1")      # rotation: new inode
        open(self.srv.log, "w").close()
        time.sleep(0.3)
        self.srv._log("two", chat=True)
        deadline = time.time() + 3
        while "two" not in seen and time.time() < deadline:
            time.sleep(0.05)
        tail.stop()
        self.assertIn("one", seen)
        self.assertIn("two", seen)          # chat prefix stripped
        self.assertTrue(all(not s.startswith("[") for s in seen))

    def bot_uri(self):
        bot = "|".join([sys.executable, os.path.join(PYDIR, "tests", "_fake_bot.py"),
                        str(self.srv.port), PASSWORD, self.srv.log])
        return f"mc:bot={bot}"

    def test_bot_roundtrip(self):
        """A Mineflayer-style operator bot as the console: commands over the bot, egress via
        the bot's chat stream (no log file, no RCON from the sidecar itself)."""
        self.roundtrip(self.bot_uri())

    def test_bot_chat_egress(self):
        self.srv.barrels = M.frame_to_items(GOLDEN)
        out = os.path.join(self.tmp, "out.hex")
        threading.Timer(0.4, self.srv.strobe).start()
        st = medium.run_io(self.bot_uri() + ",egress=chat", f"hex:path={out}", expect=1, seconds=6)
        self.assertEqual(st["frames_out"], 1, st)
        with open(out) as fh:
            self.assertEqual(fh.read().strip(), GOLDEN.hex())
        # chat lines did not get mistaken for command replies: the console still answers
        c = medium.make_transport(self.bot_uri()).reg.c
        try:
            self.assertRegex(c.exec("scoreboard players get tx_seq dcf_ctl", reply="has"), r"^tx_seq has \d+")
        finally:
            c.close()

    def test_punctim_media_lists_mc(self):
        import punctim
        self.assertIn("mc", punctim.MEDIA)
        self.assertEqual(medium.parse_uri("mc:rcon=h:1,ns=x")[1], {"rcon": "h:1", "ns": "x"})


if __name__ == "__main__":
    unittest.main()
