-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_agent.lua — LLM agent harness for DCF-Talk rooms (pure Lua).
--
--  An agent is an ordinary mesh peer. It reassembles the same certified frames
--  every other peer does; nothing about the wire changes because a model is on
--  the far end. What this module adds is the part that is easy to get expensive:
--  deciding WHEN to think, HOW MUCH context to carry, and WHAT to put back on
--  the wire.
--
--  ── The three costs, and the three decisions that dominate them ─────────────
--
--  1. INFERENCE — gated, not continuous. Presence in a room is free: frames
--     reassemble whether or not a model runs. So the LLM is invoked only when
--     the agent is ADDRESSED (mention, wake phrase, or a direct channel).
--     Sitting in a busy room all day costs zero tokens until someone says the
--     name. This is the single largest lever and it is a policy, not an
--     optimisation.
--
--  2. CONTEXT — a stable prefix plus a short tail. System prompt and rolling
--     summary are held byte-stable so a prompt cache actually hits; only the
--     recent turns change. Older turns compact into the summary rather than
--     being re-sent verbatim forever.
--
--  3. BANDWIDTH — the agent answers in TEXT, and each listener speaks it with
--     its own local TTS. A spoken reply as Opus is ~86 kbps; the same reply as
--     DCF-Text is a few hundred bytes total. Synthesising centrally and
--     shipping audio would cost roughly three orders of magnitude more for a
--     worse result (every listener wants their own voice, rate and volume
--     anyway). Audio egress exists as an option for clients that cannot
--     synthesise, and is off by default.
--
--  Latency is first-token, not total: replies stream out in DCF-Text messages
--  as the model produces them, and a human speaking cancels the rest (barge-in).
--
--  STT/TTS/LLM are injected as callbacks, so this runs headless in tests with no
--  model, no microphone and no network — same discipline as dcf_voice.lua.
--
--  READ-WRITE BY NATURE. This harness speaks into rooms, so it must never be
--  exposed as an MCP server: that surface is read-only and dry-run by
--  construction. See modules/demod-talk/README.md.
-- ============================================================================

local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local T = dofile(HERE .. "dcf_text.lua")

local M = { text = T }

-- ── Configuration ───────────────────────────────────────────────────────────
M.defaults = {
  name       = "claude",          -- what the room calls this agent
  src_id     = 0x00AA,
  channel    = nil,               -- set from dcf_text.channel_id(room)

  gate = {
    -- The agent thinks only when one of these fires. Everything else is
    -- observed, appended to context, and costs nothing.
    mentions    = true,           -- "@name" or "name," at the start of a line
    wake        = {},             -- extra wake phrases, lowercased substrings
    direct      = true,           -- any message whose dst is our own src_id
    followup_ms = 8000,           -- after replying, stay armed this long so a
                                  -- natural follow-up needs no second mention
    ignore_self = true,
    cooldown_ms = 750,            -- floor between replies; stops feedback loops
                                  -- when two agents share a room
  },

  context = {
    max_tokens     = 1400,        -- total budget for the assembled prompt
    reserve_output = 220,         -- held back for the reply
    recent_turns   = 12,          -- kept verbatim; older ones compact
    summary_tokens = 200,         -- cap on the rolling summary
    chars_per_token = 4,          -- cheap local estimate; no tokenizer needed
  },

  reply = {
    max_chars    = 700,           -- a room is a conversation, not an essay
    chunk_chars  = 180,           -- stream granularity; smaller = faster first
                                  -- word, more frames
    speak        = false,         -- emit audio too (costly — see header)
    stream       = true,
  },

  stt = {
    -- Transcribe talkspurts only. DCF already marks the trailing edge with
    -- FLAG_END_TALKSPURT via DTX, so the segmentation is free: no VAD to run,
    -- no continuous transcription of an idle room.
    enabled       = true,
    min_ms        = 400,          -- ignore blips
    max_ms        = 30000,        -- force a flush on a monologue
  },

  hooks = {
    on_gate_open  = nil,          -- fn(reason, message)
    on_gate_skip  = nil,          -- fn(reason)
    on_prompt     = nil,          -- fn(prompt, est_tokens)
    on_chunk      = nil,          -- fn(text)
    on_reply      = nil,          -- fn(full_text, stats)
    on_bargein    = nil,          -- fn()
    on_compact    = nil,          -- fn(n_turns, summary)
  },
}

local function deep_copy(t)
  if type(t) ~= "table" then return t end
  local o = {}
  for k, v in pairs(t) do o[k] = deep_copy(v) end
  return o
end

