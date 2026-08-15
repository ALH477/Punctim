-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  selftest_snake.lua — certify lua/dcf_snake.lua against the golden vectors.
--  Run:  lua lua/selftest_snake.lua   (Lua 5.3+).  Exit 0 iff every check passes.
--
--  Vectors copied from Documentation/snake_vectors.json — the same bytes the C
--  and Rust implementations certify against. Framing payloads over 256 B stay
--  JSON-only (the 8188 B / 2047-frag rail).
--
--  gm_sample_count is carried as a DECIMAL STRING, not a number: it is a u64, and
--  a value at or above 2^63 cannot survive a Lua integer literal or a float.
-- ============================================================================
local HERE = (debug.getinfo(1, "S").source:gsub("^@%s*", "")):match("(.*/)") or "./"
local S = dofile(HERE .. "dcf_snake.lua")

local CONST = {["frag_bits"]=11,["max_frags"]=2047,["max_payload"]=8188,["max_stream_id"]=31,["frame_type_ctrl"]=3,["frame_type_beacon"]=2,["mode_live"]=0,["mode_near"]=1,["mode_relaxed"]=2,["flag_more"]=1,["flag_anchor"]=2,["flag_end"]=4,["clock_len"]=16,["clock_ver"]=1,["nominal_rate_mhz_48k"]=48000000,["pid_mod"]=2048}
local ANCHOR = {["stream_id"]=5,["ts_us"]=66051,["src"]=161,["dst"]=24152,["mode_id"]=0,["flags"]=2,["payload"]="a55a0000000100050003000adededededededede",["frames"]={"d313280000a15e580014000201020378ff","d313280100a15e58a55a0000010203c0f9","d313280200a15e580001000501020376bd","d313280300a15e580003000a0102032a66","d313280400a15e58dededede0102032184","d313280500a15e58dededede01020322f1"}}
local FRAMING = {{["src"]=1,["dst"]=65535,["stream_id"]=0,["ts_us"]=0,["mode_id"]=0,["flags"]=0,["payload"]="",["frames"]={"d31300000001ffff00000000000000a114"}},{["src"]=1,["dst"]=65535,["stream_id"]=3,["ts_us"]=74565,["mode_id"]=1,["flags"]=2,["payload"]="5b",["frames"]={"d31318000001ffff00010102012345c287","d31318010001ffff5b00000001234562d3"}},{["src"]=1,["dst"]=65535,["stream_id"]=6,["ts_us"]=149130,["mode_id"]=2,["flags"]=1,["payload"]="31678ec3",["frames"]={"d31330000001ffff0004020102468a5796","d31330010001ffff31678ec302468ad3c0"}},{["src"]=1,["dst"]=65535,["stream_id"]=9,["ts_us"]=223695,["mode_id"]=0,["flags"]=6,["payload"]="fae84944cd9d60",["frames"]={"d31348000001ffff000700060369cfdf07","d31348010001fffffae849440369cf747f","d31348020001ffffcd9d60000369cf3311"}},{["src"]=1,["dst"]=65535,["stream_id"]=12,["ts_us"]=298260,["mode_id"]=1,["flags"]=0,["payload"]="aabe5fcd75c300cbd807d1f7d3",["frames"]={"d31360000001ffff000d0100048d14d10f","d31360010001ffffaabe5fcd048d1481cf","d31360020001ffff75c300cb048d14135a","d31360030001ffffd807d1f7048d1479d3","d31360040001ffffd3000000048d14a125"}},{["src"]=1,["dst"]=65535,["stream_id"]=15,["ts_us"]=372825,["mode_id"]=2,["flags"]=4,["payload"]="335eb0acc59629cd24ef0d3142401637dfa0909043fddc12b95d7616367cc79c7c309a36fc7cbb8364cf0e25cd9642d6df7f45326611a4b8f404a89c6133f538c8c4162298a2e6e5fc1d02a014bda00408c9f8a60eea368b9894ddfa96ceadbaf753f43cb4142bfe27fa2af48abf7021b2121e1653f611f001f8534e",["frames"]={"d31378000001ffff007c020405b059356e","d31378010001ffff335eb0ac05b0590c54","d31378020001ffffc59629cd05b0599594","d31378030001ffff24ef0d3105b0596ad0","d31378040001ffff4240163705b0599bb3","d31378050001ffffdfa0909005b0593178","d31378060001ffff43fddc1205b0596eca","d31378070001ffffb95d761605b059f67a","d31378080001ffff367cc79c05b05985dd","d31378090001ffff7c309a3605b0597f6c","d313780a0001fffffc7cbb8305b0595f88","d313780b0001ffff64cf0e2505b059a618","d313780c0001ffffcd9642d605b059e634","d313780d0001ffffdf7f453205b05958cc","d313780e0001ffff6611a4b805b0597f9e","d313780f0001fffff404a89c05b059af9b","d31378100001ffff6133f53805b0599d0f","d31378110001ffffc8c4162205b0598f96","d31378120001ffff98a2e6e505b0596fb8","d31378130001fffffc1d02a005b059406d","d31378140001ffff14bda00405b0596c20","d31378150001ffff08c9f8a605b0595979","d31378160001ffff0eea368b05b059f4f3","d31378170001ffff9894ddfa05b059ec2c","d31378180001ffff96ceadba05b05948cb","d31378190001fffff753f43c05b05987cd","d313781a0001ffffb4142bfe05b05929ba","d313781b0001ffff27fa2af405b059a7ae","d313781c0001ffff8abf702105b059ee2b","d313781d0001ffffb2121e1605b05923a5","d313781e0001ffff53f611f005b059cdab","d313781f0001ffff01f8534e05b059c269"}}}
local REASM = {{["name"]="in_order",["input_frames"]={"d313280000010002001e0002010203068c","d313280100010002000b16210102033931","d3132802000100022c37424d010203ae88","d31328030001000258636e79010203fbe8","d313280400010002848f9aa5010203311c","d313280500010002b0bbc6d10102039592","d313280600010002dce7f2fd010203f79f","d31328070001000208131e29010203318a","d313280800010002343f000001020318af","d313300000010002000a0006010210efe4","d313300100010002a55a00740102108fb0","d313300200010002696e7951010210b98d","d31330030001000253530000010210c781"},["messages"]={{["stream_id"]=5,["ts_us"]=66051,["src"]=1,["dst"]=2,["mode_id"]=0,["flags"]=2,["payload"]="000b16212c37424d58636e79848f9aa5b0bbc6d1dce7f2fd08131e29343f"},{["stream_id"]=6,["ts_us"]=66064,["src"]=1,["dst"]=2,["mode_id"]=0,["flags"]=6,["payload"]="a55a0074696e79515353"}},["lost"]={}},{["name"]="reordered",["input_frames"]={"d31328030001000258636e79010203fbe8","d313280600010002dce7f2fd010203f79f","d313300000010002000a0006010210efe4","d31330030001000253530000010210c781","d313300200010002696e7951010210b98d","d313280500010002b0bbc6d10102039592","d31328070001000208131e29010203318a","d313280400010002848f9aa5010203311c","d3132802000100022c37424d010203ae88","d313280100010002000b16210102033931","d313300100010002a55a00740102108fb0","d313280800010002343f000001020318af","d313280000010002001e0002010203068c"},["messages"]={{["stream_id"]=6,["ts_us"]=66064,["src"]=1,["dst"]=2,["mode_id"]=0,["flags"]=6,["payload"]="a55a0074696e79515353"},{["stream_id"]=5,["ts_us"]=66051,["src"]=1,["dst"]=2,["mode_id"]=0,["flags"]=2,["payload"]="000b16212c37424d58636e79848f9aa5b0bbc6d1dce7f2fd08131e29343f"}},["lost"]={}},{["name"]="frag_drop_lost",["input_frames"]={"d313280000010002001e0002010203068c","d313280100010002000b16210102033931","d31328030001000258636e79010203fbe8","d313280400010002848f9aa5010203311c","d313280500010002b0bbc6d10102039592","d313280600010002dce7f2fd010203f79f","d31328070001000208131e29010203318a","d313280800010002343f000001020318af","d313300000010002000a0006010210efe4","d313300100010002a55a00740102108fb0","d313300200010002696e7951010210b98d","d31330030001000253530000010210c781"},["messages"]={{["stream_id"]=6,["ts_us"]=66064,["src"]=1,["dst"]=2,["mode_id"]=0,["flags"]=6,["payload"]="a55a0074696e79515353"}},["lost"]={5}},{["name"]="duplicate",["input_frames"]={"d313280000010002001e0002010203068c","d313280000010002001e0002010203068c","d313280100010002000b16210102033931","d313280100010002000b16210102033931","d3132802000100022c37424d010203ae88","d31328030001000258636e79010203fbe8","d313280400010002848f9aa5010203311c","d313280500010002b0bbc6d10102039592","d313280600010002dce7f2fd010203f79f","d31328070001000208131e29010203318a","d313280800010002343f000001020318af","d313300000010002000a0006010210efe4","d313300100010002a55a00740102108fb0","d313300200010002696e7951010210b98d","d31330030001000253530000010210c781"},["messages"]={{["stream_id"]=5,["ts_us"]=66051,["src"]=1,["dst"]=2,["mode_id"]=0,["flags"]=2,["payload"]="000b16212c37424d58636e79848f9aa5b0bbc6d1dce7f2fd08131e29343f"},{["stream_id"]=6,["ts_us"]=66064,["src"]=1,["dst"]=2,["mode_id"]=0,["flags"]=6,["payload"]="a55a0074696e79515353"}},["lost"]={}}}
local CLOCK = {{["nominal_rate_mhz"]=48000000,["tx_seq"]=0,["epoch"]=0,["payload"]="000000000000000002dc6c0000000000",["beacon_slot"]=0,["ts_us"]=40960,["src"]=161,["dst"]=65535,["frames"]={"d312000000a1ffff0010010000a000aae7","d312000100a1ffff0000000000a0001947","d312000200a1ffff0000000000a0001cd8","d312000300a1ffff02dc6c0000a00042ee","d312000400a1ffff0000000000a00017e6"},["gm_decimal"]="0"},{["nominal_rate_mhz"]=48000000,["tx_seq"]=4660,["epoch"]=7,["payload"]="0123456789abcdef02dc6c0012340007",["beacon_slot"]=1,["ts_us"]=40961,["src"]=161,["dst"]=65535,["frames"]={"d312080000a1ffff0010010000a001b19c","d312080100a1ffff0123456700a0017b75","d312080200a1ffff89abcdef00a001e7da","d312080300a1ffff02dc6c0000a0015995","d312080400a1ffff1234000700a0012305"},["gm_decimal"]="81985529216486895"},{["nominal_rate_mhz"]=96000000,["tx_seq"]=65535,["epoch"]=1,["payload"]="000000000000bb8005b8d800ffff0001",["beacon_slot"]=2,["ts_us"]=40962,["src"]=161,["dst"]=65535,["frames"]={"d312100000a1ffff0010010000a0029c11","d312100100a1ffff0000000000a0022fb1","d312100200a1ffff0000bb8000a00235d7","d312100300a1ffff05b8d80000a00293dd","d312100400a1ffffffff000100a002a66a"},["gm_decimal"]="48000"},{["nominal_rate_mhz"]=4294967295,["tx_seq"]=43981,["epoch"]=65535,["payload"]="ffffffffffffffffffffffffabcdffff",["beacon_slot"]=3,["ts_us"]=40963,["src"]=161,["dst"]=65535,["frames"]={"d312180000a1ffff0010010000a003876a","d312180100a1ffffffffffff00a003d408","d312180200a1ffffffffffff00a003d197","d312180300a1ffffffffffff00a003d2e2","d312180400a1ffffabcdffff00a0035534"},["gm_decimal"]="18446744073709551615"}}
local UNWRAP = {{["prev_abs"]=0,["raw"]=1,["mod"]=2048,["expect"]=1},{["prev_abs"]=5,["raw"]=5,["mod"]=2048,["expect"]=5},{["prev_abs"]=2047,["raw"]=0,["mod"]=2048,["expect"]=2048},{["prev_abs"]=2040,["raw"]=5,["mod"]=2048,["expect"]=2053},{["prev_abs"]=100,["raw"]=98,["mod"]=2048,["expect"]=98},{["prev_abs"]=0,["raw"]=2047,["mod"]=2048,["expect"]=0},{["prev_abs"]=1000,["raw"]=1500,["mod"]=2048,["expect"]=1500},{["prev_abs"]=5000,["raw"]=3,["mod"]=2048,["expect"]=4099}}

