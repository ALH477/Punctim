-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_text.lua — DCF-Text L2 framing + channel rendezvous (pure Lua).
--
--  The canonical Lua port of the CERTIFIED DCF-Text adapter from this repo
--  (python/MCP/textlab_core.py, go/text/text.go); the 17-byte DeModFrame codec is
--  byte-identical to GUI/wirelab.lua / lua/dcf_audio.lua / wirelab_core.py and the
--  C and Rust references. Text is an *adapter* over the wire quantum: one UTF-8
--  message is serialised into 1 + frag_total ordinary DATA frames.
--
--  L2 layout (all frames version=1, type=DATA(0); big-endian):
--    seq (u16) = packet_id[15:10] (6 bits) | frag_idx[9:0] (10 bits)
--    frag_idx 0  descriptor : payload = [len_hi, len_lo, flags, reserved]
--    frag_idx k  data       : payload = bytes[(k-1)*4 .. +4]  (last frame zero-padded)
--    frag_total = ceil(len/4)   (<= 1023  =>  len <= 4092 bytes / message)
--
--  Why text takes DATA(0) with a 10-bit fragment index while audio takes CTRL(3)
--  with a 5-bit one: chat messages are larger and far lower-rate than 20 ms audio
--  blocks, so the seq split trades packet_id room (6 bits) for fragment room.
--  A text fragment is told apart from a game fragment by that seq split; the
--  descriptor that starts every message disambiguates which adapter owns the id.
--
--  Channel rendezvous (handshakeless): identical to dcf_audio.lua — the frame dst
--  field IS the channel. crc16 of a channel name, or 0xFFFF for broadcast. No wire
--  change; these are ordinary, certified DeModFrames.
--
--  Self-certifies on load (M.CERTIFIED) against the committed anchor from
--  Documentation/text_vectors.json. Requires Lua 5.3+ (native bitwise ops), which
--  the demod-ui host provides.
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

-- ── L2 constants (mirror textlab_core.py exactly) ───────────────────────────
M.FDATA        = 0
M.FRAG_BITS    = 10
M.FRAG_MASK    = 0x3FF
M.MAX_FRAGS    = 1023
M.MAX_PAYLOAD  = 4092           -- bytes / message
M.MAX_PACKETID = 63
M.BROADCAST    = 0xFFFF
M.FLAG_AGENT, M.FLAG_MORE, M.FLAG_RELIABLE = 0x01, 0x02, 0x04

