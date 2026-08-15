-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_transport.lua — how a burst of certified frames becomes datagrams.
--
--  lua/ already ships two certified containers that the voice and text paths were
--  not using: dcf_superpack.lua (two 17 B frames -> one 32 B message under a joint
--  CRC) and dcf_fec.lua (systematic Reed-Solomon over GF(2^8) + interleaver, so a
--  lossy medium can CORRECT a frame instead of merely detecting the damage).
--  This module composes them behind one interface.
--
--  Two orthogonal axes, both per-link configurable:
--
--    batch : "none"      one datagram per frame     -- baseline, worst on the wire
--            "concat"    one datagram per burst     -- the big win (see below)
--            "superpack" pair, then concat          -- best bytes AND best syscalls
--
--    fec   : off, or RS with N parity bytes         -- correction for RF/acoustic
--
--  Why batching dominates: a 20 ms Opus-16k block is 11 frames. One datagram each
--  costs 11 x (17 + 28) = 495 B/20 ms = 198 kbps for a 16 kbps stream. The same
--  frames concatenated cost 187 + 28 = 215 B = 86 kbps -- identical bytes on the
--  wire quantum, 2.3x less on the link, and one syscall instead of eleven.
--  SuperPack shaves it again to ~82 kbps. Measure any profile with M.report().
--
--  Nothing here changes a frame. Frames go in certified and come out certified;
--  this only decides how they are grouped into datagrams. Datagrams are Lua
--  strings (what a socket wants); frames are byte arrays (what dcf_audio wants),
--  and the conversion happens once, here.
-- ============================================================================

local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local S = dofile(HERE .. "dcf_superpack.lua")
local F = dofile(HERE .. "dcf_fec.lua")

local M = { superpack = S, fec = F }

local FRAME_LEN, SUPERPACK_LEN = 17, 32

M.defaults = {
  batch = "superpack",     -- "none" | "concat" | "superpack"
  fec = {
    enabled = false,       -- on for RF / acoustic / any medium that corrupts
    parity  = 16,          -- RS parity bytes -> corrects parity/2 byte-errors
  },
  mtu = 1200,              -- split a burst across datagrams above this (safe for
                           -- 1280 B IPv6 minimum MTU minus headers)
  strict = false,          -- true: a malformed datagram raises; false: skip + count
}

