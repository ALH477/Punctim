# SPDX-License-Identifier: LGPL-3.0-only
"""Follow a Minecraft log (server `logs/latest.log` or a Prism client's) across rotation.

Lines look like `[12:00:00] [Server thread/INFO]: message` (server / integrated server)
or `[12:00:00] [Render thread/INFO]: [System] [CHAT] message` (client-side chat).  `msg()`
strips the prefix; callbacks get the raw line and the message."""
import os
import re
import threading
import time

LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[([^\]]+)/([A-Z]+)\]: (.*)$")
CHAT = re.compile(r"^\[System\] \[CHAT\] (.*)$")


def msg(line):
    """The message part of a log line (chat prefix stripped too), or the line itself."""
    m = LINE.match(line.rstrip("\r\n"))
    text = m.group(4) if m else line.rstrip("\r\n")
    c = CHAT.match(text)
    return c.group(1) if c else text


class LogTail:
    """Background tail of `path`, starting at its current end. Reopens on rotation
    (inode change) or truncation; missing files are retried."""

    def __init__(self, path, poll_s=0.05, from_start=False):
        self.path, self.poll_s, self.from_start = path, poll_s, from_start
        self._subs = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.lines = 0

    def subscribe(self, fn):
        """fn(line, message) on every new line (called on the tail thread)."""
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn):
        with self._lock:
            self._subs = [s for s in self._subs if s is not fn]

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="logtail", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def wait_for(self, pattern, timeout=5.0):
        """Block until a message matches `pattern` (regex); returns the match or None."""
        rx = re.compile(pattern)
        box, ev = [], threading.Event()

        def fn(line, m):
            r = rx.search(m)
            if r and not box:
                box.append(r)
                ev.set()

        self.subscribe(fn)
        try:
            ev.wait(timeout)
        finally:
            self.unsubscribe(fn)
        return box[0] if box else None

    # ── internals ───────────────────────────────────────────────────────────
    def _open(self):
        try:
            fp = open(self.path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return None, None
        st = os.fstat(fp.fileno())
        if not self.from_start:
            fp.seek(0, os.SEEK_END)
        return fp, (st.st_dev, st.st_ino)

    def _run(self):
        fp, ident = self._open()
        pos = fp.tell() if fp else 0
        while not self._stop.is_set():
            if fp is None:
                time.sleep(self.poll_s * 4)
                fp, ident = self._open()
                if fp:
                    self.from_start = True    # a (re)appeared file is read from its start
                    fp.seek(0)
                    pos = 0
                continue
            line = fp.readline()
            if line:
                pos += len(line.encode("utf-8", "replace"))
                if not line.endswith("\n"):
                    fp.seek(pos - len(line.encode("utf-8", "replace")))
                    time.sleep(self.poll_s)
                    continue
                self.lines += 1
                m = msg(line)
                with self._lock:
                    subs = list(self._subs)
                for fn in subs:
                    try:
                        fn(line, m)
                    except Exception:      # a subscriber must not kill the tail
                        pass
                continue
            # EOF: check rotation / truncation
            try:
                st = os.stat(self.path)
                rotated = (st.st_dev, st.st_ino) != ident or st.st_size < pos
            except OSError:
                rotated = True
            if rotated:
                fp.close()
                fp, ident = self._open()
                if fp:
                    fp.seek(0)
                    pos = 0
                continue
            time.sleep(self.poll_s)
        if fp:
            fp.close()
