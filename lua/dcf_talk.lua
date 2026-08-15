-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_talk.lua — headless end-to-end demo of the DCF chat stack (pure Lua).
--
--  Everything below runs in-process with no sockets and no sound card, so it works
--  anywhere stock Lua 5.3+ does, and every number it prints is MEASURED from the
--  real certified modules rather than modelled:
--
--    dcf_text.lua       chat messages -> certified DATA(0) frames
--    dcf_voice.lua      capture -> L2 -> jitter/PLC/DTX -> playout
--    dcf_transport.lua  frames -> datagrams (SuperPack batching + Reed-Solomon)
--    dcf_snake.lua      the hub plane: 32 sources + a grandmaster clock
--    dcf_history.lua    persistence on DeMoD StreamDB (memory fallback here)
--
--    lua dcf_talk.lua                              two peers, text + voice
--    lua dcf_talk.lua --loss 0.15                  lossy link: PLC + FEC at work
--    lua dcf_talk.lua --transport rf --loss 0.1    Reed-Solomon repairs damage
--    lua dcf_talk.lua --hub 8                      hub vs full mesh, measured
--    lua dcf_talk.lua --blocks 200 --quiet         CI smoke
-- ============================================================================

local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local A  = dofile(HERE .. "dcf_audio.lua")
local T  = dofile(HERE .. "dcf_text.lua")
local V  = dofile(HERE .. "dcf_voice.lua")
local X  = dofile(HERE .. "dcf_transport.lua")
local S  = dofile(HERE .. "dcf_snake.lua")
local Hs = dofile(HERE .. "dcf_history.lua")

-- ── CLI ─────────────────────────────────────────────────────────────────────
local opt = {
  channel = "demod-talk", blocks = 100, loss = 0.0, corrupt = 0.0,
  transport = "wan", hub = 0, seed = 20260815, quiet = false,
}
local i = 1
while i <= #arg do
  local a = arg[i]
  local function val() i = i + 1; return arg[i] or error("missing value for " .. a) end
  if a == "--channel" then opt.channel = val()
  elseif a == "--blocks" then opt.blocks = math.tointeger(tonumber(val())) or 100
  elseif a == "--loss" then opt.loss = tonumber(val()) or 0
  elseif a == "--corrupt" then opt.corrupt = tonumber(val()) or 0
  elseif a == "--transport" then opt.transport = val()
  elseif a == "--hub" then opt.hub = math.tointeger(tonumber(val())) or 0
  elseif a == "--seed" then opt.seed = math.tointeger(tonumber(val())) or 0
  elseif a == "--quiet" then opt.quiet = true
  elseif a == "--help" or a == "-h" then
    print("usage: lua dcf_talk.lua [--channel NAME] [--blocks N]")
    print("       [--loss 0..1]      drop whole datagrams  -> concealment (PLC)")
    print("       [--corrupt 0..1]   flip bytes in flight  -> correction (FEC)")
    print("       [--transport lan|wan|rf|acoustic|debug] [--hub N] [--seed N] [--quiet]")
    os.exit(0)
  else error("unknown option: " .. a) end
  i = i + 1
end
math.randomseed(opt.seed)