-- ── harness ─────────────────────────────────────────────────────────────────
local fail = 0
local function chk(c, m) if not c then print("  FAIL  " .. m); fail = fail + 1 end end
local function unhex(h)
  local t = {}
  for i = 1, #h, 2 do t[#t + 1] = tonumber(h:sub(i, i + 1), 16) end
  return t
end
local function tohex(a)
  local t = {}
  for i = 1, #a do t[i] = ("%02x"):format(a[i]) end
  return table.concat(t)
end

chk(S.CERTIFIED, "dcf_snake.lua did not self-certify on load")
chk(S.FRAG_BITS == CONST.frag_bits and S.MAX_FRAGS == CONST.max_frags
    and S.MAX_PAYLOAD == CONST.max_payload and S.MAX_STREAM_ID == CONST.max_stream_id,
    "L2 constants")
chk(S.FCTRL == CONST.frame_type_ctrl and S.FBEACON == CONST.frame_type_beacon, "frame types")
chk(S.MODE_LIVE == CONST.mode_live and S.MODE_NEAR == CONST.mode_near
    and S.MODE_RELAXED == CONST.mode_relaxed, "hop modes")
chk(S.FLAG_MORE == CONST.flag_more and S.FLAG_ANCHOR == CONST.flag_anchor
    and S.FLAG_END == CONST.flag_end, "descriptor flags")
