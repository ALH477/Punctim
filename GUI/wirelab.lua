-- ============================================================================
-- DCF WIRELAB — DeMoD UI front panel for the 17-byte wire quantum (DeModFrame)
-- Run:  ./demod-ui wirelab.lua
--
-- Pure-Lua reference codec (CRC-16/CCITT-FALSE, poly 0x1021, init 0xFFFF),
-- bit-identical to wirelab_core.py / the fixed Punctim crc16-ccitt / the
-- Haskell crc16ccitt. Self-certifies on launch against the golden anchors:
--   crc("123456789")            = 0x29B1
--   crc(exampleFrame body)      = 0xA963
--   full exampleFrame           = D31012340001FFFFDEADBEEFAB12CDA963
-- Companion: wirelab_mcp.py (agent face), golden_vectors.json (certificate).
-- ============================================================================

-- ---------------------------------------------------------------- codec ----
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
  w[1]  = SYNC
  w[2]  = ((VERSION & 0xF) << 4) | (ftype & 0xF)
  w[3]  = (seq >> 8) & 0xFF;  w[4]  = seq & 0xFF
  w[5]  = (src >> 8) & 0xFF;  w[6]  = src & 0xFF
  w[7]  = (dst >> 8) & 0xFF;  w[8]  = dst & 0xFF
  for i = 1, 4 do w[8 + i] = payload[i] & 0xFF end
  w[13] = (ts >> 16) & 0xFF; w[14] = (ts >> 8) & 0xFF; w[15] = ts & 0xFF
  local c = crc16(w, 15)
  w[16] = (c >> 8) & 0xFF; w[17] = c & 0xFF
  return w
end

-- returns fields-table or nil, reason
local function decode(w)
  if #w ~= FRAME_LEN then return nil, ("length %d != 17"):format(#w) end
  if w[1] ~= SYNC then return nil, "bad sync byte" end
  if (w[2] >> 4) ~= VERSION then return nil, "bad version nibble" end
  if crc16(w, 15) ~= ((w[16] << 8) | w[17]) then return nil, "CRC mismatch" end
  return { ftype = w[2] & 0xF,
           seq = (w[3] << 8) | w[4], src = (w[5] << 8) | w[6],
           dst = (w[7] << 8) | w[8],
           payload = { w[9], w[10], w[11], w[12] },
           ts = (w[13] << 16) | (w[14] << 8) | w[15],
           crc = (w[16] << 8) | w[17] }
end

local function frame_to_hex(w)
  local t = {}
  for i = 1, #w do t[i] = ("%02X"):format(w[i]) end
  return table.concat(t)
end

local function hex_to_frame(s)
  s = s:gsub("[%s:,]", ""):gsub("0[xX]", "")
  if #s ~= 34 or s:find("%X") then return nil end
  local w = {}
  for i = 1, 17 do w[i] = tonumber(s:sub(i * 2 - 1, i * 2), 16) end
  return w
end

-- ------------------------------------------------------------ self-cert ----
local CERTIFIED = false
do
  local anchor = {}
  for i = 1, 9 do anchor[i] = string.byte("123456789", i) end
  local ex = encode(0, 0x1234, 0x0001, 0xFFFF, {0xDE, 0xAD, 0xBE, 0xEF}, 0xAB12CD)
  CERTIFIED = crc16(anchor, 9) == 0x29B1
          and frame_to_hex(ex) == "D31012340001FFFFDEADBEEFAB12CDA963"
          and decode(ex) ~= nil
end

-- ------------------------------------------------------------------- ui ----
local TYPE_NAMES = { [0] = "FData", [1] = "FAck", [2] = "FBeacon", [3] = "FCtrl" }
local S = { frame = nil, fields = nil, status = "", lamp = "idle", audit = nil }

local root = dm.root()
root:set_layout("vbox", 6, 12)

local title = dm.label("title", "DCF WIRELAB  //  17-BYTE WIRE QUANTUM")
title:set_fg(0x8B, 0x5C, 0xF6); title:set_bounds(0, 0, 0, 28)
root:add_child(title)

local sub = dm.label("sub", CERTIFIED and "codec CERTIFIED: 0x29B1 / 0xA963 anchors hold"
                                       or "CODEC SELF-TEST FAILED — do not trust output")
sub:set_fg(CERTIFIED and 0x4C or 0xFF, CERTIFIED and 0xFF or 0x4C,
           CERTIFIED and 0x82 or 0x6A)
sub:set_bounds(0, 0, 0, 18)
root:add_child(sub)

