# DCF — Lua framework (LGPL, dual-licensed)

Pure-Lua, **self-certifying** implementations of the DCF adapters over the 17-byte
`DeModFrame` wire quantum — **audio** (collaborative audio) and **text** (chat). Byte-identical
to the C / Rust / Python references (`codec/demod_audio.h`, `codec/src/audio.rs`,
`python/MCP/audiolab_core.py`) and certified against the same golden vectors.

This is the canonical, intentionally-open home of the framework. See
[`LICENSING.md`](LICENSING.md): **LGPL-3.0-only**, with a commercial license available
from DeMoD LLC on request.

## Files

| File | What |
|------|------|
| `dcf_audio.lua` | the framework: DeModFrame codec, L2 `packetize` / `Reassembler`, PCM-diag, PM params, and the **frequency rendezvous** helpers. Self-certifies on load (`M.CERTIFIED`). |
| `dcf_text.lua` | DCF-Text L2 `packetize` / `Reassembler` + channel rendezvous. Self-certifies on load (`M.CERTIFIED`). |
| `selftest_text.lua` | golden-vector certification for `dcf_text.lua` (`lua lua/selftest_text.lua`, exit 0/1) |
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
lua lua/selftest_text.lua
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
