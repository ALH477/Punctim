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

-- Law 10: wire-cost model matches DCF_TALK_SPEC.md §2
local opus16 = V.wire_cost(40)
chk(opus16.frames == 11, "Opus 16k = 11 frames/20 ms")
chk(math.abs(opus16.kbps - 86) < 1.5, ("batched Opus16 %.1f kbps, want ~86"):format(opus16.kbps))
local unbatched = V.wire_cost(40, { batch = false })
chk(unbatched.datagrams == 11 and unbatched.kbps > opus16.kbps * 2,
    "one datagram per frame is >2x worse")
chk(V.wire_cost(8).frames == 3, "Faust-PM = 3 frames")
print("  PASS  wire-cost model agrees with DCF_TALK_SPEC.md §2")

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

if fail == 0 then
  print("ALL VOICE + HISTORY LAWS HOLD — dcf_voice.lua / dcf_history.lua CERTIFIED")
  os.exit(0)
else
  print(("%d FAILURES"):format(fail)); os.exit(1)
end
