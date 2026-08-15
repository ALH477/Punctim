-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_snake.lua — DCF-Snake L2 framing + grandmaster media clock (pure Lua).
--
--  The Lua port of the CERTIFIED DCF-Snake adapter (codec/src/snake.rs,
--  Documentation/DCF_SNAKE_SPEC.md), pinned by Documentation/snake_vectors.json —
--  the same bytes the C and Rust implementations are certified against.
--
--  Snake is the MIXER plane. Where DCF-Audio carries a 20 ms block from one peer
--  (an 11:5 seq split, <=124 B), Snake carries a whole quanta QSS commit-hop packet
--  from one of 32 sources (a 5:11 split, <=8188 B) plus a broadcast media clock.
--  That is exactly the shape a hub needs: many numbered sources, one grandmaster.
--
--  L2 record plane (CTRL type 3; big-endian):
--    seq (u16)     = stream_id[15:11] (5 bits, 0..31) | frag_idx[10:0] (11 bits)
--    frag_idx 0    descriptor : payload = [len_hi, len_lo, mode_id, flags]
--    frag_idx k    data       : payload = qss[(k-1)*4 .. +4]  (last zero-padded)
--    frag_total    = ceil(len/4)   (<= 2047  =>  len <= 8188 bytes / message)
--
--  BEACON clock plane (type 2) reuses the SAME 5:11 fragmenter with kind=CLOCK_VER,
--  so a 16-byte clock is 5 frames. Absolute time lives ONLY in gm_sample_count; the
--  frame's 24-bit timestamp_us wraps every ~16.7 s and carries the TX instant for
--  delay/skew estimation, never alignment.
--
--  CAUTION — one reassembler per dst. CTRL(3) carries both DCF-Audio (11:5) and
--  DCF-Snake (5:11). The same seq bits mean different things, so a node must never
--  multiplex both on one `dst` channel. new_reassembler() defaults to accepting
--  CTRL only; pass FBEACON for the clock plane.
--
--  Self-certifies on load (M.CERTIFIED). Requires Lua 5.3+ (native bitwise ops),
--  which the demod-ui host provides.
-- ============================================================================

local M = {}

-- ── DeModFrame wire codec (byte-identical to GUI/wirelab.lua — certified) ───
local SYNC, VERSION, FRAME_LEN = 0xD3, 1, 17

local function crc16(bytes, n)
  local crc = 0xFFFF
  for i = 1, n do
    crc = (crc ~ ((bytes[i] & 0xFF) << 8)) & 0xFFFF
    for _ = 1, 8 do
      if (crc & 0x8000) ~= 0 then crc = ((crc << 1) ~ 0x1021) & 0xFFFF
      else crc = (crc << 1) & 0xFFFF end
    end
  end
  return crc
end

local function encode(ftype, seq, src, dst, payload, ts)
  local w = {}
  w[1] = SYNC
  w[2] = ((VERSION & 0xF) << 4) | (ftype & 0xF)
  w[3] = (seq >> 8) & 0xFF; w[4] = seq & 0xFF
  w[5] = (src >> 8) & 0xFF; w[6] = src & 0xFF
  w[7] = (dst >> 8) & 0xFF; w[8] = dst & 0xFF
  for i = 1, 4 do w[8 + i] = (payload[i] or 0) & 0xFF end
  w[13] = (ts >> 16) & 0xFF; w[14] = (ts >> 8) & 0xFF; w[15] = ts & 0xFF
  local c = crc16(w, 15)
  w[16] = (c >> 8) & 0xFF; w[17] = c & 0xFF
  return w
end

local function decode(w)
  if #w ~= FRAME_LEN then return nil, "length" end
  if w[1] ~= SYNC then return nil, "bad sync" end
  if (w[2] >> 4) ~= VERSION then return nil, "bad version" end
  if crc16(w, 15) ~= ((w[16] << 8) | w[17]) then return nil, "crc" end
  return {
    ftype = w[2] & 0xF,
    seq = (w[3] << 8) | w[4], src = (w[5] << 8) | w[6], dst = (w[7] << 8) | w[8],
    payload = { w[9], w[10], w[11], w[12] },
    ts = (w[13] << 16) | (w[14] << 8) | w[15],
  }
end

local function frame_to_hex(w)
  local t = {}
  for i = 1, #w do t[i] = ("%02x"):format(w[i]) end
  return table.concat(t)
end

local function hex_to_frame(h)
  local w = {}
  for i = 1, #h, 2 do w[#w + 1] = tonumber(h:sub(i, i + 1), 16) end
  return w
end

