# SPDX-License-Identifier: LGPL-3.0-only
"""Executable laws + golden-vector generator for DCF-Minecraft.

Mirrors gen_game_vectors.py: assert the register / signal-table / word / EVENT laws,
then emit the finite vectors the Java binding (MinecraftCertify) and the sidecar
certify against byte-for-byte.

Usage:  python3 gen_minecraft_vectors.py [minecraft_vectors.json]
Exit 0 iff every law holds.  Commit identical copies to Documentation/ and python/MCP/.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mclab_core as M
import gamelab_core as G
from wirelab_core import decode, encode

random.seed(0x4D43)      # "MC"
ok = lambda name: print(f"  PASS  {name}")

# ── Law A: the signal table is exact and round-trips for all 16 nibble values ─
assert M.BARREL_CAPACITY == 1728
for s in range(16):
    items = M.signal_to_items(s)
    assert M.items_to_signal(items) == s
    if s >= 2:                                    # minimality: one fewer item reads s-1
        assert M.items_to_signal(items - 1) == s - 1
assert M.signal_to_items(15) == M.BARREL_CAPACITY
ok("signal table: 16 values, minimal counts, 15 = full barrel")

# ── Law B: frame <-> nibbles <-> words are bijections on random valid frames ──
basis = []
for i in range(24):
    f = encode(random.randrange(4), random.randrange(1 << 16), random.randrange(1 << 16),
               random.randrange(1 << 16), bytes(random.randrange(256) for _ in range(4)),
               random.randrange(1 << 24))
    n = M.frame_to_nibbles(f)
    w = M.frame_to_words(f)
    assert M.nibbles_to_frame(n) == f and M.words_to_frame(w) == f
    assert len(n) == 34 and len(w) == 6 and all(0 <= x < (1 << 31) for x in w)
    assert n[0] == 0xD and n[1] == 0x3 and n[2] == 1        # sync + version nibbles
    assert decode(f)["crc"] == w[5]
    basis.append(f)
ok("register: frame <-> 34 nibbles <-> 6 words, words < 2^31, w5 == CRC")

# ── Law C: the chat egress line decodes to the same frame ─────────────────────
for f in basis:
    line = "[00:00:00] [Render thread/INFO]: [System] [CHAT] DCF TX " + \
           " ".join(str(w) for w in M.frame_to_words(f))
    assert M.parse_chat_tx(line) == f
assert M.parse_chat_tx("DCF TX 1 2 3") is None
ok("chat egress: 'DCF TX w0..w5' round-trips")

# ── Law D: EVENT bodies round-trip, fit 124 B, and packetize on mc-world ──────
events = [
    {"tag": M.EVT_REDSTONE, "dim": 0, "x": 0, "y": 64, "z": 0, "old": 0, "new": 15},
    {"tag": M.EVT_REDSTONE, "dim": 1, "x": -317, "y": 290, "z": -6, "old": 15, "new": 0},
    {"tag": M.EVT_REDSTONE, "dim": 2, "x": 2147483647, "y": -32768, "z": -2147483648,
     "old": 7, "new": 8},
    {"tag": M.EVT_BLOCK_SET, "dim": 0, "x": 2, "y": 64, "z": 3, "id": "minecraft:redstone_block"},
    {"tag": M.EVT_BLOCK_SET, "dim": 255, "x": -1, "y": -64, "z": 1, "id": "minecraft:air"},
    {"tag": M.EVT_BLOCK_SET, "dim": 0, "x": 0, "y": 0, "z": 0, "id": "x" * 111},
    {"tag": M.EVT_CMD_TRIGGER, "id": 0, "arg": ""},
    {"tag": M.EVT_CMD_TRIGGER, "id": 65535, "arg": "door"},
    {"tag": M.EVT_CMD_TRIGGER, "id": 7, "arg": "ünïcødé ✓"},
    {"tag": M.EVT_SCOREBOARD, "objective": "dcf_ctl", "holder": "tx_seq", "value": 1},
    {"tag": M.EVT_SCOREBOARD, "objective": "dcf_reg", "holder": "w5", "value": -2147483648},
    {"tag": M.EVT_SCOREBOARD, "objective": "o", "holder": "h", "value": 2147483647},
]
event_cases = []
for i, e in enumerate(events):
    body = M.event_pack(e)
    assert len(body) <= M.MAX_EVENT and body[0] == e["tag"]
    assert M.event_unpack(body) == e
    packet_id, ts = (i * 101) % (G.MAX_PACKET_ID + 1), (i * 0x0F0F0F) % (1 << 24)
    frames = M.event_packetize(e, packet_id, ts, 0x00B1)
    assert len(frames) == 1 + (len(body) + 3) // 4
    r = G.GameReassembler()
    got = []
    for fr in frames:
        assert decode(fr)["dst"] == M.CH_WORLD and decode(fr)["frame_type"] == G.FDATA
        got += r.push(fr)
    assert got == [("packet", packet_id, ts, G.GMSG_EVENT, body, G.FLAG_RELIABLE)]
    event_cases.append({"name": f"{M.EVT_NAMES[e['tag']].lower()}_{i}", "event": e,
                        "body": body.hex(), "packet_id": packet_id, "ts_us": ts,
                        "src": 0x00B1, "frames": [x.hex() for x in frames]})
# oversize / malformed bodies must be rejected
for bad in ({"tag": M.EVT_BLOCK_SET, "dim": 0, "x": 0, "y": 0, "z": 0, "id": "x" * 112},
            {"tag": M.EVT_CMD_TRIGGER, "id": 1, "arg": "x" * 121},
            {"tag": 9}):
    try:
        M.event_pack(bad)
        raise AssertionError("accepted a bad event")
    except ValueError:
        pass
for badbody in ("", "01", "02" + "00" * 12 + "05" + "6162", "030001", "03000102ab", "0400", "0401610100", "09"):
    try:
        M.event_unpack(bytes.fromhex(badbody))
        raise AssertionError(f"accepted bad body {badbody}")
    except ValueError:
        pass
ok("events: 12 bodies round-trip, <=124 B, EVENT frames on mc-world; bad ones rejected")

# ── Law E: geometry is deterministic and lanes never collide ──────────────────
origin = (0, 64, 0)
positions = {}
for i in range(M.NIBBLES):
    for kind in ("out", "cmp", "mid", "in"):
        p = M.lane_pos(origin, i, kind)
        assert p not in positions, ("collision", p)
        positions[p] = f"{kind}{i}"
for k in M.CTRL:
    p = M.lane_pos(origin, 0, k)
    assert p not in positions
    positions[p] = k
# pitch-2 lanes: no two IN wires are horizontally adjacent
ins = [M.lane_pos(origin, i, "in") for i in range(M.NIBBLES)]
assert all(b[0] - a[0] == 2 for a, b in zip(ins, ins[1:]))
ok("geometry: 34x4 lane cells + 4 control cells distinct, IN lanes pitch 2")

# ── emit ──────────────────────────────────────────────────────────────────────
golden = M.GOLDEN
vectors = {
    "format": "DCF-Minecraft v1 (a conforming DeModFrame register in a Minecraft world)",
    "spec": "Documentation/DCF_MINECRAFT_SPEC.md",
    "theorem": ("The register is a fixed bit-placement of the certified 17-byte DeModFrame: "
                "nibble i = frame bit-field [4i..4i+3], words are big-endian 3/3/3/3/3/2-byte "
                "slices, and barrel counts follow Minecraft's comparator law "
                "signal = floor(items*14/1728) + (items>0). EVENT bodies are opaque to the "
                "DCF-Game L2 (game_vectors.json is untouched); their tag+field layout is "
                "pinned here. Matching these vectors pins the Java binding, the datapack "
                "generator and the sidecar to this reference."),
    "constants": {"frame_len": M.FRAME_LEN, "nibbles": M.NIBBLES, "words": M.WORDS,
                  "word_bytes": list(M.WORD_BYTES), "barrel_slots": M.BARREL_SLOTS,
                  "barrel_capacity": M.BARREL_CAPACITY, "lane_pitch": M.LANE_PITCH,
                  "max_event": M.MAX_EVENT, "gmsg_event": G.GMSG_EVENT,
                  "flag_reliable": G.FLAG_RELIABLE,
                  "obj_reg": M.OBJ_REG, "obj_ctl": M.OBJ_CTL, "ctl_holders": list(M.CTL_HOLDERS)},
    "anchors": {"crc_123456789": 0x29B1, "crc_zero15": 0x4EC3,
                "golden_frame": golden.hex(),
                "golden_nibbles": M.frame_to_nibbles(golden),
                "golden_words": M.frame_to_words(golden),
                "golden_items": M.frame_to_items(golden)},
    "channels": {M.CH_WORLD_NAME: M.CH_WORLD, M.CH_CHAT_NAME: M.CH_CHAT,
                 "duet": M.channel_id("duet")},
    "signal_table": M.SIGNAL_TABLE,
    "register": [{"frame": f.hex(), "nibbles": M.frame_to_nibbles(f),
                  "words": M.frame_to_words(f), "items": M.frame_to_items(f)} for f in basis],
    "events": event_cases,
    "geometry": {"origin": list(origin), "ctrl": {k: list(v) for k, v in M.CTRL.items()},
                 "lanes": [{"i": i, "out": list(M.lane_pos(origin, i, "out")),
                            "cmp": list(M.lane_pos(origin, i, "cmp")),
                            "mid": list(M.lane_pos(origin, i, "mid")),
                            "in": list(M.lane_pos(origin, i, "in"))} for i in (0, 1, 33)]},
}
out = sys.argv[1] if len(sys.argv) > 1 else "minecraft_vectors.json"
with open(out, "w") as fh:
    json.dump(vectors, fh, indent=1)
    fh.write("\n")
print(f"wrote {out}: {len(basis)} register + {len(event_cases)} event vectors; all laws hold")