chk(S.CLOCK_LEN == CONST.clock_len and S.CLOCK_VER == CONST.clock_ver
    and S.PID_MOD == CONST.pid_mod, "clock constants")
chk(S.NOMINAL_RATE_MHZ_48K == CONST.nominal_rate_mhz_48k, "nominal rate")

-- Law A: the anchor and every framing case reproduce the golden bytes
local af = S.packetize(unhex(ANCHOR.payload), ANCHOR.stream_id, ANCHOR.ts_us,
                       ANCHOR.src, ANCHOR.dst, ANCHOR.mode_id, ANCHOR.flags)
chk(#af == #ANCHOR.frames, ("anchor: %d frames ~= %d"):format(#af, #ANCHOR.frames))
for i, f in ipairs(af) do
  chk(S.frame_to_hex(f) == ANCHOR.frames[i],
      ("anchor frame %d: %s ~= %s"):format(i, S.frame_to_hex(f), ANCHOR.frames[i]))
end
for i, c in ipairs(FRAMING) do
  local fr = S.packetize(unhex(c.payload), c.stream_id, c.ts_us, c.src, c.dst,
                         c.mode_id, c.flags)
  chk(#fr == #c.frames, ("framing %d: %d frames ~= %d"):format(i, #fr, #c.frames))
  for k, f in ipairs(fr) do
    chk(S.frame_to_hex(f) == c.frames[k],
        ("framing %d frame %d: %s ~= %s"):format(i, k, S.frame_to_hex(f), c.frames[k]))
  end
end
print(("  PASS  anchor + %d framing cases reproduce golden bytes"):format(#FRAMING))

-- Law B: reassembly under in-order / reorder / drop / duplicate
for _, rc in ipairs(REASM) do
  local r, msgs = S.new_reassembler(), {}
  for _, h in ipairs(rc.input_frames) do
    local m = r:push(unhex(h))
    if m then msgs[#msgs + 1] = m end
  end
  local lost = r:finalize()
  chk(#msgs == #rc.messages, ("%s: %d messages ~= %d"):format(rc.name, #msgs, #rc.messages))
  for k, m in ipairs(rc.messages) do
    local g = msgs[k]
    chk(g and tohex(g.payload) == m.payload, ("%s msg %d payload"):format(rc.name, k))
    chk(g and g.stream_id == m.stream_id and g.ts_us == m.ts_us and g.src == m.src
        and g.dst == m.dst and g.mode_id == m.mode_id and g.flags == m.flags,
        ("%s msg %d header"):format(rc.name, k))
  end
  chk(#lost == #rc.lost, ("%s: %d lost ~= %d"):format(rc.name, #lost, #rc.lost))
  for k, id in ipairs(rc.lost) do chk(lost[k] == id, ("%s lost[%d]"):format(rc.name, k)) end
end
print(("  PASS  %d reassembly cases (reorder / drop->lost / duplicate)"):format(#REASM))

-- Law C: grandmaster clock packs, round-trips, and frames as 5 BEACONs
for i, c in ipairs(CLOCK) do
  local gm = S.gm_from_decimal(c.gm_decimal)
  local packed = S.pack_clock({ gm_sample_count = gm, nominal_rate_mhz = c.nominal_rate_mhz,
                                tx_seq = c.tx_seq, epoch = c.epoch })
  chk(tohex(packed) == c.payload,
      ("clock %d payload %s ~= %s"):format(i, tohex(packed), c.payload))
  local u = S.unpack_clock(packed)
  chk(S.gm_tostring(u.gm_sample_count) == c.gm_decimal,
      ("clock %d gm %s ~= %s"):format(i, S.gm_tostring(u.gm_sample_count), c.gm_decimal))
  chk(u.nominal_rate_mhz == c.nominal_rate_mhz and u.tx_seq == c.tx_seq
      and u.epoch == c.epoch, ("clock %d round-trip"):format(i))
  local bf = S.beacon_packetize(packed, c.beacon_slot, c.ts_us, c.src, c.dst, 0)
  chk(#bf == #c.frames, ("clock %d: %d beacon frames ~= %d"):format(i, #bf, #c.frames))
  for k, f in ipairs(bf) do
    chk(S.frame_to_hex(f) == c.frames[k],
        ("clock %d frame %d: %s ~= %s"):format(i, k, S.frame_to_hex(f), c.frames[k]))
  end
end
print(("  PASS  %d clock cases (u64 pack + BEACON framing, full 2^64 range)"):format(#CLOCK))

-- Law D: unwrap_pid, the certified timeline primitive
for i, u in ipairs(UNWRAP) do
  local got = S.unwrap_pid(u.prev_abs, u.raw, u["mod"])
  chk(got == u.expect, ("unwrap %d: unwrap_pid(%d,%d,%d)=%s want %d")
      :format(i, u.prev_abs, u.raw, u["mod"], tostring(got), u.expect))
end
print(("  PASS  %d unwrap_pid cases"):format(#UNWRAP))

-- Law E: bounds
chk(not pcall(S.packetize, {}, S.MAX_STREAM_ID + 1, 0, 1, 2, 0, 0),
    "stream_id above the 5-bit field must be rejected")
local big = {}
for i = 1, S.MAX_PAYLOAD + 1 do big[i] = 0 end
chk(not pcall(S.packetize, big, 0, 0, 1, 2, 0, 0), "oversize message must be rejected")
chk(S.channel_id("123456789") == 0x29B1, "crc16 rendezvous anchor")
chk(S.channel_id(nil) == S.BROADCAST, "empty channel = broadcast")

-- Law F: CTRL(3) carries BOTH DCF-Audio (11:5) and DCF-Snake (5:11); a Snake
-- reassembler must not be fed audio frames on the same dst, and defaults to CTRL.
local beacon = S.beacon_packetize(S.pack_clock({ gm_sample_count = 1 }), 0, 0, 1, 2, 0)
local rc = S.new_reassembler()
for _, f in ipairs(beacon) do chk(rc:push(f) == nil, "CTRL reassembler ignores BEACON") end
local rb = S.new_reassembler(S.FBEACON)
local got = nil
for _, f in ipairs(beacon) do got = rb:push(f) or got end
chk(got ~= nil and #got.payload == S.CLOCK_LEN, "BEACON reassembler recovers the clock")
print("  PASS  bounds + plane separation (CTRL record vs BEACON clock)")

-- Law G: the mixer timeline tracks a rolling counter monotonically
local tl = S.new_timeline(S.PID_MOD)
tl:step(2040)
for _, raw in ipairs({ 2041, 2042, 2047, 0, 1, 2 }) do tl:step(raw) end
chk(tl.abs == 2050, ("timeline crossed the wrap to %d, want 2050"):format(tl.abs))
tl:step(2049)
chk(tl.abs == 2049 and tl.reorders == 1, "a small backward delta is a reorder")
local ppm = S.skew_ppm(48000480, 48000000)
chk(math.abs(ppm - 10.0) < 0.001, ("skew %.3f ppm, want 10"):format(ppm))
local out, integ = S.pi_servo(10.0, 0)
chk(out > 0 and integ == 10.0, "PI servo corrects a fast source")
local clamped = S.pi_servo(1e9, 0)
chk(clamped <= 200.0, "PI servo output is clamped")
print("  PASS  mixer timeline, skew, and PI servo")

if fail == 0 then
  print("ALL SNAKE LAWS HOLD — lua/dcf_snake.lua CERTIFIED")
  os.exit(0)
else
  print(("%d FAILURES"):format(fail)); os.exit(1)
end
