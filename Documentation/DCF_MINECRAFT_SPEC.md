# DCF-Minecraft — a conforming DeModFrame register in a Minecraft world

**Status:** v1. Byte-certified where stated (`minecraft_vectors.json`), loopback-tested elsewhere.
**Scope:** command blocks, redstone, a Paper plugin, a Fabric mod and a vanilla Bedrock client
exchanging ordinary 17-byte `DeModFrame`s with any Punctim peer. Not a new wire format: the
246-vector wire certificate and every adapter certificate are untouched.

> The old `MineCraft/` datapack is a redstone *demo* with a non-conforming "RDCF" pattern and no
> input path. This document replaces it as the normative description; the demo stays as history.

## 1. The register

The world holds one `DeModFrame` as **34 nibbles**, high nibble first (nibble 0 = `0xD`, the
sync byte's high nibble; nibble 33 = the CRC's low nibble), on two rows of lanes at pitch 2:

| cell | position (origin `O = (x, y, z)`, lane `i` = 0..33) | role |
|---|---|---|
| OUT `i` | `(x+2i, y, z)` | `barrel[facing=up]`; its item count sets a comparator's strength = nibble `i` |
| CMP `i` | `(x+2i, y, z+1)` | free for the user's comparator reading OUT `i` (the loopback fixture puts one here, `facing=north`) |
| MID `i` | `(x+2i, y, z+2)` | free (the loopback fixture puts a second comparator here so the IN wire reads the nibble losslessly) |
| IN `i` | `(x+2i, y, z+3)` | `redstone_wire` on `smooth_stone`; its `power` (0..15) is nibble `i` |
| STROBE_IN | `(x-2, y, z+3)` | `redstone_wire`; a rising edge latches the IN lanes |
| VALID_OUT | `(x-2, y, z)` | `redstone_block` pulse after a frame was written to the OUT lanes |
| ACK_OUT / NAK_OUT | `(x-4, y, z)` / `(x-6, y, z)` | pulse after a latched frame passed / failed the gate (judged outside the world) |

**Barrel arithmetic (certified, `signal_table`):** a barrel has 27 slots × 64 = **1728** items;
Minecraft's comparator law is `signal = floor(items·14/1728) + (items>0 ? 1 : 0)`, so the smallest
count reading exactly `s` is `[0, 1, 124, 247, 371, 494, 618, 741, 864, 988, 1111, 1235, 1358,
1482, 1605, 1728]` — all 16 nibble values are reachable (15 = a full barrel).

**Scoreboard image:** objective `dcf_reg` holds `n0..n33` (nibbles), `b0..b16` (bytes) and
`w0..w5` (big-endian words of 3, 3, 3, 3, 3 and 2 bytes; `w5` is the CRC; every word < 2³¹).
Objective `dcf_ctl` holds `tx_pending tx_seq rx_seq strobe_prev strobe_now ack valid_ttl ack_ttl nak_ttl`.
The golden frame `d31312340001ffffdeadbeefab12cd24c0` is `[13832978, 3407873, 16777182, 11386607,
11211469, 9408]`.

**Egress is self-announcing:** latching ends with `tellraw @a ["DCF TX ", w0, " ", …, w5]`, so a
single-player world needs no console at all — the line lands in the client's `logs/latest.log`
as `[System] [CHAT] DCF TX …`. Symmetrically `rx_commit` announces `DCF RX …`.

**Validity is judged outside the world** in v1: the sidecar/plugin runs the frame through the
gate (`0xD3`, version nibble, CRC-16/CCITT-FALSE) and answers on ACK_OUT/NAK_OUT. A redstone
builder may drive the CRC lanes by hand and will get the verdict. An in-mcfunction CRC is a
v2 stretch (and the sidecar would still verify).

## 2. The datapack (`minecraft/datapack/gen_datapack.py`)

`python3 minecraft/datapack/gen_datapack.py --out DIR --origin X Y Z [--namespace dcf] [--pack-format 48] [--autotest] [--zip]`