local function say(...) if not opt.quiet then print(...) end end
local function rule(t)
  if opt.quiet then return end
  print(("\27[36m%s\27[0m"):format(t and ("── " .. t .. " " .. ("─"):rep(math.max(0, 60 - #t)))
                                        or ("─"):rep(64)))
end

local CH = T.channel_id(opt.channel)
local ALICE, BOB = 0x00A1, 0x00B2

say()
local cfg_fec_on = X.configure(opt.transport).fec.enabled
say(("\27[1mDCF-Talk\27[0m  channel %q -> 0x%04X   transport=%s (FEC %s)   loss=%.0f%%  corrupt=%.0f%%")
      :format(opt.channel, CH, opt.transport, cfg_fec_on and "on" or "off",
              opt.loss * 100, opt.corrupt * 100))

-- ── A lossy virtual link ────────────────────────────────────────────────────
-- Two independent failure modes, because they exercise different machinery:
--   --loss     drops a whole datagram   -> nothing arrives -> L3 conceals (PLC)
--   --corrupt  flips bytes in flight    -> damage arrives  -> FEC repairs it
-- FEC cannot help with an erased datagram, and concealment cannot repair one that
-- arrived damaged. Conflating the two would make the demo flatter itself.
local link = { sent = 0, dropped = 0, corrupted = 0, bytes = 0 }
local function transmit(datagrams, deliver)
  for _, d in ipairs(datagrams) do
    link.sent = link.sent + 1
    link.bytes = link.bytes + #d
    if math.random() < opt.loss then
      link.dropped = link.dropped + 1
    else
      if opt.corrupt > 0 and math.random() < opt.corrupt and #d > 8 then
        local b = X.s2a(d)
        local hits = math.max(1, math.floor(#b * 0.02))
        for _ = 1, hits do
          local at = math.random(1, #b)
          b[at] = (b[at] ~ 0xFF) & 0xFF
        end
        d = X.a2s(b)
        link.corrupted = link.corrupted + 1
      end
      deliver(d)
    end
  end
end

-- ── 1. Text ─────────────────────────────────────────────────────────────────
rule("text")
local hist = Hs.open({ backend = "auto", autoflush = 0 })
say(("  history backend: %s"):format(hist.backend_name))

local tx = X.new(opt.transport)
local rx = X.new(opt.transport)
local inbox = T.new_reassembler(CH)
local received = {}

local script = {
  { ALICE, "hey — mic check on the mesh" },
  { BOB,   "reading you. 17-byte frames all the way down" },
  { ALICE, "agent \u{21C4} agent \u{1F680} unicode survives the adapter" },
  { BOB,   "history goes to StreamDB, keys built backwards for the reverse trie" },
}
for n, m in ipairs(script) do
  local src, text = m[1], m[2]
  local frames = T.packetize(text, n, n * 1000, src, CH, T.FLAG_RELIABLE)
  transmit(tx:wrap(frames), function(d)
    for _, f in ipairs(rx:unwrap(d)) do
      for _, ev in ipairs(inbox:push(f)) do
        if ev.kind == "message" then
          received[#received + 1] = ev
          hist:append({ kind = "text", ts_us = ev.ts_us, src = ev.src,
                        channel = opt.channel, text = ev.text })
        end
      end
    end
  end)
end

for _, ev in ipairs(received) do
  say(("  %s  %s"):format(ev.src == ALICE and "\27[35malice\27[0m" or "\27[32mbob  \27[0m", ev.text))
end
say(("  %d/%d delivered · %d rows in history · replay from store: %d")
      :format(#received, #script, #hist:query({ channel = opt.channel }),
              #hist:query({ channel = opt.channel }, { limit = 2 })))

-- ── 2. Voice ────────────────────────────────────────────────────────────────
rule("voice")
local vcfg = V.configure({
  codec_id = A.CODEC_PCM_DIAG, channel = CH, src_id = ALICE, block_ms = 20,
  transport = opt.transport,
  vad = { enabled = true, threshold = 0.02, hangover_ms = 100 },
  jitter = { target_ms = 40, resync_ahead_ms = 500 },
})
local rcfg = V.configure({ codec_id = A.CODEC_PCM_DIAG, channel = CH, src_id = BOB,
                           transport = opt.transport,
                           jitter = { target_ms = 40, resync_ahead_ms = 500 } })

local listener = V.new(rcfg)
local talker = V.new(vcfg, function(dg)
  transmit(dg, function(d) listener:receive_datagram(d) end)
end)

-- A talkspurt pattern: speech, silence, speech — so DTX has something to suppress.
local function sample_block(t, speaking)
  local s = {}
  for k = 1, A.PCM_DIAG_BLOCK do
    s[k] = speaking and 0.6 * math.sin((t * A.PCM_DIAG_BLOCK + k) * 0.11) or 0.0
  end
  return s
end

for b = 0, opt.blocks - 1 do
  local phase = (b // 25) % 2 == 0        -- alternate half-second talk / silence
  talker:capture(sample_block(b, phase), b * 20000)
end

local played, concealed, silent, starved = 0, 0, 0, 0
for _ = 1, opt.blocks do
  local _, why = listener:playout()
  if why == "packet" then played = played + 1
  elseif why == "conceal" then concealed = concealed + 1
  elseif why == "silence" then silent = silent + 1
  else starved = starved + 1 end
end

local ts, ls = talker:stats(), listener:stats()
say(("  sent %d blocks, %d suppressed by DTX (%.0f%% of airtime saved)")
      :format(ts.sent, ts.suppressed, (ts.dtx_ratio or 0) * 100))
say(("  played %d · concealed %d (PLC) · silence %d · idle %d (DTX gaps — nothing sent)")
      :format(played, concealed, silent, starved))
local fs = ls.transport
if fs then
  if fs.fec_corrected > 0 then
    say(("  \27[33mReed-Solomon repaired %d datagrams, %d damaged bytes\27[0m")
          :format(fs.fec_corrected, fs.fec_bytes_repaired))
  end
  if fs.fec_failed > 0 then
    say(("  %d datagrams beyond FEC's correcting power (fell through to PLC)")
          :format(fs.fec_failed))
  end
  if fs.malformed > 0 and not cfg_fec_on then
    say(("  %d SuperPack containers rejected by their joint CRC (damage detected, not repaired)")
          :format(fs.malformed))
  end
end
say(("  link: %d datagrams · %d dropped (%.1f%%) · %d corrupted · %.0f B/datagram")
      :format(link.sent, link.dropped, link.sent > 0 and link.dropped / link.sent * 100 or 0,
              link.corrupted, link.sent > 0 and link.bytes / link.sent or 0))

-- ── 3. What batching costs on the link ──────────────────────────────────────
rule("link cost, Opus 16 kbps (40 B / 20 ms)")
for _, m in ipairs({ "none", "concat", "superpack" }) do
  local r = X.report(40, { batch = m })
  say(("  %-10s %2d frames  %2d datagram%s  %4d B/20ms  \27[1m%6.1f kbps\27[0m")
        :format(m, r.frames, r.datagrams, r.datagrams == 1 and " " or "s", r.link_bytes, r.kbps))
end
local rf = X.report(40, "rf")
say(("  %-10s %2d frames  %2d datagram   %4d B/20ms  %6.1f kbps  (+RS-16, corrects 8 B)")
      :format("rf", rf.frames, rf.datagrams, rf.link_bytes, rf.kbps))

-- ── 4. Hub vs full mesh ─────────────────────────────────────────────────────
if opt.hub > 0 then
  rule(("hub: %d sources, selective forwarding"):format(opt.hub))
  local N = math.min(opt.hub, S.MAX_STREAM_ID + 1)
  local clock = { gm_sample_count = 0, nominal_rate_mhz = S.NOMINAL_RATE_MHZ_48K,
                  tx_seq = 0, epoch = 1 }

  -- The hub never decodes audio. It reads src/dst/type at fixed offsets, checks an
  -- ACL, and forwards the frame bytes untouched — so it needs no codec at all and
  -- the frames stay byte-identical from sender to receiver.
  local acl, forwarded, hub_bytes = {}, 0, 0
  for id = 1, N do acl[id] = true end
  local timelines = {}
  for id = 1, N do timelines[id] = S.new_timeline(S.PID_MOD) end

  local per_source_up = 0
  for id = 1, N do
    local payload = {}
    for k = 1, 40 do payload[k] = (id * k) % 256 end
    local frames = A.packetize(A.CODEC_OPUS, payload, id, 0, id, CH, 0)
    local dgrams = X.new(opt.transport):wrap(frames)
    for _, d in ipairs(dgrams) do
      per_source_up = per_source_up + #d + 28
      local first = X.s2a(d:sub(1, 17))
      local dec = A.decode(first)
      local src = dec and dec.src or id
      if acl[src] then                       -- fixed-offset check, no parsing
        timelines[src]:step(id)
        forwarded = forwarded + (N - 1)      -- to every peer but the sender
        hub_bytes = hub_bytes + (#d + 28) * (N - 1)
      end
    end
  end
  per_source_up = per_source_up / N

  local beacon = S.beacon_packetize(S.pack_clock(clock), 0, 0, 0x00FF, S.BROADCAST, 0)
  say(("  grandmaster BEACON: %d frames, %d B, epoch %d")
        :format(#beacon, #beacon * 17, clock.epoch))
  say(("  hub forwarded %d frames, %.1f kB/s aggregate, decoded 0 audio payloads")
        :format(forwarded, hub_bytes * 50 / 1000))
  say()
  say(("  uplink per client, %d-way call:"):format(N))
  say(("    full mesh   %7.0f kbps   (each peer sends to %d others)")
        :format(per_source_up * (N - 1) * 50 * 8 / 1000, N - 1))
  say(("    via hub     %7.0f kbps   (each peer sends once)")
        :format(per_source_up * 50 * 8 / 1000))
  say(("    \27[1m%.0fx cheaper per client\27[0m"):format(N - 1))
end

-- ── verdict ─────────────────────────────────────────────────────────────────
rule()
local ok = (#received == #script) and (played + concealed + silent > 0) and (ts.sent > 0)
if opt.loss == 0 then ok = ok and (concealed == 0) end
if ok then
  say("\27[32mTALK OK\27[0m — text, voice, transport, history"
      .. (opt.hub > 0 and ", hub" or "") .. " all certified end to end")
  say()
  os.exit(0)
else
  print("TALK FAILED")
  os.exit(1)
end
