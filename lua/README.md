# DCF-Audio — Lua framework (LGPL, dual-licensed)

A pure-Lua, **self-certifying** implementation of the DCF-Audio L2 framing — the
collaborative-audio adapter over the 17-byte `DeModFrame` wire quantum. Byte-identical
to the C / Rust / Python references (`codec/demod_audio.h`, `codec/src/audio.rs`,
`python/MCP/audiolab_core.py`) and certified against the same golden vectors.

This is the canonical, intentionally-open home of the framework. See
[`LICENSING.md`](LICENSING.md): **LGPL-3.0-only**, with a commercial license available
from DeMoD LLC on request.

## Files

| File | What |
|------|------|
| `dcf_audio.lua` | the framework: DeModFrame codec, L2 `packetize` / `Reassembler`, PCM-diag, PM params, and the **frequency rendezvous** helpers. Self-certifies on load (`M.CERTIFIED`). |
| `dcf_snake.lua` | DCF-Snake L2: the 5:11 mixer plane (32 sources, <=8188 B/message), BEACON grandmaster media clock, `unwrap_pid`, and mixer timeline/skew/PI-servo helpers. Self-certifies on load. |
| `selftest_snake.lua` | golden-vector certification for `dcf_snake.lua` (`lua lua/selftest_snake.lua`, exit 0/1) |
| `dcf_transport.lua` | how a burst of frames becomes datagrams: SuperPack batching + Reed-Solomon, per-link presets (`lan`/`wan`/`rf`/`acoustic`) |
| `dcf_voice.lua` | **L3**: jitter buffer (modular, adaptive), PLC, VAD/DTX, and the L4 voice pipeline. Fully config-driven — see `M.defaults`, `M.presets`, `M.register_codec`. |
| `dcf_history.lua` | persistent chat/call history on [DeMoD StreamDB](https://github.com/ALH477/DeMoD-StreamDB), pluggable backends, configurable key schema + retention |
| `selftest_voice.lua` | L3 + history law certification (`lua lua/selftest_voice.lua`, exit 0/1) |
| `dcf_profile.lua` | end-to-end deployment profiles (`handheld` / `marine` / `expedition` / `studio` / `room` / `bench`), link-budget preflight, and first-contact diagnostics |
| `selftest_profile.lua` | 8 profile laws (`lua lua/selftest_profile.lua`, exit 0/1) |
| `dcf_agent.lua` | LLM agent harness: mention gating, token budget + rolling summary, streaming reply chunker, barge-in, DTX-gated STT. Backends injected as callbacks. **Read-write — never expose as an MCP server.** |
| `selftest_agent.lua` | 12 agent laws (`lua lua/selftest_agent.lua`, exit 0/1) |
| `dcf_talk.lua` | **headless end-to-end demo of the whole chat stack** — text, voice, transport, history, hub. Every number it prints is measured, not modelled. |
| `dcf_jam.lua` | headless CLI demo: stream to a channel, watch a tuned peer receive and a mistuned peer reject |
| `selftest.lua` | golden-vector + channel certification (`lua lua/selftest.lua`, exit 0/1) |

## Frequency rendezvous (handshakeless)

The protocol is handshakeless — there is no connection setup. Peers **pre-agree on a
channel** (the frame `dst` field) and are immediately connected:

- **Numeric channel** — tune a `u16` directly (`packetize(..., dst = channel, ...)`).
- **Passphrase** — `dcf_audio.channel_from_passphrase("basement-jam")` hashes a shared
  word to a `u16` via the certified `crc16`. Same word → same channel.
- A receiver accepts a frame iff `dcf_audio.accepts(frame.dst, my_channel)` — i.e. the
  channel matches, or the frame is broadcast (`0xFFFF`). `Reassembler.new(channel)`
  applies this filter automatically.

```sh
lua lua/dcf_jam.lua --passphrase basement-jam --codec pcm --loss 0.05
lua lua/dcf_jam.lua --freq 1420 --blocks 50
lua lua/selftest.lua
```

```lua
local A = dofile("lua/dcf_audio.lua")
local ch = A.channel_from_passphrase("basement-jam")     -- shared rendezvous channel
local frames = A.packetize(A.CODEC_PCM_DIAG, payload, packet_id, ts_us, my_id, ch, 0)
-- ... send each 17-byte frame over your transport ...
local rx = A.Reassembler.new(ch)                          -- only hears CH `ch` (+ broadcast)
for _, f in ipairs(received) do local pkt = rx:push(f); if pkt then play(pkt) end end
```

## Relationship to the rest of the repo

Audio framing is an adapter over the wire quantum — the emitted frames are ordinary,
CRC-valid `DeModFrame`s and pass the 246-vector wire certificate. See
`Documentation/DCF_AUDIO_SPEC.md` and `Documentation/WIRE_QUANTUM_SPEC.md`. The
TERMINUS `demod-jam` patch (separate repo) vendors a byte-identical copy of
`dcf_audio.lua` under LGPL while keeping its UI under its own license.

## Voice pipeline (L3 + L4)

`dcf_audio.lua` stops at the certified L2 framing. `dcf_voice.lua` adds the layers a
live client needs — dejitter, concealment, silence suppression — and injects both
transport and audio devices as callbacks, so the whole pipeline runs headless:

```lua
local V = dofile("lua/dcf_voice.lua")
local cfg = V.configure("wan", {                  -- preset, then your overrides
  src_id  = 0x00A1,
  channel = V.audio.channel_from_passphrase("basement-jam"),
  jitter  = { target_ms = 60, late_policy = "accept" },
  vad     = { threshold = 0.03, hangover_ms = 300 },
  hooks   = { on_conceal = function(id, run) log("concealed", id, run) end },
})

local voice = V.new(cfg, function(frames) udp_send_one_datagram(frames) end)
voice:capture(mic_block, now_us())                -- 20 ms of floats -> certified frames
voice:receive(frame)                              -- every inbound 17-byte frame
local pkt, why = voice:playout()                  -- "packet" | "conceal" | "silence" | "starved"
```

Notable knobs beyond the obvious: `jitter.resync_ahead_ms` jumps the playout clock
across a DTX silence gap instead of concealing one block at a time across it (a 6 s
gap is ~300 packet ids); `jitter.reasm_max_partial` hard-bounds half-reassembled
packets so a lossy link cannot leak slots; `jitter.late_policy` chooses drop vs
best-effort. Hooks fire on `resync`, `conceal`, `late`, `drop`, `codec_mismatch`, the
talkspurt edges, and every pop.

Presets: `lan`, `wan`, `field`, `studio`. Bring your own codec with
`V.register_codec(id, { encode =, decode =, plc =, block_samples = })` — that is how
Opus binds in once the host provides it; pure Lua ships PCM-diag and Faust-PM.

## History (DeMoD StreamDB)

Chat and call history persists to [DeMoD StreamDB](https://github.com/ALH477/DeMoD-StreamDB),
the same engine the Lisp SDK uses for node state.

**StreamDB's index is a reverse trie: `search(x)` is a SUFFIX match.** The key schema is
therefore built backwards — the field you scan by goes LAST:

```
<seq>@<src>@<channel>      search("@duet") -> a channel
                           search("@00a1@duet") -> one peer in it
```

Two more sharp edges it handles for you: reopening a store **resumes the sequence
counter** from what is already there (restarting at 1 silently overwrites prior
history), and a query whose fields are not a contiguous run from the *end* of the
schema is **refused** rather than quietly degrading to "match everything".

`dcf_history.lua` enforces the key order at config time (a schema with `seq` last is rejected),
and `H.configure` lets you change the fields, separator, padding, serialisation,
retention and hooks. Backends are pluggable; with no native binding present the
`auto` backend falls back to an in-memory store with identical suffix semantics, so
everything is testable without `libstreamdb.so`.

```lua
local H = dofile("lua/dcf_history.lua")
local h = H.open({ path = "chat.streamdb", retention = { max_per_channel = 10000 } })
h:append({ kind = "text", ts_us = ts, src = 0x00A1, channel = "duet", text = msg })
for _, r in ipairs(h:query({ channel = "duet" }, { limit = 50 })) do render(r) end
```

## Try the whole stack

```sh
lua lua/dcf_talk.lua                            # two peers: text + voice
lua lua/dcf_talk.lua --loss 0.15                # drop datagrams  -> PLC conceals
lua lua/dcf_talk.lua --transport rf --corrupt 0.3   # flip bytes  -> FEC repairs
lua lua/dcf_talk.lua --hub 8                    # hub vs full mesh, measured
```

`--loss` and `--corrupt` are deliberately separate, because they exercise different
machinery and conflating them would let the demo flatter itself. FEC cannot recover
an erased datagram; concealment cannot repair one that arrived damaged. At 30%
corruption on the `rf` preset all 60 blocks play with nothing concealed; the same
damage without FEC plays 42 and conceals 18, with SuperPack's joint CRC correctly
rejecting the containers it cannot fix.

## Transport: turning frames into datagrams

`dcf_transport.lua` connects two containers the voice and text paths were not using —
`dcf_superpack.lua` and `dcf_fec.lua` — behind one interface with two orthogonal axes:
**batching** (`none` / `concat` / `superpack`) and **FEC** (off, or Reed-Solomon with
N parity bytes).

Batching is the single biggest link-cost win available. Measured, not modelled
(`dcf_transport.report(40)` — Opus 16 kbps, 40 B per 20 ms block):

| batch | frames | datagrams | bytes / 20 ms | link cost |
|---|---|---|---|---|
| `none` | 11 | 11 | 495 | **198 kbps** |
| `concat` | 11 | 1 | 215 | **86 kbps** |
| `superpack` | 11 | 1 | 205 | **82 kbps** |
| `rf` (superpack + RS-16) | 11 | 1 | 242 | 96.8 kbps |

Identical bytes on the wire quantum — every frame still passes the 246-vector
certificate — but 2.4x less on the link and one syscall instead of eleven.

FEC is what makes the lossy tiers usable: concealment hides damage, Reed-Solomon
*repairs* it. Six corrupted bytes in a burst cost 27 bytes of parity and come back
byte-identical; without it, SuperPack's joint CRC correctly detects the damage and
drops the whole container.

```lua
local V = dofile("lua/dcf_voice.lua")
local voice = V.new(V.configure("field"), function(datagrams)
  for _, d in ipairs(datagrams) do udp:send(d) end     -- strings, ready for sendto
end)
voice:receive_datagram(buf)                            -- unwraps + repairs, then feeds L2
```

Setting `transport` is opt-in: leave it `nil` and `send()` still receives raw frames
exactly as before.

## DCF-Snake — the mixer plane

Where DCF-Audio carries a 20 ms block from one peer (an 11:5 `seq` split, <=124 B),
`dcf_snake.lua` carries a whole quanta QSS commit-hop packet from one of **32
numbered sources** (a 5:11 split, <=8188 B) plus a broadcast grandmaster clock.
That is the shape a hub needs: many sources, one clock.

```lua
local S = dofile("lua/dcf_snake.lua")
local frames = S.packetize(qss_packet, stream_id, ts_us, src, ch, S.MODE_LIVE, S.FLAG_ANCHOR)
local beacon = S.beacon_packetize(S.pack_clock{ gm_sample_count = n }, 0, ts, src, ch)

local tl = S.new_timeline(S.PID_MOD)      -- monotonic index across the rolling wrap
local abs = tl:step(hop_index)
local ppm = S.skew_ppm(my_samples, gm_samples)
local corr, integ = S.pi_servo(ppm, integ)
```

**One reassembler per `dst`.** CTRL(3) carries both DCF-Audio (11:5) and DCF-Snake
(5:11) — the same `seq` bits mean different things — so never multiplex the two on
one channel. `new_reassembler()` accepts CTRL by default; pass `S.FBEACON` for the
clock plane.

**u64 vs Lua's signed integers.** `gm_sample_count` is a `u64`; Lua 5.3+ integers are
64-bit but *signed*, so values at or above 2^63 cannot be written as a literal. The
bit pattern is unaffected — Lua's `>>` is a logical shift, so pack/unpack round-trip
the full u64 range byte-for-byte, and 2^64-1 simply reads back as `-1`. Use
`S.gm_tostring()` / `S.gm_from_decimal()` when comparing against a decimal u64 from
a vector or a Rust log. In practice 2^63-1 samples is ~6.1 million years at 48 kHz.

## Before you plug anything in

```sh
lua lua/dcf_talk.lua --budget                    # does every profile fit its medium?
lua lua/dcf_talk.lua --profile marine --loss 0.2 # run one, then read the diagnostics
```

A profile pairs codec, jitter, DTX, transport and FEC into one named deployment
and **refuses incoherent combinations** — `studio` voice over an `acoustic`
transport is nonsense, and nothing previously stopped you writing it.

`--budget` is the honest preflight. It measures through the real transport, and
it charges IPv4+UDP headers only to media that actually carry them.

### The live-voice floor

DCF-Audio sends a descriptor frame **plus at least one data frame** per 20 ms
block. Even a one-byte codec therefore costs `2 x 17 B x 50 = 13.6 kbps` — before
any codec, FEC or header. No configuration reaches below it, because the
descriptor is what makes reassembly possible.

So a medium slower than ~14 kbps cannot carry a live call at all. It carries text
and async voice notes, and `expedition` says exactly that (`live_voice = false`).
Law 2 checks **every** profile's `live_voice` claim against its own measured
budget, so a profile cannot promise something the arithmetic denies.

### First contact

Two machines that cannot hear each other fail identically for a dozen reasons.
`dcf_profile.diagnose(stats)` maps observed counters onto the cause, most likely
first — and it distinguishes the two that look alike and need opposite fixes:

- **erasure** (datagrams dropped) → concealment hides it; FEC cannot help
- **corruption** (bytes flipped) → FEC repairs it; concealment cannot

Every `dcf_talk` run ends with this read-out.