| function | does |
|---|---|
| `load` | objectives, constants `#16 #256`, gamerules, zero unset scores, `[DCF]` banner (`--autotest`: runs `selftest`) |
| `tick` (`#minecraft:tick`) | STROBE_IN edge → `latch` (unless `tx_pending`); pulse TTLs; `ack`=1/2 → `pulse_ack`/`pulse_nak` |
| `build` / `build_loopback` | platform + 34 barrels + 34 IN wires + STROBE_IN; the two-comparator loopback per lane |
| `sample_in` → `pack` (`pack_nibbles`, `pack_words`) | IN wire power → `n_i` → `b_j` → `w_k` (scoreboard arithmetic only) |
| `unpack` (`unpack_words`, `unpack_nibbles`) → `rx_write` | `w_k` → `b_j` → `n_i` → `data merge block <OUT i> {Items:[…]}` |
| `rx_commit` | `unpack` + `rx_write` + VALID pulse + `rx_seq++` + `DCF RX` announce — **the ingress entry point** |
| `latch` / `tx` | sample + pack + `tx_pending=1` + `tx_seq++` + `DCF TX` announce — **the egress entry point** |
| `tx_from_bytes` / `tx_from_words` | a command block wrote `b0..b16` / `w0..w5` itself; announce + `tx_pending` |
| `tx_done` | `tx_pending=0` (the consumer's acknowledgement) |
| `selftest` → `selftest_check` | build + loopback + golden `rx_commit`, 10 ticks later `latch`, then `DCF SELFTEST PASS <hex>` iff `w0..w5` are the golden words |
| `clear`, `clear_words`, `help`, `pulse_ack`, `pulse_nak` | housekeeping |

`python/tests/test_minecraft_datapack.py` lints every lane (16 predicates per IN lane, 16 exact
item counts per OUT lane) and runs the generated `pack`/`unpack` through a scoreboard interpreter
to prove the arithmetic equals `mclab_core` for the golden frame and edge frames.

## 3. Driving it from outside: `punctim mc` and the `mc:` medium (Python-only)

```
mc:rcon=host:port,pass_file=F | mc:fifo=PATH,log=PATH | mc:bot=ARGV | mc:log=PATH,egress=chat
    [,egress=console|chat][,ns=dcf][,poll_hz=4]
```

| console | where | replies |
|---|---|---|
| `rcon=` | any server with `enable-rcon` (password from `pass_file=`/`$MC_RCON_PASSWORD`, never argv) | inline |
| `fifo=` | nixpkgs `services.minecraft-server` stdin FIFO, or a spawned server's stdin pipe | read from `log=` |
| `bot=` | a Mineflayer operator bot (`minecraft/tools/bot`) on a LAN world or a server without RCON | JSON lines |
| `log=` only, `egress=chat` | a Prism single-player world: the `DCF TX` chat line | egress only |

Ingress = `w0..w5` → `function <ns>:rx_commit` (waits for the function's feedback line, so a
log-based console returns only once the world executed it). Egress = poll `tx_pending`, read
`w0..w5`, gate, `ack 1|2`, `tx_done`. `punctim mc --peer host:port/proto|/bare …` bridges the
world to UDP peers with the repo's `Bridge` (flood + dedup); `punctim io --in mc:… --out udp:…`
does one direction at a time. Other `punctim`s exit 3 on `mc:`.

## 4. Modded: `minecraft/` (Paper plugin + Fabric mod over one core)

`minecraft/core` = the certified `java/com/demod/dcf` codec (unmodified: `Frame SuperPack Medium FEC
Game Text McEvent`) + `DcfUdpNode` (one socket, both dialects per peer, 20 ms lone-frame flush,
dedup on the 17 bytes). Paper (`minecraft/paper`, Java 25, `plugin.yml` api 1.21) and Fabric
(`minecraft/fabric`, 1.21.11 / Loader 0.18.4, runs in a single-player integrated server too) share
the behaviour: inbound frames are queued on the UDP thread and applied once per tick on the
main thread; STROBE_IN latches the IN lanes in-process; `/dcf tx HEX34 | frame … | event … | say … |
latch | status` works from command blocks; `mode: native` drives the barrels itself, `mode: datapack`
hands frames to `dcf:rx_commit` and polls `tx_pending` every tick (no console hop).

## 5. Bedrock

* **On the Java server:** nothing extra — Geyser/Floodgate players use the same command blocks
  (their names are `.Gamertag`; never validate against `[A-Za-z0-9_]`).
* **A vanilla Bedrock world, no server:** the client's own `/connect ws://host:19134` opens a
  WebSocket to `punctim mc --bedrock-ws host:19134` (`python/dcf/minecraft/bedrock_ws.py`, stdlib
  RFC 6455, no TLS). We subscribe `PlayerMessage`; a command block running `say DCF <hex34>` (or a
  behaviour pack echoing `DCF TX w0..w5`) becomes a frame; inbound frames become `commandRequest`s
  (`scoreboard players set w0..w5` + `function dcf/rx_commit`) run as the connected player (cheats).
  Not inter-process HTTP: it is the client's protocol crossing the host boundary.

## 6. Events and channels (certified: `events`, `channels`)

Minecraft happenings ride **DCF-Game EVENT** (msg_type 2, `FLAG_RELIABLE`) on `dst = crc16("mc-world")
= 0xD952`; chat rides **DCF-Text** on `dst = crc16("mc-chat") = 0xE624` — never both on one dst. Raw
register frames keep whatever `dst` the builder put in bytes 6..7. EVENT body = tag byte + fields,
big-endian, ≤ 124 B:

| tag | name | body |
|---|---|---|
| 0x01 | REDSTONE | `dim u8, x i32, y i16, z i32, old u8, new u8` |
| 0x02 | BLOCK_SET | `dim u8, x i32, y i16, z i32, id_len u8, id utf8` |
| 0x03 | CMD_TRIGGER | `id u16, arg_len u8, arg utf8` (→ `function <ns>:trigger_<id>`) |
| 0x04 | SCOREBOARD | `obj_len u8, obj, holder_len u8, holder, value i32` |

`dim`: 0 overworld, 1 nether, 2 end, 255 other. 0x00 and 0x05..0xFF reserved.

## 7. What is certified vs tested

| layer | proof |
|---|---|
| register packing, signal table, words, EVENT bodies, channels, geometry | `Documentation/minecraft_vectors.json` — Python (`mclab_core`) and Java (`McEvent`, `MinecraftCertify`) |
| DCF-Game / DCF-Text in Java | `game_vectors.json` / `text_vectors.json` (`GameCertify`, `TextCertify`) |
| datapack arithmetic and lane wiring | `test_minecraft_datapack.py` (lint + scoreboard interpreter) |
| sidecar over RCON / FIFO / chat log, NAK, rotation | `test_minecraft_sidecar.py` (FakeServer) |
| Bedrock WS handshake, events, commands | `test_bedrock_ws.py` (fake client) |
| UDP node, both dialects, dedup | `CoreSelfTest` (loopback) |
| a real world | `minecraft/tools/prism_test.py` (your Prism client), `devserver_test.py` (Oligarchy's Paper stack as you), `real_server_roundtrip.py` (any jar + RCON) — human-run, never CI |

Not certified: comparator refresh timing after `data merge` (loopback-tested; fallback `setblock`),
the Bedrock JSON envelope (the client's), Mineflayer version support (`minecraft-data` lists 1.21.11).
