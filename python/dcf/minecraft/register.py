# SPDX-License-Identifier: LGPL-3.0-only
"""The register protocol over a Console: scoreboard words in/out + the datapack functions."""
import os
import sys

_MCP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "MCP")
if _MCP not in sys.path:
    sys.path.insert(0, _MCP)
import mclab_core as M  # noqa: E402
import wirelab_core as wire  # noqa: E402

WORDS = [f"w{k}" for k in range(M.WORDS)]


class RegisterClient:
    def __init__(self, console, namespace="dcf"):
        self.c, self.ns = console, namespace

    # ── scoreboard primitives ──────────────────────────────────────────────
    def get(self, holder, obj=M.OBJ_REG, timeout=3.0):
        text = self.c.exec(f"scoreboard players get {holder} {obj}",
                           reply=rf"^{holder} has -?\d+ \[", timeout=timeout)
        m = M.SCORE_REPLY.search(text.strip())
        if not m or m.group(1) != holder:
            raise RuntimeError(f"no score reply for {holder} {obj}: {text!r}")
        return int(m.group(2))

    def set(self, holder, value, obj=M.OBJ_REG):
        self.c.exec(f"scoreboard players set {holder} {obj} {int(value)}")

    def function(self, name, timeout=3.0):
        """Run a datapack function and wait for its feedback line, so that a log-based
        console (FIFO/pipe) returns only once the world has executed it."""
        return self.c.exec(f"function {self.ns}:{name}", reply=rf"function '{self.ns}:{name}'",
                           timeout=timeout)

    # ── register operations ────────────────────────────────────────────────
    def read_words(self):
        return [self.get(w) for w in WORDS]

    def write_words(self, words):
        for h, v in zip(WORDS, words):
            self.set(h, v)

    def commit_rx(self, frame):
        """Write a frame into the OUT lanes: words -> scoreboard -> dcf:rx_commit."""
        self.write_words(M.frame_to_words(bytes(frame)))
        self.function("rx_commit")

    def poll_tx(self):
        """If the world latched a frame (tx_pending == 1) read it, ACK/NAK it by the gate,
        clear tx_pending, and return (frame, valid); else None."""
        if self.get("tx_pending", M.OBJ_CTL) != 1:
            return None
        frame = M.words_to_frame(self.read_words())
        valid = wire.syndrome(frame) == 0 and frame[0] == 0xD3 and (frame[1] >> 4) == 1
        self.set("ack", 1 if valid else 2, M.OBJ_CTL)
        self.function("tx_done")
        return frame, valid

    def ack(self, valid):
        self.set("ack", 1 if valid else 2, M.OBJ_CTL)