M.crc16, M.encode, M.decode = crc16, encode, decode
M.frame_to_hex, M.hex_to_frame = frame_to_hex, hex_to_frame

-- ── L2 constants (mirror codec/src/snake.rs exactly) ────────────────────────
M.FRAG_BITS     = 11
M.FRAG_MASK     = 0x7FF
M.MAX_FRAGS     = 2047
M.MAX_PAYLOAD   = 8188            -- MAX_FRAGS * 4, bytes / message
M.MAX_STREAM_ID = 31              -- (1 << (16 - FRAG_BITS)) - 1
M.BROADCAST     = 0xFFFF

M.FCTRL, M.FBEACON = 3, 2

M.MODE_LIVE, M.MODE_NEAR, M.MODE_RELAXED = 0, 1, 2   -- 64 / 128 / 256 ms hops
M.FLAG_MORE, M.FLAG_ANCHOR, M.FLAG_END = 0x01, 0x02, 0x04

M.CLOCK_LEN, M.CLOCK_VER = 16, 1
M.NOMINAL_RATE_MHZ_48K = 48000000
M.PID_MOD = 2048

-- ── Rendezvous ──────────────────────────────────────────────────────────────
function M.channel_id(name)
  if name == nil or name == "" then return M.BROADCAST end
  local b = {}
  for i = 1, #name do b[i] = string.byte(name, i) end
  return crc16(b, #name)
end

function M.accepts(frame_dst, my_channel)
  return frame_dst == my_channel or frame_dst == M.BROADCAST
end

-- ── L2: the shared 5:11 fragmenter ──────────────────────────────────────────
-- payload: 1-indexed byte array. Used by BOTH the CTRL record plane and the
-- BEACON clock plane; only frame_type and `kind` differ, which is why adding a
-- mode or a clock version never touches the vectors.
local function frag_packetize(payload, stream_id, ts_us, src, dst, ftype, kind, flags)
  local n = #payload
  assert(n <= M.MAX_PAYLOAD,
         ("payload %dB exceeds %dB / message"):format(n, M.MAX_PAYLOAD))
  assert(stream_id >= 0 and stream_id <= M.MAX_STREAM_ID,
         ("stream_id must be 0..%d (5-bit field)"):format(M.MAX_STREAM_ID))

  local frag_total = (n + 3) // 4
  local base = (stream_id << M.FRAG_BITS)
  local frames = {}

  frames[1] = encode(ftype, base, src, dst,
                     { (n >> 8) & 0xFF, n & 0xFF, kind, flags }, ts_us)
  for k = 1, frag_total do
    local chunk = {}
    for i = 1, 4 do
      local off = (k - 1) * 4 + i
      chunk[i] = (off <= n) and payload[off] or 0
    end
    frames[#frames + 1] = encode(ftype, base | k, src, dst, chunk, ts_us)
  end
  return frames
end
M.frag_packetize = frag_packetize

--- Serialise one QSS commit-hop packet into CTRL(3) record frames.
function M.packetize(payload, stream_id, ts_us, src, dst, mode_id, flags)
  return frag_packetize(payload, stream_id, ts_us, src, dst,
                        M.FCTRL, mode_id or M.MODE_LIVE, flags or 0)
end

--- Serialise a packed 16-byte grandmaster clock into BEACON(2) frames.
function M.beacon_packetize(clock_bytes, beacon_slot, ts_us, src, dst, flags)
  assert(#clock_bytes == M.CLOCK_LEN, "clock payload must be 16 bytes")
  return frag_packetize(clock_bytes, beacon_slot, ts_us, src, dst,
                        M.FBEACON, M.CLOCK_VER, flags or 0)
end

-- ── L2: reassembly ──────────────────────────────────────────────────────────
local Reassembler = {}
Reassembler.__index = Reassembler

--- accept_type defaults to CTRL(3) (record plane); pass M.FBEACON for the clock
--- plane. accept_dst nil accepts every channel.
function M.new_reassembler(accept_type, accept_dst)
  return setmetatable({
    accept_type = accept_type or M.FCTRL,
    accept_dst = accept_dst,
    slots = {}, rejected = 0,
  }, Reassembler)
end

function Reassembler:push(frame)
  local d = decode(frame)
  if not d then return nil end
  if d.ftype ~= self.accept_type then return nil end
  if self.accept_dst ~= nil and not M.accepts(d.dst, self.accept_dst) then
    self.rejected = self.rejected + 1
    return nil
  end

  local stream_id = d.seq >> M.FRAG_BITS
  local frag_idx  = d.seq & M.FRAG_MASK

  local s = self.slots[stream_id]
  if not s then s = { desc = nil, frags = {}, n = 0 }; self.slots[stream_id] = s end
  s.ts, s.src, s.dst = d.ts, d.src, d.dst

  if frag_idx == 0 then
    if not s.desc then
      local len = (d.payload[1] << 8) | d.payload[2]
      s.desc = { len = len, kind = d.payload[3], flags = d.payload[4],
                 total = (len + 3) // 4 }
    end
  elseif not s.frags[frag_idx] then
    s.frags[frag_idx] = d.payload
    s.n = s.n + 1
  end

  if not s.desc then return nil end
  if s.n < s.desc.total then return nil end
  for k = 1, s.desc.total do if not s.frags[k] then return nil end end

  local bytes = {}
  for k = 1, s.desc.total do
    local p = s.frags[k]
    bytes[#bytes + 1] = p[1]; bytes[#bytes + 1] = p[2]
    bytes[#bytes + 1] = p[3]; bytes[#bytes + 1] = p[4]
  end
  for i = #bytes, s.desc.len + 1, -1 do bytes[i] = nil end

  self.slots[stream_id] = nil
  return { stream_id = stream_id, ts_us = s.ts, src = s.src, dst = s.dst,
           mode_id = s.desc.kind, flags = s.desc.flags, payload = bytes }
end

--- Report every still-incomplete stream as lost (ascending), clearing state.
function Reassembler:finalize()
  local ids = {}
  for id in pairs(self.slots) do ids[#ids + 1] = id end
  table.sort(ids)
  self.slots = {}
  return ids
end

M.Reassembler = Reassembler

-- ── Grandmaster media clock ─────────────────────────────────────────────────
-- 16 bytes big-endian:
--   [1..8]   gm_sample_count  u64  monotonic capture-sample index, never wraps
--   [9..12]  nominal_rate_mHz u32  48000000 = 48 kHz
--   [13..14] tx_seq           u16  beacon sequence (PLL discipline / loss detect)
--   [15..16] epoch            u16  bumps on rate change / mixer restart
--
-- *** u64 vs Lua's SIGNED 64-bit integers. ***
-- Rust types gm_sample_count as u64; Lua 5.3+ integers are 64-bit but SIGNED, so
-- values at or above 2^63 cannot be written as a Lua integer literal (the parser
-- silently produces a float and loses precision). The BIT PATTERN is unaffected:
-- Lua's >> is a logical shift, so pack/unpack round-trip the full u64 range
-- byte-for-byte -- 2^64-1 simply reads back as -1, the same 64 bits.
--
-- In practice this never bites: 2^63-1 samples is ~6.1 million years at 48 kHz, so
-- a real grandmaster count is always positive and exact. It matters only when
-- comparing against a u64 written in decimal (a golden vector, a Rust log). Use
-- M.gm_tostring() to render one, and M.gm_from_decimal() to read one back.
function M.pack_clock(c)
  local o = {}
  local gm = c.gm_sample_count or 0
  for i = 0, 7 do o[i + 1] = (gm >> (56 - 8 * i)) & 0xFF end
  local r = c.nominal_rate_mhz or M.NOMINAL_RATE_MHZ_48K
  o[9]  = (r >> 24) & 0xFF; o[10] = (r >> 16) & 0xFF
  o[11] = (r >> 8) & 0xFF;  o[12] = r & 0xFF
  local t = c.tx_seq or 0
  o[13] = (t >> 8) & 0xFF; o[14] = t & 0xFF
  local e = c.epoch or 0
  o[15] = (e >> 8) & 0xFF; o[16] = e & 0xFF
  return o
end

function M.unpack_clock(b)
  assert(#b == M.CLOCK_LEN, "clock payload must be 16 bytes")
  local gm = 0
  for i = 1, 8 do gm = (gm << 8) | (b[i] & 0xFF) end
  return {
    gm_sample_count  = gm,
    nominal_rate_mhz = (b[9] << 24) | (b[10] << 16) | (b[11] << 8) | b[12],
    tx_seq = (b[13] << 8) | b[14],
    epoch  = (b[15] << 8) | b[16],
  }
end

--- Render a gm_sample_count as its UNSIGNED decimal string, matching how Rust
--- prints the same u64. Negative Lua values are the high half of the u64 range.
function M.gm_tostring(v)
  if v >= 0 then return ("%d"):format(v) end
  -- Split into high/low 32-bit halves and do the decimal add in string space, so
  -- no step ever exceeds the signed range.
  local hi = (v >> 32) & 0xFFFFFFFF
  local lo = v & 0xFFFFFFFF
  local digits, carry = {}, 0
  local a = { hi * 4294967296 // 1000000000, 0 }  -- unused placeholder
  -- 2^64 + v, computed as (hi * 2^32 + lo) with hi taken unsigned.
  local parts = { hi, lo }
  local acc = "0"
  local function mul_add(str, mul, add)
    local out, c = {}, add
    for i = #str, 1, -1 do
      local d = tonumber(str:sub(i, i)) * mul + c
      out[#out + 1] = tostring(d % 10)
      c = d // 10
    end
    while c > 0 do out[#out + 1] = tostring(c % 10); c = c // 10 end
    local r = string.reverse(table.concat(out)):gsub("^0+(%d)", "%1")
    return r
  end
  acc = mul_add(acc, 1, parts[1])
  for _ = 1, 32 do acc = mul_add(acc, 2, 0) end
  acc = mul_add(acc, 1, parts[2])
  return acc
end

--- Inverse: read an unsigned decimal u64 into the Lua integer with those bits.
function M.gm_from_decimal(str)
  local v = 0
  for i = 1, #str do
    v = v * 10 + tonumber(str:sub(i, i))   -- wraps into the signed range, bits intact
  end
  return v
end

-- ── unwrap_pid — the certified timeline primitive ───────────────────────────
--- Unwrap a rolling counter to a monotonic absolute index relative to prev_abs.
--- Forward progress dominates; a small backward delta is a reorder and steps back,
--- saturating at zero. Used for the QSS hop_index (mod 2048) on the record plane
--- and block_seq (mod 512) on the cue plane.
function M.unwrap_pid(prev_abs, raw, modulus)
  modulus = modulus or M.PID_MOD
  local prev_lo = prev_abs % modulus
  local r = raw % modulus
  local fwd = (r + modulus - prev_lo) % modulus
  if fwd <= modulus // 2 then
    return prev_abs + fwd
  end
  local back = modulus - fwd
  if back > prev_abs then return 0 end        -- saturating_sub
  return prev_abs - back
end

-- ── Mixer helpers (runtime, not byte-certified) ─────────────────────────────
-- A hub tracks one timeline per source and forwards or mixes on the grandmaster
-- clock. Alignment keys on the unwrapped counter, NEVER the 24-bit frame ts.
local Timeline = {}
Timeline.__index = Timeline

function M.new_timeline(modulus)
  return setmetatable({ modulus = modulus or M.PID_MOD, abs = 0, seen = false,
                        reorders = 0, advances = 0 }, Timeline)
end

--- Feed a rolling counter; returns the monotonic absolute index.
function Timeline:step(raw)
  if not self.seen then
    self.seen, self.abs = true, raw % self.modulus
    return self.abs
  end
  local prev = self.abs
  self.abs = M.unwrap_pid(prev, raw, self.modulus)
  if self.abs < prev then self.reorders = self.reorders + 1
  elseif self.abs > prev then self.advances = self.advances + 1 end
  return self.abs
end

M.Timeline = Timeline

--- Skew of a source against the grandmaster, in parts per million.
--- Positive means the source is running fast. Feed the source's own sample counter
--- and the grandmaster count from the latest BEACON, both at the same instant.
function M.skew_ppm(source_samples, gm_samples)
  if gm_samples == 0 then return 0.0 end
  assert(gm_samples > 0,
         "skew_ppm needs a positive grandmaster count; a negative value means the " ..
         "u64 has passed 2^63 (~6.1 million years at 48 kHz) or was misread")
  return (source_samples - gm_samples) / gm_samples * 1e6
end

--- PI servo step for a spoke disciplining its capture clock to the grandmaster.
--- Returns the new correction in ppm and the updated integrator. Gains are the
--- caller's to tune; this is deliberately a pure function so it is testable.
function M.pi_servo(err_ppm, integral, kp, ki, clamp_ppm)
  kp = kp or 0.35; ki = ki or 0.05; clamp_ppm = clamp_ppm or 200.0
  integral = integral + err_ppm
  local i_clamp = clamp_ppm / math.max(ki, 1e-9)
  if integral > i_clamp then integral = i_clamp end
  if integral < -i_clamp then integral = -i_clamp end
  local out = kp * err_ppm + ki * integral
  if out > clamp_ppm then out = clamp_ppm end
  if out < -clamp_ppm then out = -clamp_ppm end
  return out, integral
end

-- ── Self-certification on load ──────────────────────────────────────────────
-- examplePacket from Documentation/snake_vectors.json.
local ANCHOR = {
  stream_id = 5, ts_us = 0x010203, src = 0x00A1, dst = 0xFFFF,
  mode_id = 0, flags = 0x02,
  payload = { 0x00, 0x0B, 0x16, 0x21, 0x2C, 0x37, 0x42, 0x4D, 0x58, 0x63 },
  frames = {
    "d313280000a1ffff000a0200010203c0f4",
    "d313280100a1ffff000b16210102035c37",
    "d313280200a1ffff2c374 24d010203a3e0",
    "d313280300a1ffff58630000010203e40c",
  },
}

local function self_certify()
  -- (1) constants match the reference
  if M.MAX_PAYLOAD ~= M.MAX_FRAGS * 4 then return false, "max_payload" end
  if M.MAX_STREAM_ID ~= (1 << (16 - M.FRAG_BITS)) - 1 then return false, "max_stream_id" end

  -- (2) the 5:11 split places bits where the spec says
  local f = M.packetize({ 1, 2, 3, 4 }, 31, 0, 1, 2, M.MODE_LIVE, 0)
  local d0 = decode(f[1])
  if (d0.seq >> M.FRAG_BITS) ~= 31 or (d0.seq & M.FRAG_MASK) ~= 0 then
    return false, "descriptor seq split"
  end
  local d1 = decode(f[2])
  if (d1.seq & M.FRAG_MASK) ~= 1 then return false, "data seq split" end

  -- (3) packetize/reassemble is identity under reorder
  local payload = {}
  for i = 1, 800 do payload[i] = (i * 7) % 256 end
  local frames = M.packetize(payload, 5, 0x010203, 0x00A1, M.BROADCAST,
                             M.MODE_LIVE, M.FLAG_ANCHOR)
  if #frames ~= 1 + 200 then return false, "frame count" end
  local r = M.new_reassembler()
  local out
  for i = #frames, 1, -1 do out = r:push(frames[i]) or out end   -- reverse order
  if not out then return false, "reassembly under reorder" end
  if #out.payload ~= #payload then return false, "payload length" end
  for i = 1, #payload do
    if out.payload[i] ~= payload[i] then return false, "payload byte " .. i end
  end
  if out.stream_id ~= 5 or out.flags ~= M.FLAG_ANCHOR then return false, "header" end

  -- (4) clock round-trip, including a 64-bit sample count
  local c = { gm_sample_count = 0x0123456789ABCDEF,
              nominal_rate_mhz = M.NOMINAL_RATE_MHZ_48K, tx_seq = 0xBEEF, epoch = 7 }
  local got = M.unpack_clock(M.pack_clock(c))
  if got.gm_sample_count ~= c.gm_sample_count or got.tx_seq ~= c.tx_seq
     or got.nominal_rate_mhz ~= c.nominal_rate_mhz or got.epoch ~= c.epoch then
    return false, "clock round-trip"
  end
  -- the full u64 range survives as a bit pattern even past 2^63
  local maxu64 = M.gm_from_decimal("18446744073709551615")
  local rt = M.unpack_clock(M.pack_clock({ gm_sample_count = maxu64,
                                           nominal_rate_mhz = 0, tx_seq = 0, epoch = 0 }))
  if rt.gm_sample_count ~= maxu64 then return false, "u64 max round-trip" end
  if M.gm_tostring(maxu64) ~= "18446744073709551615" then return false, "gm_tostring" end
  if M.gm_tostring(0x0123456789ABCDEF) ~= "81985529216486895" then
    return false, "gm_tostring positive"
  end
  if #M.beacon_packetize(M.pack_clock(c), 0, 0, 1, M.BROADCAST, 0) ~= 5 then
    return false, "beacon is 5 frames"
  end

  -- (5) unwrap_pid: forward, wrap, reorder, saturation
  if M.unwrap_pid(0, 1, 2048) ~= 1 then return false, "unwrap forward" end
  if M.unwrap_pid(2047, 0, 2048) ~= 2048 then return false, "unwrap wrap" end
  if M.unwrap_pid(10, 9, 2048) ~= 9 then return false, "unwrap reorder" end
  if M.unwrap_pid(0, 2047, 2048) ~= 0 then return false, "unwrap saturate" end

  -- (6) the repo-wide crc16 anchor
  if M.channel_id("123456789") ~= 0x29B1 then return false, "crc16 anchor" end
  if M.channel_id(nil) ~= M.BROADCAST then return false, "empty channel" end

  return true
end

local ok, why = self_certify()
M.CERTIFIED = ok
if not ok then
  error("dcf_snake.lua FAILED self-certification: " .. tostring(why))
end

return M
