# SPDX-License-Identifier: LGPL-3.0-only
"""Ways to run a command on a Minecraft server and read its reply.

  RconConsole        enable-rcon on any vanilla/Paper/Fabric server (reply comes back inline)
  FifoConsole        the nixpkgs `services.minecraft-server` stdin FIFO, or a stdin PIPE of a
                     server you spawned (`nix run .#minecraft-server-dev`); replies are read
                     from the server log (`logs/latest.log`) through a LogTail
  SubprocessConsole  a helper process that speaks JSON lines ({"cmd": ...} in,
                     {"reply": ...} out) — the Mineflayer bot in minecraft/tools/bot joins a
                     LAN world / server as an operator and runs commands as that player

Every console serialises its commands; `exec(cmd, reply=None, timeout=...)` returns the
reply text (or "" when none is expected/available).  `reply` is a regex the expected reply
line matches — needed by the log-based consoles to pair asynchronous output with a command."""
import json
import queue
import re
import subprocess
import threading


class Console:
    def exec(self, cmd, reply=None, timeout=3.0):
        raise NotImplementedError

    def close(self):
        pass


class RconConsole(Console):
    def __init__(self, client):
        self.client = client

    def exec(self, cmd, reply=None, timeout=3.0):
        return self.client.exec(cmd)

    def close(self):
        self.client.close()


class FifoConsole(Console):
    """Write commands to a FIFO path or an open writable file object; read replies from the
    server log via `tail` (a started LogTail)."""

    def __init__(self, target, tail):
        self._own = isinstance(target, str)
        self._fp = open(target, "w", buffering=1) if self._own else target
        self.tail = tail
        self._lock = threading.Lock()

    def exec(self, cmd, reply=None, timeout=3.0):
        with self._lock:
            box, ev = [], threading.Event()
            rx = re.compile(reply) if reply else None

            def fn(line, m):
                if rx is not None and rx.search(m) and not box:
                    box.append(m)
                    ev.set()

            if rx is not None:
                self.tail.subscribe(fn)
            try:
                try:
                    self._fp.write(cmd + "\n")
                    self._fp.flush()
                except BrokenPipeError:           # the server (the FIFO's reader) is gone
                    raise RuntimeError("console FIFO has no reader (server down?)")
                if rx is None:
                    return ""
                ev.wait(timeout)
            finally:
                if rx is not None:
                    self.tail.unsubscribe(fn)
            return box[0] if box else ""

    def close(self):
        if self._own:
            self._fp.close()


class SubprocessConsole(Console):
    """A JSON-lines helper: we write {"cmd": ..., "reply": <regex>|null, "timeout": s} and it
    answers exactly one {"reply": "..."} per command (empty when none was expected or it
    expired). It may ALSO emit {"chat": "..."} lines at any time — every other chat/system
    line it sees — which go to `on_chat(text)` (a chat-egress source with no log file).
    Used for the Mineflayer bot (minecraft/tools/bot/dcf_bot.js) on LAN worlds / servers
    without RCON, where the bot is an operator player."""

    def __init__(self, argv, on_chat=None):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.on_chat = on_chat
        self._lock = threading.Lock()
        self._replies = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, name="console-helper", daemon=True)
        self._reader.start()

    def _read_loop(self):
        for line in self.proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "reply" in d:
                self._replies.put(d.get("reply") or "")
            elif "chat" in d and self.on_chat is not None:
                try:
                    self.on_chat(d["chat"])
                except Exception:
                    pass
        self._replies.put(None)                 # EOF: the helper exited

    def exec(self, cmd, reply=None, timeout=3.0):
        with self._lock:
            if self.proc.poll() is not None:
                raise RuntimeError("console helper exited")
            self.proc.stdin.write(json.dumps({"cmd": cmd, "reply": reply, "timeout": timeout}) + "\n")
            self.proc.stdin.flush()
            try:
                r = self._replies.get(timeout=timeout + 1.0)
            except queue.Empty:
                return ""
            if r is None:
                raise RuntimeError("console helper exited")
            return r

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()
