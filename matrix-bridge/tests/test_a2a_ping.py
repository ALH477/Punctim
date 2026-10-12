"""Presence / ping tests — DCP1 line codec, roster fold, and a two-node
PING/PONG loopback over the DeModFrame mesh.

Pins the behaviour the multi-kind agent pinger relies on:
  * the DCP1 line codec round-trips and rejects malformed input;
  * a PING on one node is answered with a PONG carrying that node's identity;
  * a PresenceTable folds PONGs into an insertion-ordered roster;
  * chat and non-presence traffic never lands in the roster;
  * a node on a different channel does not answer.

The Exsecutor-agent subprocess round-trip lives in test_a2a_ping_exsc (it needs
exsc/fasmg and skips cleanly without them, like exsecutor/certify.sh).

Pure stdlib — runs under pytest or directly:
    python3 matrix-bridge/tests/test_a2a_ping.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
sys.path.insert(0, BRIDGE)
sys.path.insert(0, os.path.join(BRIDGE, "..", "python", "MCP"))

import dcf_presence as pres
from dcf_node import DcfTextNode

MY_ID = 0x00A1
MY_KIND = "echo"
MY_NAME = "echo-agent"
PEER_ID = 0x00B2
PEER_KIND = "exsecutor"
PEER_NAME = "exs-agent"


# ── line codec ────────────────────────────────────────────────────────────────
def test_line_codec_roundtrip():
    line = pres.pack_presence(MY_KIND, MY_ID, MY_NAME)
    assert line == "DCP1 echo 0x00a1 echo-agent\n"
    rec = pres.parse_presence(line)
    assert rec == {"kind": MY_KIND, "node_id": MY_ID, "name": MY_NAME}


def test_line_codec_rejects_garbage():
    bad = ["", "hello\n", "DCP1 echo 0x00a1\n", "DCP1 echo 0xZZZZ x\n",
           "DCP1 Echo 0x00a1 x\n", "DCP1 echo 0x00a1 x_y\n",
           "DCP2 echo 0x00a1 x\n", "DCP1  echo 0x00a1 x\n",
           "DCP1 " + "x" * 40 + " 0x00a1 x\n"]
    for b in bad:
        assert pres.parse_presence(b) is None, b


def test_flags():
    assert pres.presence_kind(pres.FLAGS_PING) == "ping"
    assert pres.presence_kind(pres.FLAGS_PONG) == "pong"
    assert pres.presence_kind(0x01) is None          # bare AGENT = chat
    assert pres.is_presence(pres.FLAGS_PING)
    assert not pres.is_presence(0x01)


# ── responder: answer a PING with a PONG carrying our identity ────────────────
def _answer_pings(node, my_kind, my_id, my_name, stop_flag):
    """Drain the node's inbox; for every PING, send back a PONG."""
    while not stop_flag[0]:
        ev = node.poll(timeout=0.2)
        if ev is None:
            continue
        _, src, _dst, text, flags, _ts = ev
        if not (flags & pres.FLAG_PING):
            continue                       # chat is not answered here
        pong = pres.pack_presence(my_kind, my_id, my_name)
        node.send_text(pong, flags=pres.FLAGS_PONG, to=None)  # broadcast back


def _pair(channel="duet"):
    a = DcfTextNode(node_id=MY_ID, port=0, channel=channel, use_superpack=True)
    b = DcfTextNode(node_id=PEER_ID, port=0, channel=channel, use_superpack=True)
    a.add_peer("b", "127.0.0.1", b.port)
    b.add_peer("a", "127.0.0.1", a.port)
    a.start()
    b.start()
    return a, b


def _ping_pong_once(pinger, responder, kind, node_id, name, timeout=3.0):
    """Send one PING; return the PONG record the responder's table folded."""
    import threading
    stop = [False]
    t = threading.Thread(target=_answer_pings,
                         args=(responder, kind, node_id, name, stop), daemon=True)
    t.start()
    try:
        ping = pres.pack_presence("pinger", 0x0000, "pinger")
        pinger.send_text(ping, flags=pres.FLAGS_PING, to=None)
        table = pres.PresenceTable()
        deadline = time.time() + timeout
        while time.time() < deadline:
            ev = pinger.poll(timeout=0.2)
            if ev is None:
                continue
            _, _src, _dst, text, flags, seq = ev
            rec = table.fold_pong(flags, text, seq=seq)
            if rec is not None:
                return rec
        return None
    finally:
        stop[0] = True
        t.join(timeout=1.0)


def test_ping_is_answered_with_pong():
    a, b = _pair()
    try:
        rec = _ping_pong_once(a, b, PEER_KIND, PEER_ID, PEER_NAME)
        assert rec is not None, "no PONG came back"
        assert rec["kind"] == PEER_KIND
        assert rec["node_id"] == PEER_ID
        assert rec["name"] == PEER_NAME
    finally:
        a.stop()
        b.stop()


def test_roster_folds_multiple_kinds_in_insertion_order():
    table = pres.PresenceTable()
    table.fold_pong(pres.FLAGS_PONG, pres.pack_presence("echo", 0x00A1, "a"), seq=1)
    table.fold_pong(pres.FLAGS_PONG, pres.pack_presence("lua", 0x00B2, "b"), seq=2)
    table.fold_pong(pres.FLAGS_PONG, pres.pack_presence("exsecutor", 0x00C3, "c"), seq=3)
    # chat never lands
    assert table.fold_pong(0x01, "just chatting\n") is None
    # a PONG that is not a DCP1 line never lands
    assert table.fold_pong(pres.FLAGS_PONG, "not a presence line\n") is None
    assert [r["node_id"] for r in table] == [0x00A1, 0x00B2, 0x00C3]
    assert table.kinds() == ["echo", "lua", "exsecutor"]
    # re-seeing a node updates in place, keeps its slot
    table.fold_pong(pres.FLAGS_PONG, pres.pack_presence("echo", 0x00A1, "a2"), seq=4)
    assert len(table) == 3
    assert table.get(0x00A1)["name"] == "a2"
    assert [r["node_id"] for r in table] == [0x00A1, 0x00B2, 0x00C3]


def test_wrong_channel_is_not_answered():
    a = DcfTextNode(node_id=MY_ID, port=0, channel="duet", use_superpack=True)
    b = DcfTextNode(node_id=PEER_ID, port=0, channel="other", use_superpack=True)
    a.add_peer("b", "127.0.0.1", b.port)
    a.start()
    b.start()
    try:
        ping = pres.pack_presence("pinger", 0x0000, "pinger")
        a.send_text(ping, flags=pres.FLAGS_PING, to="b")
        assert a.poll(timeout=0.5) is None, "a foreign-channel node answered"
    finally:
        a.stop()
        b.stop()


if __name__ == "__main__":
    test_line_codec_roundtrip()
    test_line_codec_rejects_garbage()
    test_flags()
    test_roster_folds_multiple_kinds_in_insertion_order()
    test_ping_is_answered_with_pong()
    test_wrong_channel_is_not_answered()
    print("test_a2a_ping: CERTIFIED (codec, roster, PING/PONG loopback, channel isolation)")