-- Per-link starting points, matched to the transport tiers in DCF_TALK_SPEC.md.
M.presets = {
  lan      = { batch = "concat",    fec = { enabled = false } },
  wan      = { batch = "superpack", fec = { enabled = false } },
  rf       = { batch = "superpack", fec = { enabled = true, parity = 16 }, mtu = 512 },
  acoustic = { batch = "superpack", fec = { enabled = true, parity = 32 }, mtu = 255 },
  debug    = { batch = "none",      fec = { enabled = false } },
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

function M.configure(a, b)
  local cfg = deep_copy(M.defaults)
  if type(a) == "string" then
    deep_merge(cfg, deep_copy(M.presets[a] or error("unknown transport preset: " .. a)))
    if type(b) == "table" then deep_merge(cfg, deep_copy(b)) end
  elseif type(a) == "table" then
    deep_merge(cfg, deep_copy(a))
  elseif a ~= nil then
    error("configure expects a preset name or a table")
  end
  assert(cfg.batch == "none" or cfg.batch == "concat" or cfg.batch == "superpack",
         'batch must be "none", "concat" or "superpack"')
  assert(cfg.mtu >= FRAME_LEN, "mtu must fit at least one frame")
  local p = cfg.fec.parity
  assert(p >= 2 and p <= 128 and p % 2 == 0, "fec.parity must be even, 2..128")
  -- RS is over GF(2^8): a codeword is 255 bytes, so payload per chunk is 255-parity.
  assert(p < 255, "fec.parity must leave room for data")
  cfg.__dcf_transport_config = true
  return cfg
end

-- ── byte-array <-> string ───────────────────────────────────────────────────
local function a2s(a)
  local n = #a
  if n <= 200 then return string.char(table.unpack(a, 1, n)) end
  local parts = {}
  for i = 1, n, 200 do
    parts[#parts + 1] = string.char(table.unpack(a, i, math.min(i + 199, n)))
  end
  return table.concat(parts)
end

local function s2a(s)
  local a = {}
  for i = 1, #s do a[i] = s:byte(i) end
  return a
end

M.a2s, M.s2a = a2s, s2a

-- ── Transport ───────────────────────────────────────────────────────────────
local T = {}
T.__index = T

function M.new(cfg)
  cfg = (type(cfg) == "table" and cfg.__dcf_transport_config) and cfg or M.configure(cfg)
  return setmetatable({
    cfg = cfg,
    stats = { frames_out = 0, frames_in = 0, datagrams_out = 0, datagrams_in = 0,
              bytes_out = 0, bytes_in = 0, superpacked = 0, fec_corrected = 0,
              fec_failed = 0, malformed = 0 },
  }, T)
end

--- Group a burst of frames (byte arrays) into datagrams (strings).
--- Call once per 20 ms audio packet, or once per text message.
function T:wrap(frames)
  local cfg = self.cfg
  self.stats.frames_out = self.stats.frames_out + #frames

  -- 1. group
  local units = {}                              -- list of byte arrays
  if cfg.batch == "none" then
    for _, f in ipairs(frames) do units[#units + 1] = f end
  elseif cfg.batch == "superpack" then
    local i = 1
    while i <= #frames do
      if i + 1 <= #frames then
        units[#units + 1] = S.pack(frames[i], frames[i + 1])
        self.stats.superpacked = self.stats.superpacked + 1
        i = i + 2
      else
        units[#units + 1] = frames[i]           -- odd frame rides raw
        i = i + 1
      end
    end
  else                                          -- concat
    for _, f in ipairs(frames) do units[#units + 1] = f end
  end

  -- 2. pack into datagrams under the MTU. "none" keeps one unit per datagram;
  --    the others fill up to the MTU. FEC expands, so budget for it here rather
  --    than discovering the overflow after encoding.
  local budget = cfg.mtu
  if cfg.fec.enabled then
    -- RS adds `parity` per chunk plus a protected 5-byte header (HDR_LEN = 5+parity
    -- worst case). Reserve conservatively so a wrapped burst still fits.
    budget = math.max(FRAME_LEN, cfg.mtu - cfg.fec.parity * 2 - 16)
  end

  local out, cur, curlen = {}, {}, 0
  local function flush()
    if curlen == 0 then return end
    local buf = table.concat(cur)
    if cfg.fec.enabled then
      buf = F.encode_message(buf, cfg.fec.parity)
    end
    out[#out + 1] = buf
    self.stats.bytes_out = self.stats.bytes_out + #buf
    cur, curlen = {}, 0
  end

  for _, u in ipairs(units) do
    local s = a2s(u)
    if cfg.batch == "none" or curlen + #s > budget then flush() end
    cur[#cur + 1] = s
    curlen = curlen + #s
    if cfg.batch == "none" then flush() end
  end
  flush()

  self.stats.datagrams_out = self.stats.datagrams_out + #out
  return out
end

--- Recover frames (byte arrays) from one received datagram (string).
--- Returns frames, err. Frames survive individually: a torn tail does not discard
--- the frames already parsed ahead of it.
function T:unwrap(datagram)
  local cfg = self.cfg
  self.stats.datagrams_in = self.stats.datagrams_in + 1
  self.stats.bytes_in = self.stats.bytes_in + #datagram

  local buf = datagram
  if cfg.fec.enabled then
    local ok, dec = pcall(F.decode_message, buf)
    if not ok or not dec then
      self.stats.fec_failed = self.stats.fec_failed + 1
      if cfg.strict then error("fec: unrecoverable datagram") end
      return {}, "fec-unrecoverable"
    end
    if dec ~= buf then self.stats.fec_corrected = self.stats.fec_corrected + 1 end
    buf = dec
  end

  local frames, i, err = {}, 1, nil
  while i <= #buf do
    local remain = #buf - i + 1
    if remain >= SUPERPACK_LEN and S.is_superpack(s2a(buf:sub(i, i + SUPERPACK_LEN - 1))) then
      local okp, a, b = pcall(S.unpack, s2a(buf:sub(i, i + SUPERPACK_LEN - 1)))
      if okp and a and b then
        frames[#frames + 1] = a
        frames[#frames + 1] = b
        i = i + SUPERPACK_LEN
      else
        self.stats.malformed = self.stats.malformed + 1
        err = "superpack"
        if cfg.strict then error("transport: bad superpack at " .. i) end
        i = i + SUPERPACK_LEN
      end
    elseif remain >= FRAME_LEN then
      frames[#frames + 1] = s2a(buf:sub(i, i + FRAME_LEN - 1))
      i = i + FRAME_LEN
    else
      self.stats.malformed = self.stats.malformed + 1
      err = "trailing-bytes"
      if cfg.strict then error("transport: " .. remain .. " trailing bytes") end
      break
    end
  end

  self.stats.frames_in = self.stats.frames_in + #frames
  return frames, err
end

function T:reset_stats()
  for k in pairs(self.stats) do self.stats[k] = 0 end
end

M.Transport = T

-- ── Measurement ─────────────────────────────────────────────────────────────
--- Actually measure a codec profile through a transport, rather than modelling it.
--- Returns bytes and kbps on the link, including per-datagram IP+UDP overhead.
---   M.report(40, "superpack")   -- Opus 16 kbps, 40 B per 20 ms block
function M.report(bytes_per_block, transport_cfg, opts)
  opts = opts or {}
  local A = dofile(HERE .. "dcf_audio.lua")
  local block_ms = opts.block_ms or 20
  local udp = opts.udp_overhead or 28

  local payload = {}
  for i = 1, bytes_per_block do payload[i] = i % 256 end
  local frames = A.packetize(A.CODEC_OPUS, payload, 1, 0, 1, 2, 0)

  local t = M.new(transport_cfg)
  local dgrams = t:wrap(frames)
  local link = 0
  for _, d in ipairs(dgrams) do link = link + #d + udp end

  return {
    frames = #frames,
    datagrams = #dgrams,
    frame_bytes = #frames * FRAME_LEN,
    link_bytes = link,
    kbps = link * (1000 / block_ms) * 8 / 1000,
    bytes_per_datagram = link / #dgrams,
  }
end

return M