-- field inputs ---------------------------------------------------------------
local row1 = dm.panel("row1"); row1:set_layout("hbox", 6, 4); row1:set_bounds(0, 0, 0, 40)
row1:set_bg(0x1A, 0x1A, 0x2E)
local dd_type = dm.dropdown("ftype", "type")
for i = 0, 3 do dd_type:add_item(("%d %s"):format(i, TYPE_NAMES[i])) end
local in_seq = dm.text_input("seq", "seq hex (1234)")
local in_src = dm.text_input("src", "src hex (0001)")
local in_dst = dm.text_input("dst", "dst hex (FFFF)")
for _, w in ipairs({ dd_type, in_seq, in_src, in_dst }) do
  w:set_bounds(0, 0, 150, 32); row1:add_child(w)
end
root:add_child(row1)

local row2 = dm.panel("row2"); row2:set_layout("hbox", 6, 4); row2:set_bounds(0, 0, 0, 40)
row2:set_bg(0x1A, 0x1A, 0x2E)
local in_pay = dm.text_input("pay", "payload 8 hex (DEADBEEF)")
local in_ts  = dm.text_input("ts",  "ts_us 24-bit hex (AB12CD)")
in_pay:set_bounds(0, 0, 230, 32); in_ts:set_bounds(0, 0, 230, 32)
row2:add_child(in_pay); row2:add_child(in_ts)
root:add_child(row2)

local hexio = dm.text_input("hexio", "frame hex in/out (34 chars)")
hexio:set_bounds(0, 0, 0, 32)
root:add_child(hexio)

local status = dm.label("status", "ready")
status:set_fg(0xE8, 0xE8, 0xF0); status:set_bounds(0, 0, 0, 20)
root:add_child(status)

local function set_status(text, kind)
  S.status, S.lamp = text, kind
  status:set_text(text)
  if kind == "ok" then status:set_fg(0x4C, 0xFF, 0x82)
  elseif kind == "err" then status:set_fg(0xFF, 0x4C, 0x6A)
  else status:set_fg(0xFF, 0xD9, 0x4C) end
  dm.redraw()
end

local function read_hex(widget, bits, default)
  local v = tonumber((widget:get_value() or ""):gsub("%s", ""), 16)
  if v == nil then return default end
  return v & ((1 << bits) - 1)
end

-- buttons --------------------------------------------------------------------
local row3 = dm.panel("row3"); row3:set_layout("hbox", 6, 4); row3:set_bounds(0, 0, 0, 44)
row3:set_bg(0x1A, 0x1A, 0x2E)

local function do_encode()
  local pv, pay = (in_pay:get_value() or ""):gsub("%s", ""), { 0, 0, 0, 0 }
  if #pv > 0 then
    if #pv ~= 8 or pv:find("%X") then return set_status("payload must be 8 hex chars", "err") end
    for i = 1, 4 do pay[i] = tonumber(pv:sub(i * 2 - 1, i * 2), 16) end
  end
  local item = dd_type:get_value()
  local ftype = tonumber(type(item) == "string" and item:match("^%d") or item) or 0
  S.frame = encode(ftype, read_hex(in_seq, 16, 0), read_hex(in_src, 16, 0),
                   read_hex(in_dst, 16, 0xFFFF), pay, read_hex(in_ts, 24, 0))
  S.fields, S.audit = decode(S.frame), nil
  hexio:set_text(frame_to_hex(S.frame))
  set_status(("encoded %s  seq=%04X  crc=%04X"):format(
    TYPE_NAMES[S.fields.ftype] or "?", S.fields.seq, S.fields.crc), "ok")
end

local function do_decode()
  local w = hex_to_frame(hexio:get_value() or "")
  if not w then return set_status("need exactly 17 bytes of hex", "err") end
  S.frame, S.audit = w, nil
  local f, reason = decode(w)
  S.fields = f
  if f then
    set_status(("VALID  %s  seq=%04X  %04X->%04X  ts=%06X"):format(
      TYPE_NAMES[f.ftype] or ("type " .. f.ftype), f.seq, f.src, f.dst, f.ts), "ok")
  else
    set_status("REJECTED: " .. reason, "err")
  end
end