local function deep_merge(dst, src)
  for k, v in pairs(src) do
    if type(v) == "table" and type(dst[k]) == "table" then deep_merge(dst[k], v)
    else dst[k] = v end
  end
  return dst
end

function M.configure(t)
  local cfg = deep_copy(M.defaults)
  if t then deep_merge(cfg, deep_copy(t)) end
  assert(cfg.name ~= "" and type(cfg.name) == "string", "name must be a non-empty string")
  assert(cfg.context.reserve_output < cfg.context.max_tokens,
         "context.reserve_output must be below max_tokens")
  assert(cfg.reply.chunk_chars > 0 and cfg.reply.max_chars >= cfg.reply.chunk_chars,
         "reply.max_chars must be >= chunk_chars")
  assert(cfg.context.chars_per_token > 0, "context.chars_per_token must be > 0")
  cfg.hooks = cfg.hooks or {}
  cfg.__dcf_agent_config = true
  return cfg
end

--- Rough token estimate. Deliberately local and tokenizer-free: the budget only
--- needs to be right to within a turn, and shelling out to a tokenizer per
--- message would cost more than the slack it saves.
function M.est_tokens(s, chars_per_token)
  return math.ceil(#s / (chars_per_token or 4))
end

-- ── Gate ────────────────────────────────────────────────────────────────────
-- Returns armed(bool), reason(string). This is where the token bill is decided.
local function normalise(s) return (s:lower():gsub("^%s+", "")) end

function M.addressed(cfg, msg, now_ms, state)
  local g = cfg.gate
  if g.ignore_self and msg.src == cfg.src_id then return false, "self" end
  if state.last_reply_ms and (now_ms - state.last_reply_ms) < g.cooldown_ms then
    return false, "cooldown"
  end

  if g.direct and msg.dst == cfg.src_id then return true, "direct" end

  local lower = normalise(msg.text or "")
  local name = cfg.name:lower()
  if g.mentions then
    if lower:find("@" .. name, 1, true) then return true, "mention" end
    -- "name, ..." or "name: ..." at the start — how people actually address
    -- someone in a room. A bare occurrence mid-sentence is NOT a summons.
    if lower:match("^" .. name:gsub("%W", "%%%0") .. "%s*[,:]") then
      return true, "address"
    end
  end
  for _, w in ipairs(g.wake) do
    if lower:find(w:lower(), 1, true) then return true, "wake" end
  end

  if state.armed_until_ms and now_ms < state.armed_until_ms then
    return true, "followup"
  end
  return false, "not-addressed"
end

-- ── Context ─────────────────────────────────────────────────────────────────
-- Recent turns verbatim, everything older folded into a rolling summary. The
-- system prompt and summary are emitted first and unchanged between calls, so a
-- prompt cache can hit on the whole prefix.
local Ctx = {}
Ctx.__index = Ctx

function M.new_context(cfg)
  return setmetatable({ cfg = cfg, turns = {}, summary = "", compactions = 0 }, Ctx)
end

function Ctx:add(who, text)
  self.turns[#self.turns + 1] = { who = who, text = text }
  local keep = self.cfg.context.recent_turns
  if #self.turns > keep then
    local overflow = {}
    while #self.turns > keep do
      overflow[#overflow + 1] = table.remove(self.turns, 1)
    end
    self:_compact(overflow)
  end
end

--- Fold overflow turns into the summary. Compaction is deliberately mechanical
--- (no model call) — spending inference to shrink context for an agent that may
--- never be addressed again is exactly the cost this module exists to avoid.
--- Supply cfg.summarize to use a model instead when you want fidelity.
function Ctx:_compact(overflow)
  local lines = { self.summary }
  for _, t in ipairs(overflow) do
    lines[#lines + 1] = ("%s: %s"):format(t.who, t.text)
  end
  local merged = table.concat(lines, "\n"):gsub("^\n+", "")
  if self.cfg.summarize then
    local ok, s = pcall(self.cfg.summarize, merged)
    if ok and type(s) == "string" then merged = s end
  end
  local cap = self.cfg.context.summary_tokens * self.cfg.context.chars_per_token
  if #merged > cap then merged = "…" .. merged:sub(-cap) end
  self.summary = merged
  self.compactions = self.compactions + 1
  if self.cfg.hooks.on_compact then
    self.cfg.hooks.on_compact(#overflow, self.summary)
  end
end

--- Assemble the prompt. Returns prompt, est_tokens, prefix_len — prefix_len is
--- how many leading bytes are byte-stable across calls (the cacheable part).
function Ctx:prompt(system, ask)
  local c = self.cfg.context
  local prefix = system
  if self.summary ~= "" then
    prefix = prefix .. "\n\n[earlier in this room]\n" .. self.summary
  end

  local budget = (c.max_tokens - c.reserve_output) * c.chars_per_token
  local tail, used = {}, #prefix + #(ask or "")
  for i = #self.turns, 1, -1 do
    local line = ("%s: %s"):format(self.turns[i].who, self.turns[i].text)
    if used + #line + 1 > budget then break end
    table.insert(tail, 1, line)
    used = used + #line + 1
  end

  local prompt = prefix .. "\n\n" .. table.concat(tail, "\n")
  if ask and ask ~= "" then prompt = prompt .. "\n" .. ask end
  return prompt, M.est_tokens(prompt, c.chars_per_token), #prefix
end

M.Context = Ctx

-- ── Streaming chunker ───────────────────────────────────────────────────────
--- Split streamed model output into room-sized messages on natural boundaries,
--- never mid-UTF-8. A DCF-Text message is a whole certified packet, so chunking
--- badly would put a torn codepoint on the wire.
function M.chunker(chunk_chars)
  return setmetatable({ buf = "", limit = chunk_chars }, {
    __index = {
      --- Feed model output; returns a list of ready-to-send strings.
      push = function(self, s, flush)
        self.buf = self.buf .. (s or "")
        local out = {}
        while true do
          if #self.buf == 0 then break end
          if #self.buf < self.limit and not flush then break end
          local cut
          if #self.buf <= self.limit then
            cut = #self.buf
          else
            -- Prefer a sentence end, then a space, inside the limit.
            local window = self.buf:sub(1, self.limit)
            cut = window:match(".*()[%.%!%?]%s") or window:match(".*()%s") or self.limit
            -- Never split a multi-byte sequence: back off to a lead byte.
            while cut > 1 and (self.buf:byte(cut + 1) or 0) & 0xC0 == 0x80 do
              cut = cut - 1
            end
          end
          out[#out + 1] = self.buf:sub(1, cut)
          self.buf = self.buf:sub(cut + 1):gsub("^%s+", "")
          if not flush and #self.buf < self.limit then break end
        end
        return out
      end,
    },
  })
end

-- ── Agent ───────────────────────────────────────────────────────────────────
local Agent = {}
Agent.__index = Agent

--- backends = {
---   llm  = function(prompt, on_chunk) -> full_text        (required to reply)
---   stt  = function(pcm_blocks) -> text                   (optional)
---   tts  = function(text) -> pcm                          (optional)
---   send = function(frames)                               (required to speak)
---   now  = function() -> ms                               (optional; monotonic)
--- }
function M.new(cfg, backends)
  cfg = (type(cfg) == "table" and cfg.__dcf_agent_config) and cfg or M.configure(cfg)
  assert(cfg.channel, "cfg.channel is required (dcf_text.channel_id(room))")
  local clock = (backends and backends.now) or function() return os.time() * 1000 end
  return setmetatable({
    cfg = cfg, be = backends or {}, now = clock,
    ctx = M.new_context(cfg),
    inbox = T.new_reassembler(cfg.channel),
    packet_id = 0,
    state = { armed_until_ms = nil, last_reply_ms = nil, speaking = false },
    talkspurt = {},
    stats = { heard = 0, thought = 0, skipped = 0, replies = 0,
              chunks_sent = 0, bytes_sent = 0, bargeins = 0,
              prompt_tokens = 0, prefix_reuse = 0 },
  }, Agent)
end

--- Feed one inbound frame. Returns a reply string when the agent chose to speak.
function Agent:receive(frame)
  local events = self.inbox:push(frame)
  local reply
  for _, ev in ipairs(events) do
    if ev.kind == "message" then
      reply = self:hear({ src = ev.src, dst = ev.dst, text = ev.text }) or reply
    end
  end
  return reply
end

--- Feed a message directly (from text, or from STT).
function Agent:hear(msg)
  self.stats.heard = self.stats.heard + 1
  local now = self.now()

  -- A human speaking cancels an in-flight reply. Talking over the agent should
  -- stop it, exactly as it would stop a person.
  if self.state.speaking and msg.src ~= self.cfg.src_id then
    self.state.speaking = false
    self.stats.bargeins = self.stats.bargeins + 1
    if self.cfg.hooks.on_bargein then self.cfg.hooks.on_bargein() end
  end

  local who = ("peer%04x"):format(msg.src or 0)
  self.ctx:add(who, msg.text)

  local armed, reason = M.addressed(self.cfg, msg, now, self.state)
  if not armed then
    self.stats.skipped = self.stats.skipped + 1
    if self.cfg.hooks.on_gate_skip then self.cfg.hooks.on_gate_skip(reason) end
    return nil
  end
  if self.cfg.hooks.on_gate_open then self.cfg.hooks.on_gate_open(reason, msg) end
  return self:think(msg.text)
end

--- Run the model and stream the reply into the room.
function Agent:think(ask)
  if not self.be.llm then return nil end
  local cfg = self.cfg
  local system = cfg.system or ("You are " .. cfg.name ..
    ", a participant in a voice room. Reply in at most two short sentences.")

  local prompt, est, prefix_len = self.ctx:prompt(system, ask)
  self.stats.thought = self.stats.thought + 1
  self.stats.prompt_tokens = self.stats.prompt_tokens + est
  if self._last_prefix_len == prefix_len then
    self.stats.prefix_reuse = self.stats.prefix_reuse + 1
  end
  self._last_prefix_len = prefix_len
  if cfg.hooks.on_prompt then cfg.hooks.on_prompt(prompt, est) end

  local chunker = M.chunker(cfg.reply.chunk_chars)
  local full, stopped = {}, false
  self.state.speaking = true

  local function emit(pieces)
    for _, piece in ipairs(pieces) do
      if not self.state.speaking then stopped = true break end
      self:say(piece)
    end
  end

  local ok, result = pcall(self.be.llm, prompt, function(delta)
    if not self.state.speaking then return false end       -- barge-in: stop
    full[#full + 1] = delta
    local joined = table.concat(full)
    if #joined > cfg.reply.max_chars then
      self.state.speaking = false
      return false
    end
    if cfg.reply.stream then emit(chunker:push(delta, false)) end
    if cfg.hooks.on_chunk then cfg.hooks.on_chunk(delta) end
    return true
  end)

  local text = ok and (type(result) == "string" and result or table.concat(full))
                  or table.concat(full)
  if not cfg.reply.stream then
    emit(chunker:push(text, true))
  else
    emit(chunker:push("", true))
  end

  self.state.speaking = false
  self.state.last_reply_ms = self.now()
  self.state.armed_until_ms = self.now() + cfg.gate.followup_ms
  self.stats.replies = self.stats.replies + 1
  if text ~= "" then self.ctx:add(cfg.name, text) end
  if cfg.hooks.on_reply then cfg.hooks.on_reply(text, self.stats) end
  return text, stopped
end

--- Put one chunk on the wire as a certified DCF-Text message.
function Agent:say(piece)
  if piece == "" then return end
  local frames = T.packetize(piece, self.packet_id % (T.MAX_PACKETID + 1),
                             self.now() % (1 << 24), self.cfg.src_id,
                             self.cfg.channel, T.FLAG_AGENT)
  self.packet_id = self.packet_id + 1
  self.stats.chunks_sent = self.stats.chunks_sent + 1
  self.stats.bytes_sent = self.stats.bytes_sent + #frames * 17
  if self.be.send then self.be.send(frames) end
  -- Audio egress is opt-in: a listener's own TTS is cheaper and better. See
  -- the header for the ~1000x figure.
  if self.cfg.reply.speak and self.be.tts then pcall(self.be.tts, piece) end
  return frames
end

-- ── Voice input ─────────────────────────────────────────────────────────────
--- Accumulate a talkspurt and transcribe it when DTX marks the trailing edge.
--- Segmentation is free: FLAG_END_TALKSPURT is already in the audio descriptor,
--- so an idle room costs no STT at all.
function Agent:hear_audio(pkt, end_of_talkspurt)
  local s = self.cfg.stt
  if not s.enabled or not self.be.stt then return nil end
  if pkt then self.talkspurt[#self.talkspurt + 1] = pkt end

  local ms = #self.talkspurt * (self.cfg.block_ms or 20)
  local flush = end_of_talkspurt or ms >= s.max_ms
  if not flush then return nil end
  if ms < s.min_ms then self.talkspurt = {} return nil end

  local blocks = self.talkspurt
  self.talkspurt = {}
  local ok, text = pcall(self.be.stt, blocks)
  if not ok or not text or text == "" then return nil end
  return self:hear({ src = (blocks[1] and blocks[1].src) or 0, dst = self.cfg.channel,
                     text = text })
end

function Agent:report()
  local s = self.stats
  local r = {}
  for k, v in pairs(s) do r[k] = v end
  r.gate_ratio = s.heard > 0 and s.thought / s.heard or 0
  r.avg_prompt_tokens = s.thought > 0 and s.prompt_tokens / s.thought or 0
  r.compactions = self.ctx.compactions
  return r
end

M.Agent = Agent

return M
