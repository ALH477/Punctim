# DCF-Game — Low-Latency Multiplayer over the DeModFrame Wire

**Version 1** · DeMoD LLC · This document is normative. Companion to
[`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) and a sibling of
[`DCF_AUDIO_SPEC.md`](DCF_AUDIO_SPEC.md).

DCF-Game carries real-time multiplayer game traffic — player-state snapshots,
input frames, and discrete events — over the Punctim mesh. It introduces **no
new wire format**: one game message is an **adapter** over the 17-byte
`DeModFrame` quantum, serialised into a short burst of ordinary `DATA` (type 0)
frames. The framing is **message-type-agnostic and byte-deterministic across C,
Rust, and Python**, and is pinned by a finite certificate exactly like the wire
quantum itself.

Audio uses `CTRL` (type 3); game uses `DATA` (type 0), so the two adapters share
the same fragmentation scheme yet never collide on the wire.

## Layered model

| Layer | What | Certified? |
|-------|------|-----------|
| **L1** message-type registry | Pluggable message kinds keyed by `msg_type_id` (SNAPSHOT / INPUT / EVENT / JOIN). Body is opaque to L2. | SNAPSHOT/INPUT/JOIN byte layout: **yes**. EVENT body: no (opaque app bytes). |
| **L2** framing | `packetize` / reassemble: message bytes ⇄ `DeModFrame` `DATA` frames. | **Yes** — `game_vectors.json` |
| **L3** state buffer | Snapshots: latest-state-wins per `src_id` + dead-reckoning. Events: reliable, ordered. | Runtime (unit-tested) |
| **L4** transport + demo | Send frames over the mesh; 2-peer game loopback. | Demo |

The key invariant: **`msg_type_id` lives in the L2 descriptor, so adding message
types never changes the L2 vectors** — and neither do the reliable/ordered flags.

## L2 framing (normative)

One game message → `1 + frag_total` frames. Every frame is a fully valid
`DeModFrame` (version = 1, type = `DATA` = 0) and still passes the 246-vector wire
certificate.

```
 seq (u16)      = packet_id[15:5] (11 bits) | frag_idx[4:0] (5 bits)
 frag_idx == 0  descriptor : payload = [payload_len, frag_total, msg_type_id, flags]
 frag_idx 1..N  data       : payload = bytes[(k-1)*4 .. +4]   (last frame zero-padded)
 frag_total     = ceil(payload_len / 4)
 timestamp_us   = 24-bit send time, identical across a message's frames
 src_id         = player/node id ; dst_id = rendezvous channel (0xFFFF = broadcast to lobby)
```

Bounds (from the 5-bit fragment field):

| Quantity | Value |
|----------|-------|
| Max data fragments | 31 |
| **Max message payload** | **124 bytes / message** |
| Max `packet_id` | 2047 (wraps ≈ a few seconds of ticks) |
| Frames per message | `1 + ceil(payload_len/4)` (1–32) |

**Descriptor flags** (request transport behaviour; they do **not** change the L2
bytes): bit 0 = `RELIABLE` (ask the transport for ARQ retransmit), bit 1 =
`ORDERED` (deliver in `packet_id` order), bit 2 = `END_OF_TICK` (last message of a
simulation tick). Remaining bits reserved.

Because reliable vs. unreliable is only a flag, an unreliable SNAPSHOT and a
reliable EVENT produce **byte-identical L2 framing** — only the flag bit and the
transport path differ. State larger than 124 B must be tiled across multiple
messages (the adapter never re-fragments beyond the 124 B quantum).

**Reassembly.** Buffer by modular `packet_id`; gather the descriptor and all data
fragments; concatenate and truncate to `payload_len`; emit
`(packet_id, timestamp_us, msg_type_id, flags, payload)`. Duplicates are ignored.
Any missing fragment ⇒ the message is **lost** ⇒ L3 decides (snapshots are
dead-reckoned, reliable events are retransmitted by the transport).

## Message-type registry

| id | Name | Body | Reliable default | Byte-certified? |
|----|------|------|------------------|-----------------|
| 0 | SNAPSHOT | 14 B player state (pos/vel/yaw) | no | **Yes** |
| 1 | INPUT | 6 B (tick u32 + buttons u16) | no | **Yes** |
| 2 | EVENT | opaque app bytes (score, hit, chat) | yes | No (opaque) |
| 3 | JOIN | player_id u16 + len-prefixed UTF-8 name | yes | **Yes** |
| 4..255 | *reserved* | — | — | — |

### SNAPSHOT body (msg_type 0) — 14 bytes, Q8.8 fixed-point

```
 byte:  0   2   4   6    8    10   12
 field: x   y   z   vx   vy   vz   yaw
        i16 Q8.8 metres / metres-per-tick (6×2 B)      u16 (0..65535 = 0..2π)
```

Each coordinate is a signed Q8.8 fixed-point value (`round(v·256)` clamped to
i16); `yaw` is a raw `u16`. The **byte layout is certified** (`pack(unpack(b))==b`
for all 14-byte blocks); the float→Q8.8 quantisation of arbitrary positions is a
runtime detail, not certified.

### INPUT body (msg_type 1) — 6 bytes

```
 byte:  0           4
 field: tick (u32)  buttons (u16 bitfield)
```

For server-authoritative variants: a monotonically increasing simulation tick and
a 16-bit button/axis bitfield. Byte layout certified.

### JOIN body (msg_type 3) — variable

```
 byte:  0            2          3..
 field: player_id    name_len   UTF-8 name (name_len bytes, ≤121)
        u16          u8
```

Used for lobby membership. Byte layout certified for the Python and Rust
references (JSON vectors); the fixed-size SNAPSHOT/INPUT bodies are additionally
certified in the dependency-free C test.

## Certification

Mirrors the wire quantum and DCF-Audio: a Python reference emits golden vectors;
C and Rust diff against them byte-for-byte.

```sh
# regenerate + verify the laws, then diff against the committed vectors
python3 python/MCP/gen_game_vectors.py /tmp/game_vectors.json
diff /tmp/game_vectors.json  Documentation/game_vectors.json   # must be empty

cd codec && cargo test --test certify_game                     # Rust
gcc -std=c11 -I codec C_SDK/tests/test_game_certify.c -lm -o /tmp/gc && /tmp/gc   # C
```

`Documentation/game_vectors.json` (with an identical `python/MCP/` copy) holds 7
framing + 4 reassembly + SNAPSHOT/INPUT/JOIN body cases; `codec/game_vectors.gen.h`
is the dependency-free C vector dump. CI runs all three certs and diffs regenerated
vs. committed vectors (`.github/workflows/wire-certify.yml`, job `certify-game`).

## Reference implementations

| Lang | File | Entry points |
|------|------|--------------|
| C | `codec/demod_game.h` | `dcf_game_packetize` / `dcf_game_reasm_push`, `dcf_snapshot_*`, `dcf_input_*`, `dcf_join_*` |
| Rust | `codec/src/game.rs` | `packetize` / `GameReassembler`, `snapshot_pack/unpack`, `input_pack/unpack`, `join_pack/unpack` |
| Python | `python/MCP/gamelab_core.py` | `packetize` / `GameReassembler`, `snapshot_pack/unpack`, `input_pack/unpack`, `join_pack/unpack` |

## Transport, addressing & topology

Game frames ride the existing DCF transport. Peers rendezvous on a **channel**
(the `DeModFrame` `dst_id`): a numeric channel or one derived from a shared room
passphrase via the certified CRC-16 (`channel_from_passphrase`); `dst_id = 0xFFFF`
is the open lobby. A node accepts a frame iff `dst_id == my_channel` or
`dst_id == 0xFFFF`, and multiplexes opponents by `src_id`.

The initial topology is **direct peer-to-peer on a LAN** (mDNS discovery +
`add_peer` over UDP, as DCF-Audio uses). Multi-hop **mesh forwarding** is a future
extension: TTL-bounded flooding (re-broadcast frames whose `dst_id` ≠ self until a
hop budget is exhausted) or a routing table. Either is additive — it does not
change the wire format or this certificate.

## Theorem

L2 framing is a fixed bit-placement adapter over the certified `DeModFrame`.
Matching the framing + reassembly + SNAPSHOT/INPUT/JOIN body vectors pins the C
and Rust implementations to the Python reference on the entire input space.
`msg_type_id` and the reliable/ordered flags are opaque to L2, so the framing
vectors are invariant to message-type and reliability choice — adding message
types or transports never re-opens the certificate.
