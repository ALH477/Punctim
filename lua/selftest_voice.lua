-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  selftest_voice.lua — certify lua/dcf_voice.lua (L3) and lua/dcf_history.lua.
--  Run:  lua lua/selftest_voice.lua   (Lua 5.3+).  Exit 0 iff every check passes.
--
--  L2 framing is pinned by Documentation/audio_vectors.json (lua/selftest.lua).
--  L3 is runtime behaviour, so this asserts LAWS, not bytes: modular ordering,
--  loss concealment, adaptive depth, DTX, and the history key/query contract.
-- ============================================================================
local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local A = dofile(HERE .. "dcf_audio.lua")
local V = dofile(HERE .. "dcf_voice.lua")
local H = dofile(HERE .. "dcf_history.lua")

local fail = 0
local function chk(c, m) if not c then print("  FAIL  " .. m); fail = fail + 1 end end
local function block(n, amp)
  local s = {}
  for i = 1, n do s[i] = (amp or 0.5) * math.sin(i * 0.3) end
  return s
end

-- Law 1: config layering — defaults < preset < overrides, with validation
local c = V.configure("field", { src_id = 0x2A })
chk(c.jitter.target_ms == 120 and c.src_id == 0x2A, "preset+override merge")
chk(V.configure().jitter.target_ms == 40, "defaults intact after merge (no aliasing)")
chk(not pcall(V.configure, { jitter = { min_ms = 100, target_ms = 10 } }), "rejects min>target")
chk(not pcall(V.configure, { jitter = { late_policy = "nope" } }), "rejects bad late_policy")
chk(not pcall(V.configure, "no-such-preset"), "rejects unknown preset")
print("  PASS  config: defaults / presets / overrides / validation")

-- Law 2: modular packet_id ordering across the 2048 wrap
chk(V.mod_diff(1, 2047) > 0, "1 is AFTER 2047 across the wrap")
chk(V.mod_diff(2047, 1) < 0, "2047 is BEFORE 1 across the wrap")
chk(V.mod_diff(5, 5) == 0, "identity")
print("  PASS  modular packet_id ordering (wrap at 2048)")

