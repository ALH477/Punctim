-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  selftest_text.lua — certify lua/dcf_text.lua against the golden vectors.
--  Run:  lua lua/selftest_text.lua   (Lua 5.3+).  Exit 0 iff every check passes.
--
--  The embedded vectors are copied from Documentation/text_vectors.json — the
--  same bytes the Python, Go, C and Rust implementations certify against.
--  Cases with payload > 128 B are JSON-only (the 4092 B / 1023-frag rail), the
--  same bound gen_text_vectors.py applies to its dependency-free C header.
-- ============================================================================
local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local T = dofile(HERE .. "dcf_text.lua")

local FRAMING = {
  { src=0x0001, dst=0xFFFF, packet_id=0, ts_us=0x000000, flags=0,
    payload="",
    frames={"d31000000001ffff000000000000002cb7"} },
  { src=0x0001, dst=0xFFFF, packet_id=7, ts_us=0x012345, flags=1,
    payload="6e",
    frames={"d3101c000001ffff00010100012345a7e1","d3101c010001ffff6e000000012345caf7"} },
  { src=0x0001, dst=0xFFFF, packet_id=14, ts_us=0x02468A, flags=2,
    payload="334e3a33",
    frames={"d31038000001ffff0004020002468aa7db","d31038010001ffff334e3a3302468a7045"} },
  { src=0x0001, dst=0xFFFF, packet_id=21, ts_us=0x0369CF, flags=5,
    payload="414a4229222c60",
    frames={"d31054000001ffff000705000369cf4e29","d31054010001ffff414a42290369cfbe07","d31054020001ffff222c60000369cf8af7"} },
  { src=0x0001, dst=0xFFFF, packet_id=28, ts_us=0x048D14, flags=0,
    payload="264b5f473b79225e757b4a7a35",
    frames={"d31070000001ffff000d0000048d14e049","d31070010001ffff264b5f47048d148464","d31070020001ffff3b79225e048d142c6a","d31070030001ffff757b4a7a048d14ad75","d31070040001ffff35000000048d1483f4"} },
  { src=0x0001, dst=0xFFFF, packet_id=35, ts_us=0x05B059, flags=4,
    payload="2e43565b28403a4159722e77445d5d5539545843672f376b217a705a58684d512a3150296c64722f587d6d636a42636f684a796d5b342c5b624a677753723446",
    frames={"d3108c000001ffff0040040005b0594ad7","d3108c010001ffff2e43565b05b059e1a9","d3108c020001ffff28403a4105b0596a08","d3108c030001ffff59722e7705b059779d","d3108c040001ffff445d5d5505b0597bde","d3108c050001ffff3954584305b059414b","d3108c060001ffff672f376b05b059d45d","d3108c070001ffff217a705a05b059b495","d3108c080001ffff58684d5105b059aa94","d3108c090001ffff2a31502905b0599783","d3108c0a0001ffff6c64722f05b059b6ee","d3108c0b0001ffff587d6d6305b0593fbb","d3108c0c0001ffff6a42636f05b059648b","d3108c0d0001ffff684a796d05b059a5c3","d3108c0e0001ffff5b342c5b05b059d979","d3108c0f0001ffff624a677705b059e899","d3108c100001ffff5372344605b059aeb5"} },
  { src=0x0001, dst=0xFFFF, packet_id=42, ts_us=0x06D39E, flags=1,
    payload="5424507e6752502b717e6c5332744e4b3341202a7e5e596c5c295f772d40483c605c624773743b39413a7872483538742d332b60283430524b663f49622c3a443f733c51253074562f434e682f6c7c43732d3b7d3775593533467e223c3e667c40672d624e5e62512e53705e37716c7a413b5479575b334022205176",
    frames={"d310a8000001ffff007c010006d39ecba8","d310a8010001ffff5424507e06d39ec116","d310a8020001ffff6752502b06d39e4485","d310a8030001ffff717e6c5306d39eaf36","d310a8040001ffff32744e4b06d39ef642","d310a8050001ffff3341202a06d39ed8c9","d310a8060001ffff7e5e596c06d39ec9c8","d310a8070001ffff5c295f7706d39e8148","d310a8080001ffff2d40483c06d39eaa5c","d310a8090001ffff605c624706d39e9dd8","d310a80a0001ffff73743b3906d39e1295","d310a80b0001ffff413a787206d39e4d04","d310a80c0001ffff4835387406d39e9d71","d310a80d0001ffff2d332b6006d39ee286","d310a80e0001ffff2834305206d39e7fdb","d310a80f0001ffff4b663f4906d39e8ca3","d310a8100001ffff622c3a4406d39e1be9","d310a8110001ffff3f733c5106d39ec7f1","d310a8120001ffff2530745606d39ee0c3","d310a8130001ffff2f434e6806d39e5f77","d310a8140001ffff2f6c7c4306d39ec60b","d310a8150001ffff732d3b7d06d39eaa3d","d310a8160001ffff3775593506d39e785e","d310a8170001ffff33467e2206d39e7e6b","d310a8180001ffff3c3e667c06d39e7ea9","d310a8190001ffff40672d6206d39ed7ca","d310a81a0001ffff4e5e625106d39e4417","d310a81b0001ffff2e53705e06d39e2e0c","d310a81c0001ffff37716c7a06d39ef676","d310a81d0001ffff413b547906d39ea94a","d310a81e0001ffff575b334006d39ec405","d310a81f0001ffff2220517606d39eeaa7"} },
}

