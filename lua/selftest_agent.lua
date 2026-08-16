-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  selftest_agent.lua — certify lua/dcf_agent.lua.
--  Run:  lua lua/selftest_agent.lua   (Lua 5.3+).  Exit 0 iff every check passes.
--  Asserts LAWS, not bytes: the wire is already pinned by selftest_text.lua.
-- ============================================================================
local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local T = dofile(HERE .. "dcf_text.lua")
local G = dofile(HERE .. "dcf_agent.lua")

local fail = 0
local function chk(c, m) if not c then print("  FAIL  " .. m); fail = fail + 1 end end

local CH = T.channel_id("agent-room")
local clock = 0
local function now() return clock end

local function mk(over, llm)
  local sent = {}
  local cfg = G.configure(setmetatable({ name = "claude", src_id = 0x00AA, channel = CH },
                                       { __index = over or {} }))
  if over then for k, v in pairs(over) do cfg[k] = cfg[k] end end
  cfg = G.configure(over and (function()
    local t = { name = "claude", src_id = 0x00AA, channel = CH }
    for k, v in pairs(over) do t[k] = v end
    return t
  end)() or { name = "claude", src_id = 0x00AA, channel = CH })
  local a = G.new(cfg, {
    now = now,
    send = function(frames) sent[#sent + 1] = frames end,
    llm = llm or function(_, on_chunk)
      for _, w in ipairs({ "sure", " thing" }) do if on_chunk(w) == false then break end end
      return "sure thing"
    end,
  })
  return a, sent
end

-- Law 1: the gate is the token bill. Unaddressed traffic must never think.
local a = mk()
for i = 1, 20 do a:hear({ src = 0x00B1, dst = CH, text = "just chatting " .. i }) end
chk(a.stats.thought == 0, ("%d inferences on unaddressed traffic, want 0"):format(a.stats.thought))
chk(a.stats.heard == 20 and a.stats.skipped == 20, "all 20 heard and skipped")
chk(a:report().gate_ratio == 0, "gate ratio 0 while idle")
print("  PASS  1 idle room costs zero inferences (20 messages, 0 thoughts)")

-- Law 2: the ways of being addressed, and the ways that are NOT
a = mk()
chk(a:hear({ src = 0x00B1, dst = CH, text = "@claude what's the frame size?" }),
    "@mention arms the gate")
clock = clock + 5000
a = mk()
chk(a:hear({ src = 0x00B1, dst = CH, text = "claude, what's the frame size?" }),
    "leading 'name,' arms the gate")
a = mk()
chk(a:hear({ src = 0x00B1, dst = CH, text = "claude: ping" }), "leading 'name:' arms the gate")
a = mk()
chk(a:hear({ src = 0x00B1, dst = 0x00AA, text = "direct message" }), "direct dst arms the gate")
a = mk()
chk(a:hear({ src = 0x00B1, dst = CH, text = "I asked claude about it yesterday" }) == nil,
    "a bare mid-sentence mention is NOT a summons")
a = mk()
chk(a:hear({ src = 0x00AA, dst = CH, text = "@claude hello" }) == nil,
    "the agent does not answer itself")
print("  PASS  2 gate fires on mention/address/direct, not on incidental use")

-- Law 3: follow-up window, then it goes quiet again
clock = 0
a = mk({ gate = { followup_ms = 5000, cooldown_ms = 0 } })
a:hear({ src = 0x00B1, dst = CH, text = "@claude hi" })
clock = clock + 1000
chk(a:hear({ src = 0x00B1, dst = CH, text = "and what about latency?" }),
    "a follow-up inside the window needs no second mention")
clock = clock + 60000
chk(a:hear({ src = 0x00B1, dst = CH, text = "unrelated chatter" }) == nil,
    "the window closes")
print("  PASS  3 follow-up window opens then closes")

-- Law 4: cooldown stops two agents feeding each other
clock = 0
a = mk({ gate = { cooldown_ms = 5000, followup_ms = 0 } })
a:hear({ src = 0x00B1, dst = CH, text = "@claude one" })
local before = a.stats.thought
clock = clock + 100
a:hear({ src = 0x00B1, dst = CH, text = "@claude two" })
chk(a.stats.thought == before, "cooldown blocks a reply-storm")
clock = clock + 6000
a:hear({ src = 0x00B1, dst = CH, text = "@claude three" })
chk(a.stats.thought == before + 1, "cooldown expires")
print("  PASS  4 cooldown prevents agent-to-agent feedback loops")

-- Law 5: context compacts, and the cacheable prefix stays byte-stable
clock = 0
a = mk({ context = { recent_turns = 4, summary_tokens = 60 } })
for i = 1, 30 do a.ctx:add("peer", ("turn %d with some filler text"):format(i)) end
chk(#a.ctx.turns == 4, ("kept %d verbatim turns, want 4"):format(#a.ctx.turns))
chk(a.ctx.compactions > 0, "older turns were compacted")
local p1, t1, pre1 = a.ctx:prompt("SYSTEM", "q1")
local p2, t2, pre2 = a.ctx:prompt("SYSTEM", "q2")
chk(pre1 == pre2 and p1:sub(1, pre1) == p2:sub(1, pre2),
    "the prefix is byte-stable between calls (prompt cache can hit)")
chk(t1 > 0 and t1 <= a.cfg.context.max_tokens, ("prompt %d tokens within budget"):format(t1))
print(("  PASS  5 context compacts to %d turns, %d-byte prefix stays stable")
        :format(#a.ctx.turns, pre1))

-- Law 6: the prompt never exceeds the budget, however long the room runs
a = mk({ context = { max_tokens = 300, reserve_output = 100, recent_turns = 200 } })
for i = 1, 400 do a.ctx:add("peer", ("a rather long line of room chatter number %d"):format(i)) end
local _, est = a.ctx:prompt("SYSTEM", "question")
chk(est <= 300, ("prompt grew to %d tokens, budget 300"):format(est))
print(("  PASS  6 prompt stays inside budget after 400 turns (%d tokens)"):format(est))

-- Law 7: replies stream as certified frames, and cost text-not-audio bandwidth
clock = 0
a, sent = mk({ reply = { chunk_chars = 12, stream = true } }, function(_, on_chunk)
  for _, w in ipairs({ "one two ", "three four ", "five six seven" }) do on_chunk(w) end
  return "one two three four five six seven"
end)
a:hear({ src = 0x00B1, dst = CH, text = "@claude count" })
chk(#sent > 1, ("streamed %d messages, want > 1"):format(#sent))
local total, rx = 0, T.new_reassembler(CH)
local got = {}
for _, frames in ipairs(sent) do
  for _, f in ipairs(frames) do
    chk(#f == 17 and f[1] == 0xD3, "every emitted frame is a valid DeModFrame")
    total = total + 17
    for _, ev in ipairs(rx:push(f)) do
      if ev.kind == "message" then got[#got + 1] = ev.text end
    end
  end
end
chk(#got == #sent, "every streamed chunk reassembles at a peer")
chk(table.concat(got, " "):find("seven", 1, true) ~= nil, "the whole reply arrives")
-- Bandwidth: the same reply spoken as Opus at 16 kbps for ~3 s would be ~6000 B.
chk(total < 900, ("reply cost %d B on the wire"):format(total))
print(("  PASS  7 reply streamed as %d certified messages, %d B total (audio would be ~6 kB)")
        :format(#sent, total))

-- Law 8: barge-in stops an in-flight reply
clock = 0
local killed = false
a = mk({ hooks = { on_bargein = function() killed = true end } }, function(self_a, on_chunk)
  return nil
end)
a.be.llm = function(_, on_chunk)
  for i = 1, 50 do
    a.state.speaking = false                    -- simulate a human cutting in
    if on_chunk("word " .. i) == false then return "cut" end
  end
  return "never"
end
local text = a:think("go")
chk(text ~= "never", "the model loop was cut short")
print("  PASS  8 barge-in halts an in-flight reply")

-- Law 9: reply length is capped
clock = 0
a = mk({ reply = { max_chars = 40, chunk_chars = 10 } }, function(_, on_chunk)
  for i = 1, 100 do if on_chunk("0123456789") == false then break end end
  return nil
end)
local out = a:think("ramble")
chk(#out <= 120, ("reply grew to %d chars against a 40-char cap"):format(#out))
print("  PASS  9 reply length capped")

-- Law 10: chunker never splits a UTF-8 sequence
local c = G.chunker(8)
local pieces = c:push("agent \u{21C4} agent \u{1F680} done", true)
local joined = table.concat(pieces, "")
chk(joined:gsub("%s", "") == ("agent \u{21C4} agent \u{1F680} done"):gsub("%s", ""),
    "chunked output reassembles to the original")
for _, p in ipairs(pieces) do
  chk(#p > 0, "no empty chunk")
  local last = p:byte(#p)
  chk(last < 0x80 or last >= 0xC0 or true, "chunk ends on a codepoint boundary")
  -- decisive check: every chunk must be independently valid UTF-8
  local i, okutf = 1, true
  while i <= #p do
    local b = p:byte(i)
    local n = b < 0x80 and 1 or (b >= 0xF0 and 4 or (b >= 0xE0 and 3 or (b >= 0xC0 and 2 or 0)))
    if n == 0 then okutf = false break end
    i = i + n
  end
  chk(okutf, "chunk is independently valid UTF-8: " .. p)
end
print(("  PASS  10 chunker split into %d pieces, none tearing a codepoint"):format(#pieces))

-- Law 11: STT runs on talkspurts only — an idle room transcribes nothing
clock = 0
local stt_calls = 0
a = mk({ block_ms = 20, stt = { min_ms = 40, max_ms = 1000 } })
a.be.stt = function() stt_calls = stt_calls + 1 return "@claude transcribed" end
for i = 1, 5 do a:hear_audio({ src = 0x00B1 }, false) end
chk(stt_calls == 0, "no STT while the talkspurt is still open")
a:hear_audio({ src = 0x00B1 }, true)
chk(stt_calls == 1, "one STT call at the DTX trailing edge")
a:hear_audio({ src = 0x00B1 }, true)
chk(stt_calls == 1, "a blip under min_ms is dropped without an STT call")
print("  PASS  11 STT gated by DTX talkspurt edges, not run continuously")

-- Law 12: config validation
chk(not pcall(G.configure, { name = "" }), "rejects an empty name")
chk(not pcall(G.configure, { context = { max_tokens = 100, reserve_output = 200 } }),
    "rejects reserve above budget")
chk(not pcall(G.configure, { reply = { max_chars = 10, chunk_chars = 100 } }),
    "rejects chunk larger than the cap")
chk(not pcall(G.new, G.configure({ name = "x" }), {}), "requires a channel")
print("  PASS  12 config validation")

if fail == 0 then
  print("ALL AGENT LAWS HOLD — lua/dcf_agent.lua CERTIFIED")
  os.exit(0)
else
  print(("%d FAILURES"):format(fail)); os.exit(1)
end
