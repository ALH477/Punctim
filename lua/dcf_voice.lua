-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_voice.lua — DCF-Audio L3 (jitter buffer + PLC) and the voice pipeline.
--
--  lua/dcf_audio.lua implements L1 (codec registry) and L2 (certified framing).
--  This module adds the layers above it that a live voice client needs and that
--  the Lua tier was missing — L3 dejitter/conceal (the C and Rust references have
--  it; see codec/src/audio.rs `Codec::plc`) and L4 pipeline glue.
--
--  L2 is certified; L3 is runtime behaviour and is deliberately NOT byte-pinned —
--  concealment is a quality choice, not a wire fact. Nothing here can change a
--  frame: every byte still comes out of dcf_audio.packetize.
--
--  EVERYTHING IS CONFIGURABLE. See M.defaults for the full knob list, M.presets
--  for named starting points, and M.register_codec to add your own codec with its
--  own concealment strategy. Transport and audio devices are injected as
--  callbacks, so the whole pipeline runs headless and testable with no sockets
--  and no sound card.
--
--  Requires Lua 5.3+ (native bitwise ops), which the demod-ui host provides.
-- ============================================================================

local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local A = dofile(HERE .. "dcf_audio.lua")

local M = { audio = A }

-- ── Configuration ───────────────────────────────────────────────────────────
-- Every value below can be overridden per-call; M.configure deep-merges your
-- table over these and validates the result. Nothing is hard-coded downstream.
M.defaults = {
  codec_id     = A.CODEC_PCM_DIAG, -- 0 Opus · 1 PCM-diag · 2 Faust-PM · or your own
  block_ms     = 20,               -- L2 block period (the spec's quantum)
  channel      = A.BROADCAST,      -- rendezvous dst; see dcf_audio.channel_from_passphrase
  src_id       = 0x0001,           -- this node's u16 id

  jitter = {
    target_ms   = 40,    -- nominal buffer depth (2 blocks) — latency vs loss
    min_ms      = 20,    -- adaptive floor
    max_ms      = 200,   -- adaptive ceiling
    adaptive    = true,  -- grow on late packets, shrink when clean
    grow_ms     = 20,    -- step up when a packet arrives late
    shrink_ms   = 5,     -- step down after `shrink_after` clean pops
    shrink_after= 100,
    late_policy = "drop",-- "drop" (arrived after playout) or "accept" (best effort)
    max_slots   = 256,   -- hard cap on buffered packets (memory bound)
  },

  plc = {
    enabled      = true,
    max_consecutive = 5,  -- after N concealed blocks in a row, emit silence instead
    fade         = 0.65,  -- per-block amplitude decay while concealing (0..1)
  },

  vad = {
    enabled       = true,   -- silence suppression (DTX) — the biggest bandwidth win
    threshold     = 0.02,   -- RMS below this counts as silence
    hangover_ms   = 200,    -- keep sending this long after speech stops
    -- On the trailing edge the last packet is flagged FLAG_END_TALKSPURT, which is
    -- exactly what that spec flag is for; receivers can then relax the buffer.
  },

  hooks = {
    on_talkspurt_start = nil, -- fn()
    on_talkspurt_end   = nil, -- fn()
    on_packet_out      = nil, -- fn(frames, packet_id)      -- after packetize
    on_packet_in       = nil, -- fn(pkt)                    -- after reassembly
    on_conceal         = nil, -- fn(packet_id, run_length)  -- PLC fired
    on_late            = nil, -- fn(packet_id, playout_id)
    on_drop            = nil, -- fn(packet_id, reason)
    on_stats           = nil, -- fn(stats)                  -- each pop
  },
}

-- Named starting points. Copy and override — these are just tables.
M.presets = {
  -- switched LAN: minimal latency, loss is rare
  lan    = { jitter = { target_ms = 20, max_ms = 60 } },
  -- internet / VPN: absorb reordering and RTT jitter
  wan    = { jitter = { target_ms = 60, max_ms = 300, grow_ms = 40 } },
  -- lossy RF / acoustic: deep buffer, aggressive concealment, DTX hard on
  field  = { jitter = { target_ms = 120, max_ms = 500 },
             plc = { max_consecutive = 12, fade = 0.8 },
             vad = { threshold = 0.035, hangover_ms = 400 } },
  -- studio jam: latency over everything, no silence gating
  studio = { jitter = { target_ms = 20, min_ms = 20, max_ms = 40, adaptive = false },
             vad = { enabled = false } },
}

local function deep_merge(dst, src)
  for k, v in pairs(src) do
    if type(v) == "table" and type(dst[k]) == "table" then deep_merge(dst[k], v)
    else dst[k] = v end
  end
  return dst
end

local function deep_copy(t)
  if type(t) ~= "table" then return t end
  local o = {}
  for k, v in pairs(t) do o[k] = deep_copy(v) end
  return o
end

--- Build a validated config. Accepts a preset name, a table, or both:
---   configure("wan")                      configure{ jitter = { target_ms = 80 } }
---   configure("field", { src_id = 0x2A })
function M.configure(a, b)
  local cfg = deep_copy(M.defaults)
  if type(a) == "string" then
    local p = M.presets[a] or error("unknown preset: " .. a)
    deep_merge(cfg, deep_copy(p))
    if type(b) == "table" then deep_merge(cfg, deep_copy(b)) end
  elseif type(a) == "table" then
    deep_merge(cfg, deep_copy(a))
  elseif a ~= nil then
    error("configure expects a preset name or a table")
  end

  local j = cfg.jitter
  assert(cfg.block_ms > 0, "block_ms must be > 0")
  assert(j.min_ms <= j.target_ms and j.target_ms <= j.max_ms,
         "jitter: require min_ms <= target_ms <= max_ms")
  assert(j.late_policy == "drop" or j.late_policy == "accept",
         'jitter.late_policy must be "drop" or "accept"')
  assert(j.max_slots > 0, "jitter.max_slots must be > 0")
  assert(cfg.plc.fade >= 0 and cfg.plc.fade <= 1, "plc.fade must be 0..1")
  assert(cfg.src_id >= 0 and cfg.src_id <= 0xFFFF, "src_id must be u16")
  assert(cfg.channel >= 0 and cfg.channel <= 0xFFFF, "channel must be u16")
  cfg.hooks = cfg.hooks or {}
  return cfg
end

-- ── Codec registry (extensible) ─────────────────────────────────────────────
-- A codec is { encode(samples)->bytes, decode(bytes)->samples, plc(state)->samples,
--              block_samples } . Register your own id, or replace a built-in.
M.codecs = {}

function M.register_codec(id, def)
  assert(type(id) == "number" and id >= 0 and id < 256, "codec id must be u8")
  assert(type(def.encode) == "function" and type(def.decode) == "function",
         "codec needs encode/decode")
  def.plc = def.plc or function(st)                  -- default: decaying repeat
    if not st.last then return nil end
    local out, g = {}, st.fade or 0.65
    for i = 1, #st.last do out[i] = st.last[i] * g end
    return out
  end
  M.codecs[id] = def
  return def
end

-- PCM-diag (id 1): 6 kHz 8-bit, 120 B/block — pure Lua, no dependencies.
M.register_codec(A.CODEC_PCM_DIAG, {
  name = "pcm-diag",
  block_samples = A.PCM_DIAG_BLOCK,
  encode = A.pcm_diag_encode,
  decode = A.pcm_diag_decode,
})

-- Faust-PM (id 2): 8-byte parameter block; conceal by holding params and decaying.
M.register_codec(A.CODEC_FAUST_PM, {
  name = "faust-pm",
  block_samples = 0,           -- resynthesised by the host, not sample-carried
  encode = function(p) return A.pm_pack(p) end,
  decode = function(b) return A.pm_unpack(b) end,
  plc = function(st)
    if not st.last_params then return nil end
    local p = {}
    for k, v in pairs(st.last_params) do p[k] = v end
    p.amp = math.floor((p.amp or 0) * (st.fade or 0.65))
    return p
  end,
})

-- Opus (id 0) is host-provided: bind it and register. Left unregistered so a pure
-- Lua build fails loudly rather than silently sending garbage.
--   dcf_voice.register_codec(0, { encode = dm.opus.encode, decode = dm.opus.decode,
--                                 plc = dm.opus.plc, block_samples = 960 })

-- ── L3: jitter buffer with PLC ──────────────────────────────────────────────
-- packet_id is 11 bits and wraps every 2048 packets (~41 s at 20 ms), so ordering
-- is modular, never plain <. This is the classic sequence-wrap bug; it is handled
-- once here and nowhere else needs to think about it.
local SPACE = A.MAX_PACKETID + 1          -- 2048
local HALF  = SPACE // 2

local function mod_diff(a, b)             -- signed distance a - b in modular space
  return ((a - b + HALF) % SPACE) - HALF
end
M.mod_diff = mod_diff

local Jitter = {}
Jitter.__index = Jitter

function M.new_jitter(cfg)
  cfg = cfg or M.configure()
  return setmetatable({
    cfg = cfg,
    slots = {},          -- packet_id -> pkt
    n = 0,
    playout = nil,       -- next packet_id to emit
    started = false,     -- true once the first block has been popped
    depth_ms = cfg.jitter.target_ms,
    clean_run = 0,
    conceal_run = 0,
    plc_state = { fade = cfg.plc.fade },
    stats = { pushed = 0, popped = 0, concealed = 0, late = 0,
              dropped = 0, silence = 0, reordered = 0, max_occupancy = 0 },
  }, Jitter)
end

function Jitter:depth_packets()
  local n = math.floor(self.depth_ms / self.cfg.block_ms + 0.5)
  return n < 1 and 1 or n
end

--- Insert a reassembled packet (as returned by dcf_audio.Reassembler:push).
function Jitter:push(pkt)
  if not pkt then return false end
  local h = self.cfg.hooks
  self.stats.pushed = self.stats.pushed + 1

  if self.playout == nil then                     -- first packet starts the clock
    self.playout = pkt.packet_id
  elseif mod_diff(pkt.packet_id, self.playout) < 0 and not self.started then
    -- Still priming: an earlier packet just means the stream began before the one
    -- that happened to arrive first. Wind the clock back instead of calling it
    -- late, otherwise a reordered first burst is silently discarded.
    self.playout = pkt.packet_id
    self.stats.reordered = self.stats.reordered + 1
  elseif mod_diff(pkt.packet_id, self.playout) < 0 then
    self.stats.late = self.stats.late + 1
    if h.on_late then h.on_late(pkt.packet_id, self.playout) end
    if self.cfg.jitter.adaptive then              -- late => widen the window
      self.depth_ms = math.min(self.cfg.jitter.max_ms,
                               self.depth_ms + self.cfg.jitter.grow_ms)
      self.clean_run = 0
    end
    if self.cfg.jitter.late_policy == "drop" then
      self.stats.dropped = self.stats.dropped + 1
      if h.on_drop then h.on_drop(pkt.packet_id, "late") end
      return false
    end
  end

  if self.slots[pkt.packet_id] then return false end   -- duplicate
  if self.n >= self.cfg.jitter.max_slots then
    self.stats.dropped = self.stats.dropped + 1
    if h.on_drop then h.on_drop(pkt.packet_id, "overflow") end
    return false
  end

  self.slots[pkt.packet_id] = pkt
  self.n = self.n + 1
  if self.n > self.stats.max_occupancy then self.stats.max_occupancy = self.n end
  if h.on_packet_in then h.on_packet_in(pkt) end
  return true
end

--- Emit the next block, or conceal it. Returns:
---   pkt, "packet"    a real packet
---   samples, "conceal"  PLC output (nil samples if the codec can't conceal)
---   nil, "silence"   concealment exhausted -> emit silence
---   nil, "starved"   not enough buffered yet; caller should wait
function Jitter:pop()
  local h, cfg = self.cfg.hooks, self.cfg
  if self.playout == nil then return nil, "starved" end

  local want = self.playout
  local pkt = self.slots[want]

  if not pkt then
    -- Nothing for this slot. Only conceal once the buffer has actually filled to
    -- depth; otherwise we are still priming and should wait.
    if self.n < self:depth_packets() then
      local any = false
      for id in pairs(self.slots) do
        if mod_diff(id, want) > 0 then any = true break end
      end
      if not any then return nil, "starved" end
    end

    self.playout = (want + 1) % SPACE
    self.conceal_run = self.conceal_run + 1
    self.clean_run = 0
    if not cfg.plc.enabled or self.conceal_run > cfg.plc.max_consecutive then
      self.stats.silence = self.stats.silence + 1
      return nil, "silence"
    end
    self.stats.concealed = self.stats.concealed + 1
    if h.on_conceal then h.on_conceal(want, self.conceal_run) end
    local codec = M.codecs[cfg.codec_id]
    local out = codec and codec.plc and codec.plc(self.plc_state) or nil
    self.plc_state.last = out or self.plc_state.last
    return out, "conceal"
  end

  self.slots[want] = nil
  self.n = self.n - 1
  self.playout = (want + 1) % SPACE
  self.started = true
  self.conceal_run = 0
  self.clean_run = self.clean_run + 1

  if cfg.jitter.adaptive and self.clean_run >= cfg.jitter.shrink_after then
    self.depth_ms = math.max(cfg.jitter.min_ms, self.depth_ms - cfg.jitter.shrink_ms)
    self.clean_run = 0
  end

  -- Remember the decoded block so PLC has something to work from.
  local codec = M.codecs[pkt.codec_id]
  if codec and codec.decode then
    local okd, dec = pcall(codec.decode, pkt.payload)
    if okd then
      if cfg.codec_id == A.CODEC_FAUST_PM then self.plc_state.last_params = dec
      else self.plc_state.last = dec end
      pkt.samples = dec
    end
  end

  self.stats.popped = self.stats.popped + 1
  if h.on_stats then h.on_stats(self.stats) end
  return pkt, "packet"
end

function Jitter:occupancy() return self.n end
function Jitter:reset()
  self.slots, self.n, self.playout = {}, 0, nil
  self.conceal_run, self.clean_run, self.started = 0, 0, false
end

M.Jitter = Jitter

-- ── VAD / DTX ───────────────────────────────────────────────────────────────
local function rms(samples)
  if not samples or #samples == 0 then return 0 end
  local s = 0
  for i = 1, #samples do s = s + samples[i] * samples[i] end
  return math.sqrt(s / #samples)
end
M.rms = rms

local Vad = {}
Vad.__index = Vad

function M.new_vad(cfg)
  cfg = cfg or M.configure()
  return setmetatable({ cfg = cfg, active = false, hangover = 0 }, Vad)
end

--- Returns send(bool), end_of_talkspurt(bool).
function Vad:update(samples)
  local c = self.cfg.vad
  if not c.enabled then return true, false end
  local hang_blocks = math.floor(c.hangover_ms / self.cfg.block_ms + 0.5)
  local loud = rms(samples) >= c.threshold
  local h = self.cfg.hooks

  if loud then
    if not self.active and h.on_talkspurt_start then h.on_talkspurt_start() end
    self.active, self.hangover = true, hang_blocks
    return true, false
  end
  if self.hangover > 0 then
    self.hangover = self.hangover - 1
    if self.hangover == 0 then
      self.active = false
      if h.on_talkspurt_end then h.on_talkspurt_end() end
      return true, true                    -- last packet: flag END_TALKSPURT
    end
    return true, false
  end
  return false, false
end

M.Vad = Vad

-- ── L4: pipeline ────────────────────────────────────────────────────────────
-- Transport and devices are injected, so this runs headless in tests and against
-- dm.net / dm.audio in demod-ui without changing a line:
--   send  = function(frames) ... end     -- one call per 20 ms block (batch these
--                                        -- into ONE datagram; see DCF_TALK_SPEC §2.1)
local Voice = {}
Voice.__index = Voice

function M.new(cfg, send)
  cfg = (type(cfg) == "table" and cfg.jitter and cfg) or M.configure(cfg)
  return setmetatable({
    cfg = cfg, send = send,
    packet_id = 0,
    reasm = A.Reassembler.new(cfg.channel),
    jitter = M.new_jitter(cfg),
    vad = M.new_vad(cfg),
    sent = 0, suppressed = 0,
  }, Voice)
end

--- Capture side: hand it one block of float samples (-1..1) and a µs timestamp.
--- Returns the frames it sent, or nil when VAD suppressed the block.
function Voice:capture(samples, ts_us)
  local send_it, eot = self.vad:update(samples)
  if not send_it then
    self.suppressed = self.suppressed + 1
    return nil
  end
  local codec = M.codecs[self.cfg.codec_id]
        or error("codec " .. tostring(self.cfg.codec_id) .. " not registered")
  local payload = codec.encode(samples)
  local flags = eot and A.FLAG_END_TALKSPURT or 0
  local frames = A.packetize(self.cfg.codec_id, payload, self.packet_id,
                             ts_us % (1 << 24), self.cfg.src_id, self.cfg.channel, flags)
  self.packet_id = (self.packet_id + 1) % SPACE
  self.sent = self.sent + 1
  if self.cfg.hooks.on_packet_out then
    self.cfg.hooks.on_packet_out(frames, self.packet_id)
  end
  if self.send then self.send(frames) end
  return frames
end

--- Receive side: feed every inbound 17-byte frame.
function Voice:receive(frame)
  local pkt = self.reasm:push(frame)
  if pkt then self.jitter:push(pkt) end
  return pkt
end

--- Playout side: call once per block period. Same returns as Jitter:pop.
function Voice:playout() return self.jitter:pop() end

function Voice:stats()
  local s = {}
  for k, v in pairs(self.jitter.stats) do s[k] = v end
  s.sent, s.suppressed = self.sent, self.suppressed
  s.depth_ms, s.occupancy = self.jitter.depth_ms, self.jitter.n
  s.rejected = self.reasm.rejected
  if s.sent + s.suppressed > 0 then
    s.dtx_ratio = s.suppressed / (s.sent + s.suppressed)
  end
  return s
end

M.Voice = Voice

-- ── Bandwidth model (informative; see DCF_TALK_SPEC.md §2) ──────────────────
--- Wire cost of a codec profile. bytes_per_block -> frames, bytes/s, kbps.
--- batch=true models one datagram per audio packet (the big win); false models
--- one datagram per frame. udp_overhead defaults to 28 B (IPv4+UDP).
function M.wire_cost(bytes_per_block, opts)
  opts = opts or {}
  local block_ms = opts.block_ms or 20
  local udp = opts.udp_overhead or 28
  local batch = opts.batch ~= false
  local frames = 1 + math.ceil(bytes_per_block / 4)
  local frame_bytes = frames * 17
  local datagrams = batch and 1 or frames
  local total = frame_bytes + datagrams * udp
  local per_sec = 1000 / block_ms
  return {
    frames = frames, datagrams = datagrams,
    frame_bytes = frame_bytes, wire_bytes = total,
    kbps = total * per_sec * 8 / 1000,
    tax = frame_bytes / math.max(bytes_per_block, 1),
  }
end

return M