local REASM = {
  { name="in_order",
    input={"d310140000010002001501000102033a9f","d31014010001000268656c6c0102037287","d3101402000100026f206f76010203da93","d31014030001000265722044010203646c","d310140400010002654d6f64010203c577","d3101405000100024672616d010203a626","d3101406000100026500000001020308c8","d310180000010002000605000102104b58","d3101801000100027265706c0102101566","d310180200010002792100000102103c01"},
    messages={{packet_id=5,ts_us=0x010203,src=0x0001,dst=0x0002,flags=1,payload="68656c6c6f206f7665722044654d6f644672616d65"},{packet_id=6,ts_us=0x010210,src=0x0001,dst=0x0002,flags=5,payload="7265706c7921"}},
    lost={} },
  { name="reordered",
    input={"d31014030001000265722044010203646c","d3101406000100026500000001020308c8","d310180200010002792100000102103c01","d3101405000100024672616d010203a626","d310140400010002654d6f64010203c577","d3101402000100026f206f76010203da93","d310180000010002000605000102104b58","d31014010001000268656c6c0102037287","d3101801000100027265706c0102101566","d310140000010002001501000102033a9f"},
    messages={{packet_id=6,ts_us=0x010210,src=0x0001,dst=0x0002,flags=5,payload="7265706c7921"},{packet_id=5,ts_us=0x010203,src=0x0001,dst=0x0002,flags=1,payload="68656c6c6f206f7665722044654d6f644672616d65"}},
    lost={} },
  { name="frag_drop_lost",
    input={"d310140000010002001501000102033a9f","d31014010001000268656c6c0102037287","d31014030001000265722044010203646c","d310140400010002654d6f64010203c577","d3101405000100024672616d010203a626","d3101406000100026500000001020308c8","d310180000010002000605000102104b58","d3101801000100027265706c0102101566","d310180200010002792100000102103c01"},
    messages={{packet_id=6,ts_us=0x010210,src=0x0001,dst=0x0002,flags=5,payload="7265706c7921"}},
    lost={5} },
  { name="duplicate",
    input={"d310140000010002001501000102033a9f","d310140000010002001501000102033a9f","d31014010001000268656c6c0102037287","d31014010001000268656c6c0102037287","d3101402000100026f206f76010203da93","d31014030001000265722044010203646c","d310140400010002654d6f64010203c577","d3101405000100024672616d010203a626","d3101406000100026500000001020308c8","d310180000010002000605000102104b58","d3101801000100027265706c0102101566","d310180200010002792100000102103c01"},
    messages={{packet_id=5,ts_us=0x010203,src=0x0001,dst=0x0002,flags=1,payload="68656c6c6f206f7665722044654d6f644672616d65"},{packet_id=6,ts_us=0x010210,src=0x0001,dst=0x0002,flags=5,payload="7265706c7921"}},
    lost={} },
}

