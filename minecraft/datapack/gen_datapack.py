#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
"""DCF-Minecraft datapack generator — a CONFORMING DeModFrame register in a vanilla world.

Unlike the old MineCraft/ demo (a baked, non-conforming "RDCF" pattern with no input
path), this datapack implements the register defined in Documentation/DCF_MINECRAFT_SPEC.md
and pinned by Documentation/minecraft_vectors.json:

  OUT lanes  34 barrels; a barrel's item count sets its comparator strength = nibble i
  IN lanes   34 redstone-wire cells; the wire's power (0..15) is sampled as nibble i
  STROBE_IN  rising edge latches the IN lanes into scoreboard words w0..w5, sets
             tx_pending and announces "DCF TX w0 .. w5" (self-announcing egress)
  rx_commit  writes scoreboard words w0..w5 out to the barrels and pulses VALID_OUT

A command block, a Paper plugin, a Fabric mod, the `punctim mc` sidecar (over RCON or
the console FIFO) or a Bedrock `/connect` bridge can all drive the same register, because
it is nothing but scoreboard scores + blocks.  CRC-16 is verified OUTSIDE the world (the
sidecar/plugin answers on ACK_OUT / NAK_OUT); an in-mcfunction CRC is a v2 stretch.

    python3 minecraft/datapack/gen_datapack.py --out ./dcf_datapack --origin 0 64 0

Reused from the old generator: the nibble/barrel arithmetic (via python/MCP/mclab_core.py,
with the barrel capacity fixed to 27x64 = 1728 so nibble 15 is reachable) and the
DatapackBuilder / barrel_nbt shape.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "MCP"))
import mclab_core as M  # noqa: E402

DEFAULT_PACK_FORMAT = 48          # 1.21.0; supported range below lets newer clients load it
MAX_PACK_FORMAT = 999
DESCRIPTION = "DCF-Minecraft: a conforming DeModFrame register (Documentation/DCF_MINECRAFT_SPEC.md)"
PULSE_TICKS = 4
STONE = "minecraft:stone"
GOLDEN_WORDS = M.frame_to_words(M.GOLDEN)


# ── NBT / geometry helpers ────────────────────────────────────────────────────
def barrel_items_nbt(items, item_id=STONE):
    """{Items:[...]} for an exact item count spread over 64-stacks (empty list = empty)."""
    entries, slot, remaining = [], 0, items
    while remaining > 0 and slot < M.BARREL_SLOTS:
        stack = min(64, remaining)
        entries.append(f'{{Slot:{slot}b,id:"{item_id}",count:{stack}}}')
        remaining -= stack
        slot += 1
    return "{Items:[" + ",".join(entries) + "]}"


def pos(p):
    return f"{p[0]} {p[1]} {p[2]}"


def score_json(holder, obj=M.OBJ_REG):
    return f'{{"score":{{"name":"{holder}","objective":"{obj}"}}}}'


def announce(prefix, holders):
    # A homogeneous list of compounds: valid JSON (<= 1.21.4) AND valid SNBT text
    # (>= 1.21.5, where a mixed string/compound list no longer parses).
    parts = [f'{{"text":"{prefix}"}}']
    for i, h in enumerate(holders):
        if i:
            parts.append('{"text":" "}')
        parts.append(score_json(h))
    return "tellraw @a [" + ",".join(parts) + "]"


class Datapack:
    def __init__(self, out, ns, origin, pack_format, loopback, autotest):
        self.out, self.ns, self.o = out, ns, origin
        self.pack_format, self.loopback, self.autotest = pack_format, loopback, autotest
        self.fn_dir = os.path.join(out, "data", ns, "function")
        self.tag_dir = os.path.join(out, "data", "minecraft", "tags", "function")
        self.functions = {}

    # ── positions ────────────────────────────────────────────────────────────
    def lane(self, i, kind):
        return M.lane_pos(self.o, i, kind)

    def ctrl(self, key):
        return M.lane_pos(self.o, 0, key)

    # ── functions ────────────────────────────────────────────────────────────
    def f(self, name, lines):
        self.functions[name] = lines

    def build_all(self):
        ns, o = self.ns, self.o
        REG, CTL = M.OBJ_REG, M.OBJ_CTL
        strobe, valid, ack, nak = (self.ctrl(k) for k in ("strobe_in", "valid_out", "ack_out", "nak_out"))

        # load: objectives, constants, gamerules, unset-score init
        L = [f'scoreboard objectives add {REG} dummy "DCF register"',
             f'scoreboard objectives add {CTL} dummy "DCF control"',
             f"scoreboard players set #16 {CTL} 16",
             f"scoreboard players set #256 {CTL} 256",
             f"scoreboard players set origin_x {CTL} {o[0]}",
             f"scoreboard players set origin_y {CTL} {o[1]}",
             f"scoreboard players set origin_z {CTL} {o[2]}"]
        # No gamerule lines: 1.21.11 renamed them to snake_case (command_block_output ...)
        # and a single unparseable line rejects the whole function on that version. The
        # defaults (commandBlockOutput / logAdminCommands / sendCommandFeedback = true) are
        # what the console/log egress relies on; the spec says not to turn them off.
        for h in M.CTL_HOLDERS:
            L.append(f"execute unless score {h} {CTL} = {h} {CTL} run scoreboard players set {h} {CTL} 0")
        for i in range(M.NIBBLES):
            L.append(f"execute unless score n{i} {REG} = n{i} {REG} run scoreboard players set n{i} {REG} 0")
        for j in range(M.FRAME_LEN):
            L.append(f"execute unless score b{j} {REG} = b{j} {REG} run scoreboard players set b{j} {REG} 0")
        for k in range(M.WORDS):
            L.append(f"execute unless score w{k} {REG} = w{k} {REG} run scoreboard players set w{k} {REG} 0")
        L.append(f'tellraw @a {{"text":"[DCF] register datapack loaded (origin {pos(o)}); '
                 f'/function {ns}:build then /function {ns}:help"}}')
        if self.autotest:
            L.append(f"function {ns}:selftest")
        self.f("load", L)

        self.f("help", [
            f'tellraw @a "[DCF] OUT lanes: barrels along x={o[0]}.. at y={o[1]} z={o[2]} (read with comparators)"',
            f'tellraw @a "[DCF] IN lanes: redstone wire at z={o[2] + 3}; STROBE_IN at {pos(strobe)} latches them"',
            f'tellraw @a "[DCF] VALID_OUT {pos(valid)}  ACK_OUT {pos(ack)}  NAK_OUT {pos(nak)}"',
            f'tellraw @a "[DCF] command blocks: /function {ns}:tx | set w0..w5 then /function {ns}:tx_from_words | '
            f'/function {ns}:rx_commit"',
        ])

        # tick: strobe edge, pulse TTLs, ack requests
        T = [f"execute store success score strobe_now {CTL} unless block {pos(strobe)} minecraft:redstone_wire[power=0]",
             f"execute if score strobe_now {CTL} matches 1 if score strobe_prev {CTL} matches 0 "
             f"if score tx_pending {CTL} matches 0 run function {ns}:latch",
             f"scoreboard players operation strobe_prev {CTL} = strobe_now {CTL}"]
        for ttl, cell in (("valid_ttl", valid), ("ack_ttl", ack), ("nak_ttl", nak)):
            T.append(f"execute if score {ttl} {CTL} matches 1.. run scoreboard players remove {ttl} {CTL} 1")
            T.append(f"execute if score {ttl} {CTL} matches 0 if block {pos(cell)} minecraft:redstone_block "
                     f"run setblock {pos(cell)} minecraft:air")
        T.append(f"execute if score ack {CTL} matches 1 run function {ns}:pulse_ack")
        T.append(f"execute if score ack {CTL} matches 2 run function {ns}:pulse_nak")
        self.f("tick", T)

        for name, cell, ttl in (("pulse_ack", ack, "ack_ttl"), ("pulse_nak", nak, "nak_ttl")):
            self.f(name, [f"setblock {pos(cell)} minecraft:redstone_block",
                          f"scoreboard players set {ttl} {CTL} {PULSE_TICKS}",
                          f"scoreboard players set ack {CTL} 0"])

        # sample_in: wire power -> n_i   (16 predicates per lane; 0 is the reset default)
        S = []
        for i in range(M.NIBBLES):
            p = pos(self.lane(i, "in"))
            S.append(f"scoreboard players set n{i} {REG} 0")
            for P in range(1, 16):
                S.append(f"execute if block {p} minecraft:redstone_wire[power={P}] run scoreboard players set n{i} {REG} {P}")
        self.f("sample_in", S)

        # pack_nibbles: b_j = n_2j*16 + n_2j+1 ; pack_words: w_k from bytes (big-endian)
        P = []
        for j in range(M.FRAME_LEN):
            P += [f"scoreboard players operation b{j} {REG} = n{2 * j} {REG}",
                  f"scoreboard players operation b{j} {REG} *= #16 {CTL}",
                  f"scoreboard players operation b{j} {REG} += n{2 * j + 1} {REG}"]
        self.f("pack_nibbles", P)
        W, off = [], 0
        for k, nb in enumerate(M.WORD_BYTES):
            W.append(f"scoreboard players operation w{k} {REG} = b{off} {REG}")
            for j in range(off + 1, off + nb):
                W += [f"scoreboard players operation w{k} {REG} *= #256 {CTL}",
                      f"scoreboard players operation w{k} {REG} += b{j} {REG}"]
            off += nb
        self.f("pack_words", W)
        self.f("pack", [f"function {ns}:pack_nibbles", f"function {ns}:pack_words"])

        # unpack_words: w_k -> bytes ; unpack_nibbles: b_j -> n
        U, off = [], 0
        for k, nb in enumerate(M.WORD_BYTES):
            U.append(f"scoreboard players operation #tmp {REG} = w{k} {REG}")
            for j in range(off + nb - 1, off - 1, -1):
                U += [f"scoreboard players operation b{j} {REG} = #tmp {REG}",
                      f"scoreboard players operation b{j} {REG} %= #256 {CTL}",
                      f"scoreboard players operation #tmp {REG} /= #256 {CTL}"]
            off += nb
        self.f("unpack_words", U)
        N = []
        for j in range(M.FRAME_LEN):
            N += [f"scoreboard players operation n{2 * j} {REG} = b{j} {REG}",
                  f"scoreboard players operation n{2 * j} {REG} /= #16 {CTL}",
                  f"scoreboard players operation n{2 * j + 1} {REG} = b{j} {REG}",
                  f"scoreboard players operation n{2 * j + 1} {REG} %= #16 {CTL}"]
        self.f("unpack_nibbles", N)
        self.f("unpack", [f"function {ns}:unpack_words", f"function {ns}:unpack_nibbles"])

        # rx_write: n_i -> barrel item count (16 cases per lane)
        R = []
        for i in range(M.NIBBLES):
            p = pos(self.lane(i, "out"))
            for P in range(16):
                R.append(f"execute if score n{i} {REG} matches {P} run data merge block {p} "
                         f"{barrel_items_nbt(M.signal_to_items(P))}")
        self.f("rx_write", R)

        words = [f"w{k}" for k in range(M.WORDS)]
        self.f("rx_commit", [f"function {ns}:unpack", f"function {ns}:rx_write",
                             f"setblock {pos(valid)} minecraft:redstone_block",
                             f"scoreboard players set valid_ttl {CTL} {PULSE_TICKS}",
                             f"scoreboard players add rx_seq {CTL} 1",
                             announce("DCF RX ", words)])

        self.f("latch", [f"function {ns}:sample_in", f"function {ns}:pack",
                         f"scoreboard players set tx_pending {CTL} 1",
                         f"scoreboard players add tx_seq {CTL} 1",
                         announce("DCF TX ", words)])
        self.f("tx", [f"function {ns}:latch"])
        self.f("tx_from_bytes", [f"function {ns}:pack_words",
                                 f"scoreboard players set tx_pending {CTL} 1",
                                 f"scoreboard players add tx_seq {CTL} 1",
                                 announce("DCF TX ", words)])
        self.f("tx_from_words", [f"scoreboard players set tx_pending {CTL} 1",
                                 f"scoreboard players add tx_seq {CTL} 1",
                                 announce("DCF TX ", words)])
        self.f("tx_done", [f"scoreboard players set tx_pending {CTL} 0"])

        # build: floor, barrels, IN wires, strobe wire, forceload
        x0, y, z0 = o
        x_lo, x_hi = x0 - 7, x0 + M.LANE_PITCH * (M.NIBBLES - 1) + 1
        B = [f"forceload add {x_lo} {z0 - 1} {x_hi} {z0 + 4}",
             f"fill {x_lo} {y - 1} {z0 - 1} {x_hi} {y - 1} {z0 + 4} minecraft:smooth_stone",
             f"fill {x_lo} {y} {z0 - 1} {x_hi} {y + 1} {z0 + 4} minecraft:air"]
        for i in range(M.NIBBLES):
            B.append(f"setblock {pos(self.lane(i, 'out'))} minecraft:barrel[facing=up]")
            B.append(f"setblock {pos(self.lane(i, 'in'))} minecraft:redstone_wire")
        B.append(f"setblock {pos(strobe)} minecraft:redstone_wire")
        B.append(f'tellraw @a "[DCF] register built at {pos(o)}; /function {ns}:help"')
        self.f("build", B)

        # build_loopback: two comparators per lane chain barrel -> IN wire losslessly
        LB = []
        for i in range(M.NIBBLES):
            LB.append(f"setblock {pos(self.lane(i, 'cmp'))} minecraft:comparator[facing=north]")
            LB.append(f"setblock {pos(self.lane(i, 'mid'))} minecraft:comparator[facing=north]")
        LB.append('tellraw @a "[DCF] loopback fixture built: OUT barrels now drive the IN lanes"')
        self.f("build_loopback", LB)

        C = [f"scoreboard players set {h} {CTL} 0" for h in M.CTL_HOLDERS]
        C += [f"scoreboard players set n{i} {REG} 0" for i in range(M.NIBBLES)]
        C += [f"scoreboard players set b{j} {REG} 0" for j in range(M.FRAME_LEN)]
        C += [f"scoreboard players set w{k} {REG} 0" for k in range(M.WORDS)]
        C += [f"data merge block {pos(self.lane(i, 'out'))} {{Items:[]}}" for i in range(M.NIBBLES)]
        C += [f"setblock {pos(c)} minecraft:air" for c in (valid, ack, nak)]
        self.f("clear", C)

        # selftest: golden frame -> barrels -> comparators -> IN wires -> latch == golden.
        # Two phases: `build` forceloads + places blocks, but in a freshly generated world
        # those chunks are not loaded within the same tick, so writing the register right
        # away would hit air. Stage 2 runs a few ticks later.
        self.f("selftest", [f"function {ns}:build", f"schedule function {ns}:selftest_stage2 5t"])
        ST = [f"function {ns}:build_loopback", f"function {ns}:clear"]
        ST += [f"scoreboard players set w{k} {REG} {GOLDEN_WORDS[k]}" for k in range(M.WORDS)]
        ST += [f"function {ns}:rx_commit", f"schedule function {ns}:selftest_check 10t"]
        self.f("selftest_stage2", ST)
        cond = " ".join(f"if score w{k} {REG} matches {GOLDEN_WORDS[k]}" for k in range(M.WORDS))
        self.f("selftest_check", [
            f"function {ns}:clear_words", f"function {ns}:latch",
            f'execute {cond} run tellraw @a "DCF SELFTEST PASS {M.GOLDEN.hex()}"',
            f'execute unless score w0 {REG} matches {GOLDEN_WORDS[0]} run tellraw @a "DCF SELFTEST FAIL"'])
        # No tx_done here: the latched frame stays pending for whoever consumes the register
        # (plugin poll, sidecar, bot). Clearing it in the same tick would race the consumer.
        self.f("clear_words", [f"scoreboard players set w{k} {REG} 0" for k in range(M.WORDS)])

    # ── write ────────────────────────────────────────────────────────────────
    def write(self):
        if os.path.isdir(self.out):
            shutil.rmtree(self.out)
        os.makedirs(self.fn_dir)
        os.makedirs(self.tag_dir)
        meta = {"pack": {"pack_format": self.pack_format, "description": DESCRIPTION,
                         "supported_formats": {"min_inclusive": self.pack_format,
                                               "max_inclusive": MAX_PACK_FORMAT},
                         "min_format": self.pack_format, "max_format": MAX_PACK_FORMAT}}
        with open(os.path.join(self.out, "pack.mcmeta"), "w") as fh:
            json.dump(meta, fh, indent=2)
            fh.write("\n")
        with open(os.path.join(self.tag_dir, "load.json"), "w") as fh:
            json.dump({"values": [f"{self.ns}:load"]}, fh)
        with open(os.path.join(self.tag_dir, "tick.json"), "w") as fh:
            json.dump({"values": [f"{self.ns}:tick"]}, fh)
        for name, lines in self.functions.items():
            with open(os.path.join(self.fn_dir, f"{name}.mcfunction"), "w") as fh:
                fh.write(f"# DCF-Minecraft {name} — generated; origin {pos(self.o)}\n")
                fh.write("\n".join(lines) + "\n")
        return sorted(self.functions)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="./dcf_datapack", help="datapack directory (replaced)")
    ap.add_argument("--namespace", default="dcf")
    ap.add_argument("--origin", nargs=3, type=int, default=[0, 64, 0], metavar=("X", "Y", "Z"))
    ap.add_argument("--pack-format", type=int, default=DEFAULT_PACK_FORMAT)
    ap.add_argument("--loopback", action="store_true", help="(kept for symmetry; build_loopback is always emitted)")
    ap.add_argument("--autotest", action="store_true", help="run the selftest on every world load")
    ap.add_argument("--zip", action="store_true", help="also write <out>.zip")
    a = ap.parse_args(argv)
    dp = Datapack(a.out, a.namespace, tuple(a.origin), a.pack_format, a.loopback, a.autotest)
    dp.build_all()
    names = dp.write()
    if a.zip:
        shutil.make_archive(a.out, "zip", a.out)
    print(f"wrote {a.out}: {len(names)} functions in namespace {a.namespace!r}, origin {pos(dp.o)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
