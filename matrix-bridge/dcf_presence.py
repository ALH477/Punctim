# SPDX-License-Identifier: LGPL-3.0-only
"""DCF presence / ping adapter — discover and ping multi-kind agents on a mesh.

An *adapter* over DCF-Text (which is itself an adapter over the 17-byte
``DeModFrame`` quantum).  Like every DCF adapter it introduces **no new wire
format**: presence probes and replies ride ordinary DCF-Text DATA frames, and
they are told apart from chat by the descriptor's flag byte, which L2 treats
as opaque — so the certified ``text_vectors.json`` certificate is untouched.

Flag bits (descriptor ``payload[2]``; bits 0-2 are DCF-Text's AGENT / MORE /
RELIABLE, bits 3-7 were reserved):

  bit 3  FLAG_PING (0x08)  presence probe:  "what agents are here, what kind?"
  bit 4  FLAG_PONG (0x10)  presence reply:  "I am here, I am kind K"

A presence message's payload is one deterministic line, **not** JSON (the
Exsecutor agent has no JSON parser and this layer is meant to be parseable in
any binding):

    DCP1 <kind> <node_id_hex> <name>\\n

e.g.  ``DCP1 echo 0x00a1 echo-agent\\n``.  Fields are space-separated,
LF-terminated UTF-8.  ``kind`` and ``name`` are constrained to ``[a-z0-9-]``
so no quoting is needed.  ``node_id_hex`` is ``0x`` + four lowercase hex
digits.

The module is deliberately dependency-free (stdlib only) so it runs anywhere
Python does, mirroring ``dcf_node.py`` / ``dcf_text.py``.  All functions are
pure and deterministic; ``PresenceTable`` iterates in insertion order.

Self-test:  python3 dcf_presence.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "python", "MCP"))
from textlab_core import (  # noqa: F401  (re-exported for callers)
    channel_id, packetize, TextReassembler, BROADCAST,
    FLAG_AGENT, FLAG_MORE, FLAG_RELIABLE,
)

# ── presence flag bits (opaque to L2 — they never change the framing bytes) ──
FLAG_PING = 0x08               # presence probe
FLAG_PONG = 0x10               # presence reply
FLAG_PRESENCE = FLAG_PING | FLAG_PONG

# A presence message always also carries the AGENT flag (it is agent traffic).
FLAGS_PING = FLAG_AGENT | FLAG_PING
FLAGS_PONG = FLAG_AGENT | FLAG_PONG

# ── the DCP1 line ─────────────────────────────────────────────────────────────
MAGIC = "DCP1"
LINE_MAX = 128                 # generous cap; kind/name are short by contract

_KIND_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")
_HEX = set("0123456789abcdef")


class PresenceError(ValueError):
    """Raised when a DCP1 line is malformed."""


def _check_token(tok, what):
    if not tok or len(tok) > 32 or any(c not in _KIND_CHARS for c in tok):
        raise PresenceError(f"{what} must be 1..32 chars of [a-z0-9-], got {tok!r}")


def pack_presence(kind, node_id, name):
    """Serialise one presence record into the canonical DCP1 line (with \\n)."""
    _check_token(kind, "kind")
    _check_token(name, "name")
    if not (0 <= node_id <= 0xFFFF):
        raise PresenceError(f"node_id must be u16, got {node_id!r}")
    return f"{MAGIC} {kind} {node_id:#06x} {name}\n"


def parse_presence(text):
    """Parse a DCP1 line.  Returns {"kind", "node_id", "name"} or None if the
    text is not a well-formed presence line (chat, empty, garbage)."""
    if not isinstance(text, str):
        return None
    body = text[:-1] if text.endswith("\n") else text
    if not body.startswith(MAGIC + " "):
        return None
    parts = body.split(" ")
    if len(parts) != 4:
        return None
    _, kind, node_hex, name = parts
    if len(node_hex) != 6 or node_hex[:2] != "0x" \
            or any(c not in _HEX for c in node_hex[2:]):
        return None
    try:
        _check_token(kind, "kind")
        _check_token(name, "name")
    except PresenceError:
        return None
    return {"kind": kind, "node_id": int(node_hex, 16), "name": name}


def is_presence(flags):
    """True if the descriptor flag byte marks a presence (PING or PONG) message."""
    return bool(flags & FLAG_PRESENCE)


def presence_kind(flags):
    """'ping' / 'pong' / None from a descriptor flag byte."""
    if flags & FLAG_PING:
        return "ping"
    if flags & FLAG_PONG:
        return "pong"
    return None


# ── roster ────────────────────────────────────────────────────────────────────
class PresenceTable:
    """Fold of PONGs into a roster: node_id -> record, in insertion order.

    Deterministic: iteration order is first-seen order, never a pointer or
    hash value (the repo's determinism rule, spec §9.3 / DCF_MEDIUM_SPEC).
    Re-seeing a node_id updates the record in place (keeping its slot).
    """

    def __init__(self):
        self._order = []                      # node_id, first-seen order
        self._rows = {}                       # node_id -> record dict

    def __len__(self):
        return len(self._order)

    def __contains__(self, node_id):
        return node_id in self._rows

    def __iter__(self):
        """Iterate records in first-seen (insertion) order."""
        for nid in self._order:
            yield self._rows[nid]

    def get(self, node_id):
        return self._rows.get(node_id)

    def kinds(self):
        """Distinct kinds present, in first-seen order."""
        seen, out = set(), []
        for row in self:
            if row["kind"] not in seen:
                seen.add(row["kind"])
                out.append(row["kind"])
        return out

    def upsert(self, kind, node_id, name, seq=None):
        """Insert or refresh one agent.  Returns the record."""
        rec = {"kind": kind, "node_id": node_id, "name": name, "seq": seq}
        if node_id not in self._rows:
            self._order.append(node_id)
        self._rows[node_id] = rec
        return rec

    def fold_pong(self, flags, text, seq=None):
        """Fold one inbound message.  If it is a PONG carrying a DCP1 line,
        upsert its record and return it; else return None."""
        if not (flags & FLAG_PONG):
            return None
        rec = parse_presence(text)
        if rec is None:
            return None
        return self.upsert(rec["kind"], rec["node_id"], rec["name"], seq=seq)

    def snapshot(self):
        """A JSON-friendly list of records, insertion-ordered."""
        return [dict(r) for r in self]


# ── selftest ──────────────────────────────────────────────────────────────────
def _selftest():
    # line codec round-trips
    line = pack_presence("echo", 0x00A1, "echo-agent")
    assert line == "DCP1 echo 0x00a1 echo-agent\n", line
    rec = parse_presence(line)
    assert rec == {"kind": "echo", "node_id": 0x00A1, "name": "echo-agent"}, rec

    # every kind we ship
    for kind in ("echo", "coordinator", "langgraph-llm", "lua", "exsecutor"):
        r = parse_presence(pack_presence(kind, 0x1234, f"{kind}-agent"))
        assert r["kind"] == kind and r["node_id"] == 0x1234

    # rejects: chat, empty, bad magic, bad hex, bad tokens, missing field
    for bad in ("", "hello there\n", "DCP1 echo 0x00a1\n",
                "DCP1 echo 0xZZZZ echo-agent\n", "DCP1 Echo 0x00a1 echo-agent\n",
                "DCP1 echo 0x00a1 echo_agent\n", "DCP2 echo 0x00a1 echo-agent\n",
                "DCP1  echo 0x00a1 echo-agent\n"):
        assert parse_presence(bad) is None, bad

    # flags
    assert is_presence(FLAGS_PING) and is_presence(FLAGS_PONG)
    assert not is_presence(FLAG_AGENT)
    assert presence_kind(FLAGS_PING) == "ping"
    assert presence_kind(FLAGS_PONG) == "pong"
    assert presence_kind(FLAG_AGENT) is None

    # table: insertion order, dedup by node_id, kind list
    t = PresenceTable()
    assert t.fold_pong(FLAGS_PONG, pack_presence("echo", 0x00A1, "a"), seq=1) is not None
    assert t.fold_pong(FLAGS_PONG, pack_presence("lua", 0x00B2, "b"), seq=2) is not None
    assert t.fold_pong(FLAGS_PONG, pack_presence("exsecutor", 0x00C3, "c"), seq=3) is not None
    # non-PONG and non-presence text are ignored
    assert t.fold_pong(FLAG_AGENT, pack_presence("echo", 0x00D4, "d")) is None
    assert t.fold_pong(FLAGS_PONG, "just chatting\n") is None
    # re-seeing 0x00A1 updates in place, keeps its slot
    assert t.fold_pong(FLAGS_PONG, pack_presence("echo", 0x00A1, "a2"), seq=4) is not None
    assert len(t) == 3
    order = [r["node_id"] for r in t]
    assert order == [0x00A1, 0x00B2, 0x00C3], order
    assert t.get(0x00A1)["name"] == "a2"
    assert t.kinds() == ["echo", "lua", "exsecutor"]

    print("dcf_presence selftest: CERTIFIED (line codec, flags, roster fold)")


if __name__ == "__main__":
    _selftest()
