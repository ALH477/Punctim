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
| `dcf_voice.lua` | **L3**: jitter buffer (modular, adaptive), PLC, VAD/DTX, and the L4 voice pipeline. Fully config-driven — see `M.defaults`, `M.presets`, `M.register_codec`. |
| `dcf_history.lua` | persistent chat/call history on [DeMoD StreamDB](https://github.com/ALH477/DeMoD-StreamDB), pluggable backends, configurable key schema + retention |
| `selftest_voice.lua` | L3 + history law certification (`lua lua/selftest_voice.lua`, exit 0/1) |
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