-- ── Channel rendezvous (channel = frame dst) ────────────────────────────────
-- Derive a u16 channel from a name using the certified crc16, so peers who agree
-- on a word land on the same handshakeless channel. nil/"" => broadcast (0xFFFF,
-- which is also crc16 of the empty string — the two agree by construction).
function M.channel_id(name)
  if name == nil or name == "" then return M.BROADCAST end
  local b = {}
  for i = 1, #name do b[i] = string.byte(name, i) end
  return crc16(b, #name)
end

function M.accepts(frame_dst, my_channel)
  return frame_dst == my_channel or frame_dst == M.BROADCAST
end

-- ── L2: packetize ───────────────────────────────────────────────────────────
-- text: a Lua string (raw bytes; UTF-8 is carried transparently — Lua strings are
-- byte strings, so no transcoding happens and multibyte sequences survive intact).
-- Returns { frame, ... }: descriptor first, then frag_total data frames.
function M.packetize(text, packet_id, ts_us, src, dst, flags)
  flags = flags or 0
  local n = #text
  assert(n <= M.MAX_PAYLOAD,
         ("message %dB exceeds %dB cap (split into multiple messages)"):format(n, M.MAX_PAYLOAD))
  assert(packet_id >= 0 and packet_id <= M.MAX_PACKETID,
         ("packet_id must be 0..%d"):format(M.MAX_PACKETID))
  assert(flags >= 0 and flags < 256, "flags must be u8")

  local frag_total = (n + 3) // 4
  local frames = {}

  -- frag_idx 0 — descriptor (carries the true byte length so we can unpad)
  frames[1] = encode(M.FDATA, (packet_id << M.FRAG_BITS) | 0, src, dst,
                     { (n >> 8) & 0xFF, n & 0xFF, flags, 0 }, ts_us)

  -- frag_idx 1..frag_total — data, last chunk zero-padded to 4 bytes
  for k = 1, frag_total do
    local chunk = {}
    for i = 1, 4 do
      local off = (k - 1) * 4 + i
      chunk[i] = (off <= n) and string.byte(text, off) or 0
    end
    frames[#frames + 1] = encode(M.FDATA, (packet_id << M.FRAG_BITS) | k,
                                 src, dst, chunk, ts_us)
  end
  return frames
end

-- ── L2: stateful reassembly ─────────────────────────────────────────────────
-- push(frame) returns a (possibly empty) list of events, emitting a message as
-- soon as its descriptor and every data fragment have arrived. Duplicates are
-- ignored. finalize() reports every still-incomplete message as lost.
--   message event = { kind="message", packet_id, ts_us, src, dst, text, flags }
--   lost event    = { kind="lost",    packet_id }
local Reassembler = {}
Reassembler.__index = Reassembler

-- accept_dst: nil => accept every channel; else only this dst + BROADCAST.
function M.new_reassembler(accept_dst)
  return setmetatable({ _accept = accept_dst, _pkts = {} }, Reassembler)
end

function Reassembler:push(frame)
  local d = decode(frame)
  if not d then return {} end
  if d.ftype ~= M.FDATA then return {} end
  if self._accept ~= nil and not M.accepts(d.dst, self._accept) then return {} end

  local packet_id = d.seq >> M.FRAG_BITS
  local frag_idx  = d.seq & M.FRAG_MASK

  local e = self._pkts[packet_id]
  if not e then
    e = { desc = nil, frags = {}, n_frags = 0 }
    self._pkts[packet_id] = e
  end
  e.ts, e.src, e.dst = d.ts, d.src, d.dst

  if frag_idx == 0 then
    if not e.desc then
      local len = (d.payload[1] << 8) | d.payload[2]
      e.desc = { len = len, flags = d.payload[3], frag_total = (len + 3) // 4 }
    end
  elseif not e.frags[frag_idx] then
    e.frags[frag_idx] = d.payload
    e.n_frags = e.n_frags + 1
  end

  return self:_try_emit(packet_id)
end

function Reassembler:_try_emit(packet_id)
  local e = self._pkts[packet_id]
  if not e or not e.desc then return {} end
  if e.n_frags < e.desc.frag_total then return {} end
  for k = 1, e.desc.frag_total do
    if not e.frags[k] then return {} end
  end

  local out = {}
  for k = 1, e.desc.frag_total do
    local p = e.frags[k]
    out[#out + 1] = string.char(p[1], p[2], p[3], p[4])
  end
  local text = table.concat(out):sub(1, e.desc.len)
  self._pkts[packet_id] = nil
  return { { kind = "message", packet_id = packet_id, ts_us = e.ts,
             src = e.src, dst = e.dst, text = text, flags = e.desc.flags } }
end

-- Report every still-incomplete message as lost (ascending packet_id), clearing
-- state. Mirrors TextReassembler.finalize in the Python/Go/C references.
function Reassembler:finalize()
  local ids = {}
  for pid in pairs(self._pkts) do ids[#ids + 1] = pid end
  table.sort(ids)
  local events = {}
  for _, pid in ipairs(ids) do
    events[#events + 1] = { kind = "lost", packet_id = pid }
  end
  self._pkts = {}
  return events
end

-- ── Self-certification on load ──────────────────────────────────────────────
-- The anchor is exampleTextMessage from Documentation/text_vectors.json — the same
-- bytes the Python, Go, C and Rust implementations are certified against.
local ANCHOR = {
  packet_id = 0x0A, ts_us = 0x010203, src = 0x00A1, dst = 0xEED7, flags = 0x05,
  text = "agent \u{21C4} agent \u{1F680}",
  frames = {
    "d310280000a1eed7001405000102035490",
    "d310280100a1eed76167656e01020335b6",
    "d310280200a1eed77420e287010203ee3d",
    "d310280300a1eed7842061670102038a38",
    "d310280400a1eed7656e7420010203d0fe",
    "d310280500a1eed7f09f9a80010203fc60",
  },
}
M.ANCHOR = ANCHOR

local function self_certify()
  -- (1) packetize reproduces the golden frames byte-for-byte
  local frames = M.packetize(ANCHOR.text, ANCHOR.packet_id, ANCHOR.ts_us,
                             ANCHOR.src, ANCHOR.dst, ANCHOR.flags)
  if #frames ~= #ANCHOR.frames then
    return false, ("frame count %d ~= %d"):format(#frames, #ANCHOR.frames)
  end
  for i, f in ipairs(frames) do
    local got = frame_to_hex(f)
    if got ~= ANCHOR.frames[i] then
      return false, ("frame %d: %s ~= %s"):format(i, got, ANCHOR.frames[i])
    end
  end

  -- (2) the golden frames reassemble back to the anchor text
  local r = M.new_reassembler()
  local events = {}
  for _, h in ipairs(ANCHOR.frames) do
    for _, ev in ipairs(r:push(hex_to_frame(h))) do events[#events + 1] = ev end
  end
  if #events ~= 1 or events[1].kind ~= "message" then
    return false, "reassembly did not emit exactly one message"
  end
  local m = events[1]
  if m.text ~= ANCHOR.text then return false, "reassembled text mismatch" end
  if m.src ~= ANCHOR.src or m.dst ~= ANCHOR.dst or m.flags ~= ANCHOR.flags
     or m.packet_id ~= ANCHOR.packet_id or m.ts_us ~= ANCHOR.ts_us then
    return false, "reassembled header mismatch"
  end
  if #r:finalize() ~= 0 then return false, "finalize should be empty" end

  -- (3) channel rendezvous anchor — the repo-wide crc16 anchor
  if M.channel_id("123456789") ~= 0x29B1 then return false, "crc16 anchor" end
  if M.channel_id(nil) ~= M.BROADCAST then return false, "nil channel" end

  return true
end

local ok, why = self_certify()
M.CERTIFIED = ok
if not ok then
  error("dcf_text.lua FAILED self-certification: " .. tostring(why))
end

return M
