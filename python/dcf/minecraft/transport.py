# SPDX-License-Identifier: LGPL-3.0-only
"""`mc:` — a Minecraft world as a DCF medium (Python-only; other `punctim`s exit 3).

Two egress modes:
  console  poll `tx_pending` through a Console (RCON / FIFO / bot) at `poll_hz`, read the
           words, ACK/NAK by the frame gate — works with nobody online
  chat     no console needed: follow a log (a Prism client's `logs/latest.log`, or a server's)
           for the datapack's self-announcing `DCF TX w0..w5` lines
Ingress (`_transmit`) always needs a Console: words -> scoreboard -> `function dcf:rx_commit`.
"""
import os
import sys
import threading
import time

from .. import transport as T
from .register import RegisterClient
from .logtail import LogTail

_MCP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "MCP")
if _MCP not in sys.path:
    sys.path.insert(0, _MCP)
import mclab_core as M  # noqa: E402


class MinecraftTransport(T.Transport):
    def __init__(self, name, console=None, log_path=None, namespace="dcf", egress=None,
                 poll_hz=4.0, tail=None):
        super().__init__(name, rate_bps=17 * 8 * 5)       # ~5 frames/s: a slow link
        if console is None and log_path is None and tail is None:
            raise T.MediumUnsupported("mc: needs a console (rcon=/fifo=/bot=) or log= for chat egress")
        self.reg = RegisterClient(console, namespace) if console is not None else None
        self.egress = egress or ("console" if console is not None else "chat")
        if self.egress == "console" and self.reg is None:
            raise T.MediumUnsupported("mc: egress=console needs a console")
        self._bot_chat = self.egress == "chat" and log_path is None and tail is None \
            and console is not None and hasattr(console, "on_chat")
        if self.egress == "chat" and log_path is None and tail is None and not self._bot_chat:
            raise T.MediumUnsupported("mc: egress=chat needs log= (or a bot console)")
        if self._bot_chat:
            console.on_chat = lambda text: self._on_log(None, text)   # the bot's chat is the log
        self.poll_s = 1.0 / max(0.1, poll_hz)
        self.tail = tail or (LogTail(log_path) if log_path else None)
        self._own_tail = self.tail is not None and not self.tail.running   # we start it => we stop it
        self._stop = threading.Event()
        self._thread = None
        self.invalid_datagrams = 0
        self.tx_seen = 0

    # ── ingress: peer -> world ─────────────────────────────────────────────
    def _transmit(self, frame, dest):
        if self.reg is None:
            raise T.MediumUnsupported("mc: writing into the world needs a console")
        self.reg.commit_rx(frame)

    # ── egress: world -> peer ──────────────────────────────────────────────
    def _start_recv(self):
        self._stop.clear()
        if self.tail is not None:
            self.tail.start()                 # idempotent
        if self.egress == "chat":
            if self.tail is not None:
                self.tail.subscribe(self._on_log)
        else:
            self._thread = threading.Thread(target=self._poll_loop, name="mc-poll", daemon=True)
            self._thread.start()

    def _stop_recv(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.tail is not None:
            if self.egress == "chat":
                self.tail.unsubscribe(self._on_log)
            if self._own_tail:
                self.tail.stop()
        if self.reg is not None:
            self.reg.c.close()

    def _on_log(self, line, m):
        frame = M.parse_chat_tx(m)
        if frame is None:
            return
        self.tx_seen += 1
        self._deliver(frame, {"egress": "chat"})

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                got = self.reg.poll_tx()
            except Exception as e:           # console hiccup: keep polling
                sys.stderr.write(f"[mc] poll error: {e}\n")
                got = None
                self._stop.wait(self.poll_s * 4)
                continue
            if got is not None:
                frame, valid = got
                self.tx_seen += 1
                self._deliver(frame, {"egress": "console", "valid": valid})
                continue
            self._stop.wait(self.poll_s)