-- Law 3: clean stream in == same packets out, in order
local cfg = V.configure({ jitter = { target_ms = 40, adaptive = false } })
local j = V.new_jitter(cfg)
for i = 0, 9 do j:push({ packet_id = i, ts_us = i * 20000, codec_id = 1, payload = {} }) end
local got = {}
for _ = 1, 10 do
  local p, why = j:pop()
  if why == "packet" then got[#got + 1] = p.packet_id end
end
chk(#got == 10, ("clean stream popped %d/10"):format(#got))
for i = 1, 10 do chk(got[i] == i - 1, "order at " .. i) end
print("  PASS  clean stream: 10/10 in order")

-- Law 4: reordered arrival still pops in order
j = V.new_jitter(cfg)
for _, i in ipairs({ 3, 1, 0, 4, 2 }) do
  j:push({ packet_id = i, ts_us = 0, codec_id = 1, payload = {} })
end
got = {}
for _ = 1, 5 do
  local p, why = j:pop()
  if why == "packet" then got[#got + 1] = p.packet_id end
end
chk(#got == 5 and got[1] == 0 and got[5] == 4, "reordered arrival reassembles in order")
print("  PASS  reordered arrival pops in sequence")

-- Law 5: a hole is concealed (PLC), not skipped, and the run is bounded
local conceals = 0
cfg = V.configure({ jitter = { target_ms = 40, adaptive = false },
                    plc = { max_consecutive = 2 },
                    hooks = { on_conceal = function() conceals = conceals + 1 end } })
j = V.new_jitter(cfg)
j.plc_state.last = block(120)
for _, i in ipairs({ 0, 4, 5, 6 }) do              -- 1,2,3 missing
  j:push({ packet_id = i, ts_us = 0, codec_id = 1, payload = {} })
end
local kinds = {}
for _ = 1, 7 do local _, why = j:pop(); kinds[#kinds + 1] = why end
chk(kinds[1] == "packet", "first real packet")
chk(kinds[2] == "conceal" and kinds[3] == "conceal", "holes concealed")
chk(kinds[4] == "silence", "conceal run capped -> silence")
chk(conceals == 2, ("on_conceal fired %d times, want 2"):format(conceals))
print("  PASS  loss concealment fires, decays, and is bounded")

-- Law 6: adaptive depth grows on late packets, shrinks when clean
cfg = V.configure({ jitter = { target_ms = 40, max_ms = 200, grow_ms = 20,
                               shrink_ms = 5, shrink_after = 3 } })
j = V.new_jitter(cfg)
j:push({ packet_id = 10, ts_us = 0, codec_id = 1, payload = {} })
j:pop()
local before = j.depth_ms
j:push({ packet_id = 5, ts_us = 0, codec_id = 1, payload = {} })   -- late
chk(j.depth_ms > before, "late packet widened the buffer")
chk(j.stats.late == 1 and j.stats.dropped == 1, "late packet counted and dropped")
print("  PASS  adaptive jitter depth reacts to lateness")

-- Law 7: VAD/DTX suppresses silence, flags the talkspurt edge
local starts, ends = 0, 0
cfg = V.configure({ vad = { threshold = 0.05, hangover_ms = 40 },
                    hooks = { on_talkspurt_start = function() starts = starts + 1 end,
                              on_talkspurt_end   = function() ends = ends + 1 end } })
local vad = V.new_vad(cfg)
local s1 = vad:update(block(120, 0.8))
chk(s1 and starts == 1, "speech opens a talkspurt")
local sent, eot = vad:update(block(120, 0.0))
chk(sent and not eot, "hangover keeps sending")
sent, eot = vad:update(block(120, 0.0))
chk(sent and eot and ends == 1, "trailing edge flags END_TALKSPURT")
chk(not (vad:update(block(120, 0.0))), "silence suppressed after hangover")
print("  PASS  VAD/DTX: talkspurt edges + silence suppression")

-- Law 8: end-to-end voice loop over real certified frames
local wire = {}
cfg = V.configure({ codec_id = A.CODEC_PCM_DIAG, channel = A.channel_from_passphrase("ci"),
                    src_id = 0x00A1, vad = { enabled = false },
                    jitter = { target_ms = 20, adaptive = false } })
local tx = V.new(cfg, function(frames) for _, f in ipairs(frames) do wire[#wire + 1] = f end end)
local rx = V.new(cfg)
for i = 0, 4 do tx:capture(block(A.PCM_DIAG_BLOCK), i * 20000) end
chk(#wire == 5 * (1 + math.ceil(120 / 4)), ("emitted %d frames"):format(#wire))
for _, f in ipairs(wire) do
  chk(#f == 17 and f[1] == 0xD3 and (f[2] >> 4) == 1, "every emitted frame is a valid DeModFrame")
  rx:receive(f)
end
local n = 0
for _ = 1, 5 do local _, why = rx:playout(); if why == "packet" then n = n + 1 end end
chk(n == 5, ("end-to-end recovered %d/5 blocks"):format(n))
chk(tx:stats().sent == 5, "tx stats")
print("  PASS  end-to-end: capture -> certified frames -> jitter -> playout")

-- Law 9: a mistuned peer rejects every frame (rendezvous still holds at L3)
local off = V.new(V.configure({ channel = A.channel_from_passphrase("other-room"),
                                codec_id = A.CODEC_PCM_DIAG }))
for _, f in ipairs(wire) do off:receive(f) end
chk(off:stats().rejected == #wire, "mistuned peer rejected every frame")
print("  PASS  mistuned peer rejects the whole stream")

-- Law 10: wire-cost model matches DCF_AUDIO_SPEC.md "L2 framing" (1 + ceil(len/4) frames)
local opus16 = V.wire_cost(40)
chk(opus16.frames == 11, "Opus 16k = 11 frames/20 ms")
chk(math.abs(opus16.kbps - 86) < 1.5, ("batched Opus16 %.1f kbps, want ~86"):format(opus16.kbps))
local unbatched = V.wire_cost(40, { batch = false })
chk(unbatched.datagrams == 11 and unbatched.kbps > opus16.kbps * 2,
    "one datagram per frame is >2x worse")
chk(V.wire_cost(8).frames == 3, "Faust-PM = 3 frames")
print("  PASS  wire-cost model agrees with DCF_AUDIO_SPEC.md L2 framing")

-- Law 11: history — suffix key schema, query by channel and by peer
local h = H.open({ backend = "memory", autoflush = 0 })
chk(h.backend_name == "memory", "explicit backend honoured")
for i = 1, 6 do
  h:append({ kind = "text", ts_us = i * 1000, src = (i % 2 == 0) and 0x00A1 or 0x00B2,
             channel = "duet", text = "msg " .. i })
end
h:append({ kind = "text", ts_us = 99, src = 0x00A1, channel = "other", text = "elsewhere" })
chk(#h:query({ channel = "duet" }) == 6, "channel query returns its 6 rows")
chk(#h:query({ channel = "other" }) == 1, "other channel isolated")
chk(#h:query({ channel = "duet", src = 0x00A1 }) == 3, "peer-within-channel query")
local rows = h:query({ channel = "duet" })
chk(rows[1].text == "msg 1" and rows[6].text == "msg 6", "rows come back in order")
chk(rows[1].ts_us == 1000 and type(rows[1].ts_us) == "number", "numeric fields round-trip")
chk(#h:query({ channel = "duet" }, { limit = 2 }) == 2, "limit takes the newest N")
chk(h:query({ channel = "duet" }, { limit = 2 })[2].text == "msg 6", "limit keeps newest")
print("  PASS  history: suffix schema, channel/peer queries, ordering, limit")

-- Law 12: the schema guard catches the reverse-trie footgun
chk(not pcall(H.configure, { schema = { fields = { "channel", "src", "seq" } } }),
    "putting seq last must be rejected (kills channel queries)")
print("  PASS  schema guard rejects a prefix-style key layout")

-- Law 13: retention prunes oldest and never empties the last row
h = H.open({ backend = "memory", autoflush = 0,
             retention = { max_per_channel = 3, keep_minimum = 1 } })
for i = 1, 8 do h:append({ ts_us = i, src = 1, channel = "c", text = "m" .. i }) end
rows = h:query({ channel = "c" })
chk(#rows == 3, ("retention kept %d rows, want 3"):format(#rows))
chk(rows[#rows].text == "m8", "newest survives pruning")
chk(rows[1].text == "m6", "oldest pruned first")
print("  PASS  retention prunes oldest, keeps newest, never empties")

-- Law 14: unicode + serialisation round-trip
h = H.open({ backend = "memory", autoflush = 0 })
local msg = "agent \u{21C4} agent \u{1F680} ;: weird"
h:append({ kind = "text", ts_us = 1, src = 0x00A1, channel = "u", text = msg })
chk(h:query({ channel = "u" })[1].text == msg, "unicode + delimiters survive encode/decode")
print("  PASS  record serialisation round-trips unicode and delimiters")

-- Law 15: auto backend degrades to memory with no native binding present
h = H.open({ backend = "auto", autoflush = 0 })
chk(h.backend_name == "memory", "auto fell back to memory with no StreamDB binding")
h:append({ ts_us = 1, src = 1, channel = "z", text = "ok" })
chk(#h:query({ channel = "z" }) == 1, "fallback store is fully functional")
h:close()
print("  PASS  auto backend degrades cleanly without libstreamdb")

-- ── Regression laws (each pins a bug found in the polish pass) ──────────────

-- R1: DTX leaves a gap in packet_id; the clock must jump it, not grind across it
local resyncs = 0
cfg = V.configure({ jitter = { target_ms = 20, adaptive = false, resync_ahead_ms = 500 },
                    hooks = { on_resync = function() resyncs = resyncs + 1 end } })
j = V.new_jitter(cfg)
j:push({ packet_id = 0, ts_us = 0, codec_id = 1, payload = {} }); j:pop()
j:push({ packet_id = 300, ts_us = 0, codec_id = 1, payload = {} })   -- 6 s of silence
local pops = 0
for _ = 1, 400 do
  pops = pops + 1
  local _, why = j:pop()
  if why == "packet" then break end
end
chk(pops == 1, ("crossing a DTX gap took %d pops, want 1"):format(pops))
chk(resyncs == 1, "resync hook fired once")
chk(j.stats.resyncs == 1 and j.stats.resync_blocks_skipped == 299,
    ("skipped %d blocks, want 299 (playout had already advanced past 0)")
      :format(j.stats.resync_blocks_skipped))
-- and a SHORT gap must still be concealed, not resynced away
j = V.new_jitter(V.configure({ jitter = { target_ms = 20, adaptive = false,
                                          resync_ahead_ms = 500 } }))
j.plc_state.last = block(120)
j:push({ packet_id = 0, ts_us = 0, codec_id = 1, payload = {} }); j:pop()
j:push({ packet_id = 2, ts_us = 0, codec_id = 1, payload = {} })
local _, why2 = j:pop()
chk(why2 == "conceal", "a one-block hole is still concealed, not resynced")
print("  PASS  R1 DTX gap resync (jumps long gaps, conceals short ones)")

-- R2: a raw overrides table handed to new()/new_jitter() must be built, not trusted
local v2 = V.new({ jitter = { target_ms = 100 } })
chk(v2.cfg.codec_id ~= nil and v2.cfg.hooks ~= nil and v2.cfg.jitter.target_ms == 100,
    "raw overrides table is normalised through configure()")
chk(V.new_jitter({ jitter = { target_ms = 80 } }).cfg.block_ms == 20,
    "new_jitter normalises a raw table too")
chk(V.new_jitter(V.configure()).cfg.__dcf_voice_config, "a built config passes through")
print("  PASS  R2 raw config tables are normalised, not silently half-initialised")

-- R3: Faust-PM concealment decays in the PARAMS slot and compounds
cfg = V.configure({ codec_id = A.CODEC_FAUST_PM, plc = { fade = 0.5, max_consecutive = 5 },
                    jitter = { target_ms = 20, adaptive = false, resync_ahead_ms = 0 } })
j = V.new_jitter(cfg)
local pm = { f0 = 440, amp = 200, mod_index = 8, mod_ratio = 2,
             bright = 100, env = 200, flags = 0 }
j:push({ packet_id = 0, ts_us = 0, codec_id = 2, payload = A.pm_pack(pm) })
j:push({ packet_id = 4, ts_us = 0, codec_id = 2, payload = A.pm_pack(pm) })
j:pop()
local amps = {}
for _ = 1, 3 do
  local o, k = j:pop()
  if k == "conceal" and type(o) == "table" then amps[#amps + 1] = o.amp end
end
chk(#amps == 3, ("PM concealed %d blocks, want 3"):format(#amps))
chk(amps[1] == 100 and amps[2] == 50 and amps[3] == 25, "PM amplitude decays and compounds")
chk(j.plc_state.last == nil, "PM never writes params into the sample slot")
print("  PASS  R3 Faust-PM conceal decays in the params slot (no type confusion)")

-- R4: partial reassembly is hard-bounded, oldest evicted first
local vo = V.new(V.configure({ codec_id = A.CODEC_PCM_DIAG, channel = A.BROADCAST,
                               jitter = { reasm_max_partial = 64 } }))
for i = 0, 499 do
  vo:receive(A.packetize(1, { 1, 2, 3, 4 }, i % 2048, 0, 1, A.BROADCAST, 0)[1])
end
local partial = vo:stats().reasm_partial
chk(partial == 64, ("partial slots %d, want cap 64"):format(partial))
chk(vo.jitter.stats.dropped == 436, "evictions counted as drops")
chk(vo.reasm.slots[499] ~= nil and vo.reasm.slots[0] == nil, "newest kept, oldest evicted")
vo:reset()
chk(vo:stats().reasm_partial == 0, "reset() clears receive state")
print("  PASS  R4 orphaned reassembler slots are bounded, oldest-first")

-- R5: decode follows the PACKET's codec, and a mismatch is reported
local mism = 0
cfg = V.configure({ codec_id = A.CODEC_PCM_DIAG,
                    hooks = { on_codec_mismatch = function() mism = mism + 1 end } })
j = V.new_jitter(cfg)
j:push({ packet_id = 0, ts_us = 0, codec_id = A.CODEC_FAUST_PM, payload = A.pm_pack(pm) })
local pkt = j:pop()
chk(mism == 1 and j.stats.codec_mismatch == 1, "codec mismatch counted and hooked")
chk(type(pkt.samples) == "table" and pkt.samples.f0 == 440,
    "payload decoded as PM (the packet's codec), not as PCM samples")
print("  PASS  R5 decode follows the packet's codec; mismatch is surfaced")

-- R6: reopening a store resumes the sequence instead of overwriting history
local shared = {}
H.register_backend("shared", {
  open = function() return shared end,
  insert = function(h, k, v) if h[k] == nil then h[#h + 1] = k end h[k] = v return true end,
  get = function(h, k) return h[k] end,
  delete = function(h, k) h[k] = nil return true end,
  search = function(h, sfx)
    local o = {}
    for _, k in ipairs(h) do
      if h[k] and (sfx == "" or k:sub(-#sfx) == sfx) then o[#o + 1] = { k, h[k] } end
    end
    return o
  end,
  flush = function() return true end, close = function() end,
})
local resumed = 0
local h1 = H.open({ backend = "shared", autoflush = 0 })
h1:append({ ts_us = 1, src = 1, channel = "c", text = "s1-a" })
h1:append({ ts_us = 2, src = 1, channel = "c", text = "s1-b" })
local h2 = H.open({ backend = "shared", autoflush = 0,
                    hooks = { on_resume = function() resumed = resumed + 1 end } })
h2:append({ ts_us = 3, src = 1, channel = "c", text = "s2-a" })
rows = h2:query({ channel = "c" })
chk(#rows == 3, ("reopen+append kept %d rows, want 3 (was overwriting)"):format(#rows))
chk(rows[1].text == "s1-a" and rows[3].text == "s2-a", "prior session survived")
chk(resumed == 1 and h2.seq == 3, "sequence resumed from the store")
print("  PASS  R6 reopening a store resumes seq (no silent history loss)")

-- R7: a query that isn't a key suffix must be refused, not silently widened
h = H.open({ backend = "memory", autoflush = 0 })
h:append({ ts_us = 1, src = 0x00A1, channel = "a", text = "in-a" })
h:append({ ts_us = 2, src = 0x00B2, channel = "b", text = "in-b" })
chk(not pcall(function() return h:query({ src = 0x00A1 }) end),
    "query by src alone must be refused (a reverse trie cannot answer it)")
chk(#h:query({ channel = "a" }) == 1, "suffix query still works")
chk(#h:query({ channel = "a", src = 0x00A1 }) == 1, "contiguous suffix run works")
chk(#h:query({}) == 2, "unconstrained query returns everything")
print("  PASS  R7 non-suffix queries refuse instead of matching everything")

-- R8: schema guards catch both key-order footguns
chk(not pcall(H.configure, { schema = { fields = { "channel", "src", "seq" } } }),
    "seq last is rejected")
chk(not pcall(H.configure, { schema = { fields = { "channel", "seq", "src" } } }),
    "seq must be first (resume parses it from the head of the key)")
print("  PASS  R8 schema guards on both ends of the key")

-- R9: clear() honours keep_minimum
h = H.open({ backend = "memory", autoflush = 0 })
for i = 1, 5 do h:append({ ts_us = i, src = 1, channel = "c", text = "m" .. i }) end
h:append({ ts_us = 9, src = 1, channel = "d", text = "other" })
chk(h:clear("c") == 5 and #h:query({ channel = "c" }) == 0, "clear() empties one channel")
chk(#h:query({ channel = "d" }) == 1, "clear() leaves other channels alone")
print("  PASS  R9 clear() scoped to a channel")

-- R10: retention is exact by default; throttling overshoots by a bounded amount
h = H.open({ backend = "memory", autoflush = 0, retention = { max_per_channel = 3 } })
for i = 1, 10 do h:append({ ts_us = i, src = 1, channel = "c", text = "m" .. i }) end
chk(#h:query({ channel = "c" }) == 3, "default retention holds the cap exactly")
h = H.open({ backend = "memory", autoflush = 0,
             retention = { max_per_channel = 3, check_every = 4 } })
for i = 1, 10 do h:append({ ts_us = i, src = 1, channel = "c", text = "m" .. i }) end
local n10 = #h:query({ channel = "c" })
chk(n10 >= 3 and n10 <= 3 + 3, ("throttled retention held %d rows, want 3..6"):format(n10))
print("  PASS  R10 retention exact by default, bounded overshoot when throttled")

-- ── Transport integration (SuperPack + FEC) ─────────────────────────────────
local X = dofile(HERE .. "dcf_transport.lua")

-- T1: every batch mode is lossless end to end
for _, mode in ipairs({ "none", "concat", "superpack" }) do
  local t = X.new({ batch = mode })
  local payload = {}
  for i = 1, 40 do payload[i] = i end
  local fr = A.packetize(0, payload, 1, 0, 0x11, 0x22, 0)
  local dg = t:wrap(fr)
  local back = {}
  for _, d in ipairs(dg) do
    for _, f in ipairs(t:unwrap(d)) do back[#back + 1] = f end
  end
  chk(#back == #fr, ("%s: %d frames back, want %d"):format(mode, #back, #fr))
  for i = 1, #fr do
    for k = 1, 17 do
      chk(back[i] and back[i][k] == fr[i][k], ("%s: frame %d byte %d"):format(mode, i, k))
    end
  end
  chk(mode ~= "none" or #dg == #fr, "none = one datagram per frame")
  chk(mode == "none" or #dg == 1, mode .. " = one datagram for the burst")
end
print("  PASS  T1 transport round-trips every batch mode losslessly")

-- T2: batching actually delivers the link saving the spec claims
local none = X.report(40, { batch = "none" })
local concat = X.report(40, { batch = "concat" })
local sp = X.report(40, { batch = "superpack" })
chk(none.datagrams == 11 and concat.datagrams == 1, "datagram counts")
chk(math.abs(none.kbps - 198) < 1, ("none %.1f kbps, want ~198"):format(none.kbps))
chk(math.abs(concat.kbps - 86) < 1, ("concat %.1f kbps, want ~86"):format(concat.kbps))
chk(sp.kbps < concat.kbps, "superpack beats plain concat on bytes")
chk(sp.link_bytes == concat.link_bytes - 10, "superpack saves 2 B per pair (5 pairs)")
print(("  PASS  T2 measured link cost: %.0f -> %.0f -> %.0f kbps (none/concat/superpack)")
        :format(none.kbps, concat.kbps, sp.kbps))

-- T3: FEC corrects real in-flight corruption instead of concealing it
local t = X.new("rf")
local payload = {}
for i = 1, 40 do payload[i] = i end
local fr = A.packetize(0, payload, 1, 0, 0x11, 0x22, 0)
local dg = t:wrap(fr)
chk(#dg == 1, "rf preset still batches to one datagram")
local b = X.s2a(dg[1])
for _, i in ipairs({ 20, 21, 22, 23, 24, 25 }) do b[i] = (b[i] ~ 0xFF) & 0xFF end
local back, err = t:unwrap(X.a2s(b))
chk(err == nil and #back == #fr, ("FEC recovered %d/%d frames"):format(#back, #fr))
local intact = true
for i = 1, #fr do
  for k = 1, 17 do if back[i][k] ~= fr[i][k] then intact = false end end
end
chk(intact, "recovered frames are byte-identical after 6 corrupted bytes")
chk(t.stats.fec_corrected == 1, "correction was counted")
-- and the same corruption WITHOUT FEC must not silently pass
local raw = X.new({ batch = "superpack" })
local rdg = raw:wrap(fr)
local rb = X.s2a(rdg[1])
for _, i in ipairs({ 20, 21, 22, 23, 24, 25 }) do rb[i] = (rb[i] ~ 0xFF) & 0xFF end
local rback = raw:unwrap(X.a2s(rb))
-- SuperPack's joint CRC catches the damage and rejects the container, so the
-- frames inside are simply lost. That is the whole point: without FEC the damage
-- is DETECTED and dropped; with FEC it is CORRECTED and delivered.
chk(#rback < #fr, ("without FEC the same damage lost frames: %d/%d"):format(#rback, #fr))
chk(raw.stats.malformed > 0, "damage was detected, not silently accepted")
for _, f in ipairs(rback) do
  chk(A.decode(f) ~= nil, "no corrupted frame slipped through undetected")
end
print("  PASS  T3 Reed-Solomon corrects damage that otherwise fails CRC")

-- T4: transport config validation
chk(not pcall(X.configure, { batch = "nope" }), "rejects unknown batch mode")
chk(not pcall(X.configure, { fec = { parity = 17 } }), "rejects odd parity")
chk(not pcall(X.configure, { mtu = 4 }), "rejects an MTU below one frame")
chk(not pcall(X.configure, "no-such-preset"), "rejects unknown preset")
chk(X.configure("acoustic").fec.parity == 32, "acoustic preset carries heavy FEC")
print("  PASS  T4 transport config validation")

-- T5: MTU splits a burst instead of emitting an oversized datagram
t = X.new({ batch = "concat", mtu = 64 })
dg = t:wrap(fr)
chk(#dg > 1, "burst split across datagrams under a small MTU")
for _, d in ipairs(dg) do chk(#d <= 64, ("datagram %d B exceeds MTU"):format(#d)) end
local back2 = {}
for _, d in ipairs(dg) do
  for _, f in ipairs(t:unwrap(d)) do back2[#back2 + 1] = f end
end
chk(#back2 == #fr, "split burst still reassembles completely")
print("  PASS  T5 MTU splitting stays lossless")

-- T6: Voice uses the transport end to end, and stays backward compatible
local dgrams = {}
cfg = V.configure({ codec_id = A.CODEC_PCM_DIAG, channel = A.BROADCAST, src_id = 0x00A1,
                    transport = "wan", vad = { enabled = false },
                    jitter = { target_ms = 20, adaptive = false } })
local tx2 = V.new(cfg, function(d) for _, x in ipairs(d) do dgrams[#dgrams + 1] = x end end)
local rx2 = V.new(cfg)
for i = 0, 4 do tx2:capture(block(A.PCM_DIAG_BLOCK), i * 20000) end
chk(#dgrams == 5, ("5 blocks -> %d datagrams, want 5"):format(#dgrams))
chk(type(dgrams[1]) == "string", "send() receives datagrams when a transport is set")
local nf = 0
for _, d in ipairs(dgrams) do nf = nf + rx2:receive_datagram(d) end
chk(nf == 5 * 31, ("recovered %d frames, want %d"):format(nf, 5 * 31))
local got5 = 0
for _ = 1, 5 do local _, w = rx2:playout(); if w == "packet" then got5 = got5 + 1 end end
chk(got5 == 5, ("end-to-end over transport recovered %d/5 blocks"):format(got5))
chk(tx2:stats().transport.superpacked == 5 * 15, "superpack pairing counted")
-- no transport configured => raw frames, exactly as before
local rawv = V.new(V.configure({ codec_id = A.CODEC_PCM_DIAG }))
chk(rawv.transport == nil, "transport is opt-in")
chk(not pcall(function() return rawv:receive_datagram("x") end),
    "receive_datagram refuses without a transport")
print("  PASS  T6 Voice sends/receives datagrams; raw-frame path unchanged")

-- T7: fec_corrected must count REAL corrections, not every successful decode.
-- The FEC blob always differs from the message it carries (it has parity
-- attached), so comparing decoded output to the blob reports a correction every
-- single time -- the demo printed "repaired 111 datagrams" on a clean link.
t = X.new("rf")
local clean = t:wrap(fr)
for _, d in ipairs(clean) do t:unwrap(d) end
chk(t.stats.fec_corrected == 0,
    ("clean link reported %d corrections, want 0"):format(t.stats.fec_corrected))
chk(t.stats.fec_bytes_repaired == 0, "no bytes repaired on a clean link")
-- Corrupt the BODY, not the RS-protected header: decode_message accumulates its
-- correction count from the body codewords only, so a header-only repair is
-- silently fixed but not counted. Worth knowing before trusting the number.
local damaged = X.s2a(clean[1])
for _, at in ipairs({ 30, 31, 32, 33 }) do damaged[at] = (damaged[at] ~ 0xFF) & 0xFF end
local rec = t:unwrap(X.a2s(damaged))
chk(t.stats.fec_corrected == 1, "a damaged datagram reports exactly one correction")
chk(t.stats.fec_bytes_repaired >= 4,
    ("repaired %d bytes, want >= 4"):format(t.stats.fec_bytes_repaired))
chk(#rec == #fr, ("recovered %d/%d frames from the repaired datagram"):format(#rec, #fr))
print("  PASS  T7 FEC correction stats count real repairs only")

if fail == 0 then
  print("ALL VOICE + HISTORY LAWS HOLD — dcf_voice.lua / dcf_history.lua CERTIFIED")
  os.exit(0)
else
  print(("%d FAILURES"):format(fail)); os.exit(1)
end
