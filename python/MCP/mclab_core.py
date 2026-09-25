# SPDX-License-Identifier: LGPL-3.0-only
"""DCF MinecraftLab core — the reference model for DCF-Minecraft (the certified layer).

DCF-Minecraft puts a *conforming* 17-byte DeModFrame register inside a Minecraft world
and lets command blocks / redstone / a Paper plugin / a Fabric mod / a vanilla Bedrock
client exchange frames with any Punctim peer.  Three things are byte-pinned here and
certified in Python and Java (Documentation/minecraft_vectors.json):

  1. the REGISTER model — frame <-> 34 nibbles <-> 17 bytes <-> 6 scoreboard words,
     and nibble <-> barrel item count (comparator signal strength 0..15);
  2. the EVENT sub-type bodies carried as DCF-Game EVENT (msg_type 2) messages
     (REDSTONE / BLOCK_SET / CMD_TRIGGER / SCOREBOARD);
  3. the rendezvous channels (crc16 of a name, the repo-wide trick).

Everything else (mcfunction text, RCON, log tailing, the Bedrock WebSocket) is
loopback-tested, not vectored.  Spec: Documentation/DCF_MINECRAFT_SPEC.md.

Register layout (origin O = (x, y, z), lane pitch 2 blocks on +x):
  OUT lane i (0..33): barrel at (x+2i, y, z); item count -> comparator strength = nibble i
  IN  lane i        : redstone_wire at (x+2i, y, z+3), power 0..15 = nibble i
  STROBE_IN (x-2,y,z+3)   VALID_OUT (x-2,y,z)   ACK_OUT (x-4,y,z)   NAK_OUT (x-6,y,z)
Nibble 0 is the HIGH nibble of byte 0 (the 0xD sync nibble), nibble 33 the low nibble
of the CRC's second byte.  Words w0..w4 are 3-byte big-endian, w5 is the 2-byte CRC;
every word is < 2^31 so it fits a scoreboard score.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wirelab_core import crc16_ccitt, decode, encode  # certified DeModFrame codec
import gamelab_core as G

# ── register constants ────────────────────────────────────────────────────────
FRAME_LEN = 17
NIBBLES = 2 * FRAME_LEN                  # 34
WORDS = 6                                # w0..w4 = 3 bytes, w5 = 2 bytes (CRC)
WORD_BYTES = (3, 3, 3, 3, 3, 2)
BARREL_SLOTS = 27                        # a barrel, not a dropper (9)
SLOT_CAPACITY = 64
BARREL_CAPACITY = BARREL_SLOTS * SLOT_CAPACITY   # 1728
LANE_PITCH = 2

OBJ_REG = "dcf_reg"                      # n0..n33, b0..b16, w0..w5
OBJ_CTL = "dcf_ctl"                      # control holders below
CTL_HOLDERS = ("tx_pending", "tx_seq", "rx_seq", "strobe_prev", "strobe_now", "ack",
               "valid_ttl", "ack_ttl", "nak_ttl")

# control-lane offsets relative to the origin: (dx, dy, dz)
LANE_OUT_DZ, LANE_CMP_DZ, LANE_MID_DZ, LANE_IN_DZ = 0, 1, 2, 3
CTRL = {"strobe_in": (-2, 0, 3), "valid_out": (-2, 0, 0),
        "ack_out": (-4, 0, 0), "nak_out": (-6, 0, 0)}

# ── channels ──────────────────────────────────────────────────────────────────
def channel_id(name):
    """16-bit rendezvous dst for a channel name (textlab_core.channel_id, verbatim)."""
    if not name:
        return 0xFFFF
    return crc16_ccitt(name.encode("utf-8"))

CH_WORLD_NAME, CH_CHAT_NAME = "mc-world", "mc-chat"
CH_WORLD = channel_id(CH_WORLD_NAME)     # 0xD952 — DCF-Game EVENT traffic
CH_CHAT = channel_id(CH_CHAT_NAME)       # 0xE624 — DCF-Text chat


# ── nibbles / bytes / words ───────────────────────────────────────────────────
def bytes_to_nibbles(data):
    """bytes -> list of nibbles, high nibble first (the old demo's helper, kept)."""
    out = []
    for b in data:
        out.append((b >> 4) & 0x0F)
        out.append(b & 0x0F)
    return out


def nibbles_to_bytes(nibbles):
    if len(nibbles) % 2:
        raise ValueError("odd nibble count")
    return bytes(((nibbles[i] & 0xF) << 4) | (nibbles[i + 1] & 0xF)
                 for i in range(0, len(nibbles), 2))


def frame_to_nibbles(frame):
    if len(frame) != FRAME_LEN:
        raise ValueError("a DeModFrame is 17 bytes")
    return bytes_to_nibbles(frame)


def nibbles_to_frame(nibbles):
    if len(nibbles) != NIBBLES:
        raise ValueError(f"a register holds {NIBBLES} nibbles")
    return nibbles_to_bytes(nibbles)


def frame_to_words(frame):
    """17 bytes -> [w0..w5]: five 3-byte big-endian words then the 2-byte CRC."""
    if len(frame) != FRAME_LEN:
        raise ValueError("a DeModFrame is 17 bytes")
    words, off = [], 0
    for n in WORD_BYTES:
        words.append(int.from_bytes(frame[off:off + n], "big"))
        off += n
    return words


def words_to_frame(words):
    if len(words) != WORDS:
        raise ValueError(f"a register holds {WORDS} words")
    out = b""
    for w, n in zip(words, WORD_BYTES):
        if not (0 <= w < (1 << (8 * n))):
            raise ValueError(f"word {w} does not fit {n} bytes")
        out += int(w).to_bytes(n, "big")
    return out


# ── comparator arithmetic (Minecraft: signal = floor(fullness*14) + (items>0)) ─
def items_to_signal(items, capacity=BARREL_CAPACITY):
    """Item count in an all-stackable container -> comparator strength 0..15."""
    if items <= 0:
        return 0
    if items > capacity:
        raise ValueError(f"{items} items exceed capacity {capacity}")
    return (items * 14) // capacity + 1


def signal_to_items(signal, capacity=BARREL_CAPACITY):
    """Smallest item count giving exactly `signal` (0..15). 15 = a full container."""
    if not (0 <= signal <= 15):
        raise ValueError(f"signal strength must be 0..15, got {signal}")
    if signal == 0:
        return 0
    if signal == 1:
        return 1
    items = -(-(signal - 1) * capacity // 14)      # ceil((s-1)*cap/14)
    assert items_to_signal(items, capacity) == signal
    return items


SIGNAL_TABLE = [signal_to_items(s) for s in range(16)]


def frame_to_items(frame):
    """Per OUT lane: the barrel item count that makes its comparator read that nibble."""
    return [signal_to_items(n) for n in frame_to_nibbles(frame)]


# ── geometry ──────────────────────────────────────────────────────────────────
def lane_pos(origin, i, kind):
    """World position of lane i: kind in out|cmp|mid|in, or a CTRL key."""
    x, y, z = origin
    if kind in CTRL:
        dx, dy, dz = CTRL[kind]
        return (x + dx, y + dy, z + dz)
    if not (0 <= i < NIBBLES):
        raise ValueError("lane 0..33")
    dz = {"out": LANE_OUT_DZ, "cmp": LANE_CMP_DZ, "mid": LANE_MID_DZ, "in": LANE_IN_DZ}[kind]
    return (x + LANE_PITCH * i, y, z + dz)


# ── console reply grammar ─────────────────────────────────────────────────────
# `scoreboard players get <holder> <obj>` -> "<holder> has <n> [<display>]"
SCORE_REPLY = re.compile(r"^(\S+) has (-?\d+) \[([^\]]+)\]$")
# self-announcing egress from dcf:latch (tellraw), as it appears in a client log
CHAT_TX = re.compile(r"DCF TX((?: -?\d+){6})\b")


def parse_chat_tx(line):
    """A 'DCF TX w0 w1 w2 w3 w4 w5' chat line -> 17-byte frame, or None."""
    m = CHAT_TX.search(line)
    if not m:
        return None
    words = [int(t) for t in m.group(1).split()]
    try:
        return words_to_frame(words)
    except ValueError:
        return None


# ── EVENT sub-types (DCF-Game msg_type 2, tag byte at payload[0]) ─────────────
EVT_REDSTONE, EVT_BLOCK_SET, EVT_CMD_TRIGGER, EVT_SCOREBOARD = 1, 2, 3, 4
EVT_NAMES = {EVT_REDSTONE: "REDSTONE", EVT_BLOCK_SET: "BLOCK_SET",
             EVT_CMD_TRIGGER: "CMD_TRIGGER", EVT_SCOREBOARD: "SCOREBOARD"}
DIM_OVERWORLD, DIM_NETHER, DIM_END, DIM_OTHER = 0, 1, 2, 255
DIM_NAMES = {"minecraft:overworld": DIM_OVERWORLD, "minecraft:the_nether": DIM_NETHER,
             "minecraft:the_end": DIM_END}
MAX_EVENT = G.MAX_PAYLOAD                # 124 bytes including the tag


def _i32(v):
    return (int(v) & 0xFFFFFFFF).to_bytes(4, "big")


def _u32s(b):
    v = int.from_bytes(b, "big")
    return v - (1 << 32) if v & 0x80000000 else v


def _i16(v):
    return (int(v) & 0xFFFF).to_bytes(2, "big")


def _u16s(b):
    v = int.from_bytes(b, "big")
    return v - (1 << 16) if v & 0x8000 else v


def _pos(e):
    return bytes([e["dim"] & 0xFF]) + _i32(e["x"]) + _i16(e["y"]) + _i32(e["z"])


def _unpos(b):
    return {"dim": b[0], "x": _u32s(b[1:5]), "y": _u16s(b[5:7]), "z": _u32s(b[7:11])}


def _lstr(s, limit):
    raw = s.encode("utf-8")
    if len(raw) > limit:
        raise ValueError(f"string {len(raw)}B exceeds {limit}B")
    return bytes([len(raw)]) + raw


def event_pack(e):
    """dict -> EVENT body (tag + fields), <= 124 bytes.  Byte-deterministic."""
    tag = e["tag"]
    if tag == EVT_REDSTONE:
        body = bytes([tag]) + _pos(e) + bytes([e["old"] & 0xFF, e["new"] & 0xFF])
    elif tag == EVT_BLOCK_SET:
        body = bytes([tag]) + _pos(e) + _lstr(e["id"], MAX_EVENT - 13)
    elif tag == EVT_CMD_TRIGGER:
        body = bytes([tag]) + _i16(e["id"]) + _lstr(e.get("arg", ""), MAX_EVENT - 4)
    elif tag == EVT_SCOREBOARD:
        obj, holder = _lstr(e["objective"], 255), _lstr(e["holder"], 255)
        body = bytes([tag]) + obj + holder + _i32(e["value"])
    else:
        raise ValueError(f"unknown EVENT tag {tag}")
    if len(body) > MAX_EVENT:
        raise ValueError(f"EVENT body {len(body)}B exceeds {MAX_EVENT}B")
    return body


def event_unpack(body):
    """EVENT body -> dict (inverse of event_pack); raises ValueError on a bad body."""
    if not body:
        raise ValueError("empty EVENT body")
    tag = body[0]
    if tag == EVT_REDSTONE:
        if len(body) != 14:
            raise ValueError("REDSTONE body is 14 bytes")
        e = {"tag": tag}
        e.update(_unpos(body[1:12]))
        e["old"], e["new"] = body[12], body[13]
        return e
    if tag == EVT_BLOCK_SET:
        if len(body) < 13 or len(body) != 13 + body[12]:
            raise ValueError("bad BLOCK_SET length")
        e = {"tag": tag}
        e.update(_unpos(body[1:12]))
        e["id"] = body[13:13 + body[12]].decode("utf-8")
        return e
    if tag == EVT_CMD_TRIGGER:
        if len(body) < 4 or len(body) != 4 + body[3]:
            raise ValueError("bad CMD_TRIGGER length")
        return {"tag": tag, "id": int.from_bytes(body[1:3], "big"),
                "arg": body[4:4 + body[3]].decode("utf-8")}
    if tag == EVT_SCOREBOARD:
        if len(body) < 2:
            raise ValueError("bad SCOREBOARD length")
        ol = body[1]
        obj = body[2:2 + ol]
        p = 2 + ol
        if len(body) < p + 1:
            raise ValueError("bad SCOREBOARD length")
        hl = body[p]
        holder = body[p + 1:p + 1 + hl]
        p = p + 1 + hl
        if len(body) != p + 4:
            raise ValueError("bad SCOREBOARD length")
        return {"tag": tag, "objective": obj.decode("utf-8"),
                "holder": holder.decode("utf-8"), "value": _u32s(body[p:p + 4])}
    raise ValueError(f"unknown EVENT tag {tag}")


def event_packetize(e, packet_id, ts_us, src, dst=CH_WORLD, flags=G.FLAG_RELIABLE):
    """One Minecraft event -> DCF-Game EVENT frames on the mc-world channel."""
    return G.packetize(G.GMSG_EVENT, event_pack(e), packet_id, ts_us, src, dst, flags)


# ── self-test ─────────────────────────────────────────────────────────────────
GOLDEN = bytes.fromhex("d31312340001ffffdeadbeefab12cd24c0")


def _selftest():
    assert CH_WORLD == 0xD952 and CH_CHAT == 0xE624 and channel_id("duet") == 0xEED7
    assert SIGNAL_TABLE == [0, 1, 124, 247, 371, 494, 618, 741, 864, 988, 1111, 1235,
                            1358, 1482, 1605, 1728]
    for s in range(16):
        assert items_to_signal(signal_to_items(s)) == s
    n = frame_to_nibbles(GOLDEN)
    assert n[:2] == [0xD, 0x3] and nibbles_to_frame(n) == GOLDEN
    w = frame_to_words(GOLDEN)
    assert w == [13832978, 3407873, 16777182, 11386607, 11211469, 9408]
    assert words_to_frame(w) == GOLDEN and max(w) < (1 << 31)
    assert parse_chat_tx("[12:00:00] [Render thread/INFO]: [System] [CHAT] DCF TX "
                         + " ".join(map(str, w))) == GOLDEN
    assert decode(GOLDEN)["crc"] == 0x24C0
    for e in ({"tag": EVT_REDSTONE, "dim": 0, "x": -317, "y": 290, "z": -6, "old": 0, "new": 15},
              {"tag": EVT_BLOCK_SET, "dim": 1, "x": 1, "y": -64, "z": 2, "id": "minecraft:redstone_block"},
              {"tag": EVT_CMD_TRIGGER, "id": 7, "arg": "door"},
              {"tag": EVT_SCOREBOARD, "objective": "dcf_ctl", "holder": "tx_seq", "value": -1}):
        assert event_unpack(event_pack(e)) == e
        fr = event_packetize(e, 5, 0x123456, 0x00B1)
        assert all(decode(f)["dst"] == CH_WORLD for f in fr)
    print("mclab_core: CERTIFIED (register, signal table, words, events, channels)")


if __name__ == "__main__":
    _selftest()