local function do_audit()
  if not (S.frame and S.fields) then return set_status("encode or decode a valid frame first", "err") end
  local accepted = 0
  for bit = 0, FRAME_LEN * 8 - 1 do
    local bad = {}
    for i = 1, FRAME_LEN do bad[i] = S.frame[i] end
    local bi = (bit // 8) + 1
    bad[bi] = bad[bi] ~ (1 << (7 - bit % 8))
    if decode(bad) then accepted = accepted + 1 end
  end
  S.audit = accepted
  if accepted == 0 then set_status("AUDIT: all 136 single-bit corruptions rejected", "ok")
  else set_status(("AUDIT FAILED: %d corruptions accepted"):format(accepted), "err") end
end

local function do_example()
  hexio:set_text("D31012340001FFFFDEADBEEFAB12CDA963")
  do_decode()
end

local buttons = { { "ENCODE", do_encode }, { "DECODE", do_decode },
                  { "AUDIT 136", do_audit }, { "EXAMPLE", do_example } }
for _, spec in ipairs(buttons) do
  local b = dm.button("btn_" .. spec[1], spec[1])
  b:set_bounds(0, 0, 140, 36)
  b:on_click(function() spec[2]() end)
  row3:add_child(b)
end
root:add_child(row3)

-- ------------------------------------------------------- custom overlay ----
-- field spans (byte index 1..17) -> color; CRC cells flip green/red live
local FIELD_SPANS = {
  { 1, 1,  0x8B, 0x5C, 0xF6, "SYNC" }, { 2, 2,  0x8B, 0x5C, 0xF6, "VER|TYPE" },
  { 3, 4,  0x00, 0xF5, 0xD4, "SEQ" },  { 5, 6,  0x4C, 0xFF, 0x82, "SRC" },
  { 7, 8,  0xFF, 0xD9, 0x4C, "DST" },  { 9, 12, 0xE8, 0xE8, 0xF0, "PAYLOAD" },
  { 13, 15, 0x8B, 0x5C, 0xF6, "TS_US" }, { 16, 17, 0x00, 0xF5, 0xD4, "CRC16" },
}

function on_update(dt)
  dm.redraw()  -- lamp pulse + grid stay live (demod-ui needs explicit redraws)
end

function on_draw()
  local gx, gy = 16, 248
  local cell, gap = 38, 4
  -- byte grid
  for i = 1, FRAME_LEN do
    local x = gx + (i - 1) * (cell + gap)
    local r, g, b = 0x2A, 0x2A, 0x3E
    for _, sp in ipairs(FIELD_SPANS) do
      if i >= sp[1] and i <= sp[2] then r, g, b = sp[3], sp[4], sp[5] end
    end
    if i >= 16 and S.frame then
      if S.fields then r, g, b = 0x4C, 0xFF, 0x82 else r, g, b = 0xFF, 0x4C, 0x6A end
    end
    dm.draw.rect(x, gy, cell, 30, 0x1A, 0x1A, 0x2E)
    dm.draw.rect(x, gy + 30, cell, 3, r, g, b)
    local txt = S.frame and ("%02X"):format(S.frame[i]) or "--"
    dm.draw.text(x + 11, gy + 8, txt, r, g, b)
    dm.draw.text(x + 11, gy + 38, ("%02d"):format(i - 1), 0x2A + 0x40, 0x2A + 0x40, 0x3E + 0x40)
  end
  -- legend
  local lx = gx
  for _, sp in ipairs(FIELD_SPANS) do
    dm.draw.rect(lx, gy + 58, 10, 10, sp[3], sp[4], sp[5])
    dm.draw.text(lx + 14, gy + 56, sp[6], 0xE8, 0xE8, 0xF0)
    lx = lx + 14 + 9 * #sp[6] + 16
  end
  -- validity lamp (pulse when valid)
  local pulse = 0.5 + 0.5 * math.sin(dm.time() * 4.0)
  local lampx, lampy = dm.width() - 120, gy + 4
  if S.lamp == "ok" then
    dm.draw.circle(lampx, lampy + 10, 9 + 3 * pulse, 0x4C, 0xFF, 0x82, 90)
    dm.draw.circle(lampx, lampy + 10, 7, 0x4C, 0xFF, 0x82)
  elseif S.lamp == "err" then
    dm.draw.circle(lampx, lampy + 10, 7, 0xFF, 0x4C, 0x6A)
  else
    dm.draw.circle(lampx, lampy + 10, 7, 0xFF, 0xD9, 0x4C)
  end
  -- Sierpinski Trinity seal, glow keyed to certification (depth 3: real-time safe)
  local sxc, syc = dm.width() - 70, 64
  dm.draw.sierpinski_glow(sxc - 34, syc + 30, sxc + 34, syc + 30, sxc, syc - 30, 3,
    { 0x0A, 0x0A, 0x0F, 255 },
    { 0x8B, 0x5C, 0xF6, 255 },
    CERTIFIED and { 0x00, 0xF5, 0xD4, 255 } or { 0xFF, 0x4C, 0x6A, 255 },
    math.floor(4 + 3 * pulse))
end