-- ── harness ─────────────────────────────────────────────────────────────────
local fail = 0
local function chk(c, msg) if not c then print("  FAIL  " .. msg); fail = fail + 1 end end

local function unhex(h)
  local t = {}
  for i = 1, #h, 2 do t[#t + 1] = string.char(tonumber(h:sub(i, i + 1), 16)) end
  return table.concat(t)
end
local function tohex(s)
  return (s:gsub(".", function(c) return ("%02x"):format(c:byte()) end))
end

chk(T.CERTIFIED, "dcf_text.lua did not self-certify on load")

-- Law A: packetize reproduces the golden framing bytes exactly
for i, c in ipairs(FRAMING) do
  local fr = T.packetize(unhex(c.payload), c.packet_id, c.ts_us, c.src, c.dst, c.flags)
  chk(#fr == #c.frames, ("framing %d: %d frames ~= %d"):format(i, #fr, #c.frames))
  for k, f in ipairs(fr) do
    chk(T.frame_to_hex(f) == c.frames[k],
        ("framing %d frame %d: %s ~= %s"):format(i, k, T.frame_to_hex(f), c.frames[k]))
  end
end
print(("  PASS  %d framing cases reproduce golden bytes"):format(#FRAMING))

-- Law B: reassembly under in-order / reorder / fragment-drop / duplicate
for _, rc in ipairs(REASM) do
  local r, msgs, lost = T.new_reassembler(), {}, {}
  for _, h in ipairs(rc.input) do
    for _, e in ipairs(r:push(T.hex_to_frame(h))) do msgs[#msgs + 1] = e end
  end
  for _, e in ipairs(r:finalize()) do lost[#lost + 1] = e.packet_id end

  chk(#msgs == #rc.messages, ("%s: %d messages ~= %d"):format(rc.name, #msgs, #rc.messages))
  for k, m in ipairs(rc.messages) do
    local g = msgs[k]
    chk(g ~= nil, ("%s: missing message %d"):format(rc.name, k))
    if g then
      chk(tohex(g.text) == m.payload,
          ("%s msg %d payload %s ~= %s"):format(rc.name, k, tohex(g.text), m.payload))
      chk(g.packet_id == m.packet_id and g.src == m.src and g.dst == m.dst
          and g.flags == m.flags and g.ts_us == m.ts_us,
          ("%s msg %d header mismatch"):format(rc.name, k))
    end
  end
  chk(#lost == #rc.lost, ("%s: %d lost ~= %d"):format(rc.name, #lost, #rc.lost))
  for k, pid in ipairs(rc.lost) do
    chk(lost[k] == pid, ("%s lost[%d]: %s ~= %d"):format(rc.name, k, tostring(lost[k]), pid))
  end
end
print(("  PASS  %d reassembly cases (in-order / reorder / drop->lost / duplicate)"):format(#REASM))

-- Law C: bounds + rendezvous anchors
chk(not pcall(T.packetize, string.rep("x", T.MAX_PAYLOAD + 1), 0, 0, 1, 1), "oversize must be rejected")
chk(not pcall(T.packetize, "x", T.MAX_PACKETID + 1, 0, 1, 1), "packet_id overflow must be rejected")
chk(T.channel_id("123456789") == 0x29B1, "crc16 rendezvous anchor")
chk(T.channel_id(nil) == T.BROADCAST and T.channel_id("") == T.BROADCAST, "empty channel = broadcast")
print("  PASS  bounds + crc16 rendezvous anchors (\"123456789\" -> 0x29B1)")

if fail == 0 then
  print("ALL TEXT LAWS HOLD — lua/dcf_text.lua CERTIFIED")
  os.exit(0)
else
  print(("%d FAILURES"):format(fail))
  os.exit(1)
end
