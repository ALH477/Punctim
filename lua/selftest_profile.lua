-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  selftest_profile.lua — certify lua/dcf_profile.lua.
--  Run:  lua lua/selftest_profile.lua   (Lua 5.3+).  Exit 0 iff every check passes.
-- ============================================================================
local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local P = dofile(HERE .. "dcf_profile.lua")

local fail = 0
local function chk(c, m) if not c then print("  FAIL  " .. m); fail = fail + 1 end end

-- Law 1: every shipped profile builds and is internally coherent
local names = {}
for n in pairs(P.profiles) do names[#names + 1] = n end
table.sort(names)
for _, n in ipairs(names) do
  local ok, err = P.check(n)
  chk(ok, ("profile %q is incoherent: %s"):format(n, tostring(err)))
end
chk(#names >= 6, ("only %d profiles"):format(#names))
print(("  PASS  1 all %d profiles build coherently"):format(#names))

-- Law 2: EVERY profile's live_voice claim matches its own link budget.
-- This is the whole point of the module: a profile that promises live voice on
-- a medium that cannot carry it is a lie that only surfaces in the field.
for _, b in ipairs(P.report()) do
  chk(b.consistent,
      ("%s declares live_voice=%s but budgets %s (%.1f kbps needed, %.1f available)")
        :format(b.profile, tostring(b.live_voice_declared), b.verdict,
                b.needed_kbps, b.capacity_kbps))
end
print("  PASS  2 every profile's live_voice claim matches its measured budget")

-- Law 3: the live-voice floor is structural — no config reaches below it
chk(P.MIN_LIVE_VOICE_BPS == 2 * 17 * 50 * 8, "floor = 2 frames x 17 B x 50 blocks/s")
chk(P.MIN_LIVE_VOICE_BPS == 13600, ("floor is %d bps"):format(P.MIN_LIVE_VOICE_BPS))
local cheapest = math.huge
for _, bytes in pairs(P.codec_bytes) do
  local r = P.transport.report(bytes, { batch = "superpack" },
                               { block_ms = 20, udp_overhead = 0 })
  if r.kbps < cheapest then cheapest = r.kbps end
end
chk(cheapest * 1000 >= P.MIN_LIVE_VOICE_BPS,
    ("cheapest codec is %.1f kbps, below the stated floor"):format(cheapest))
print(("  PASS  3 live-voice floor is 13.6 kbps; cheapest codec is %.1f kbps"):format(cheapest))

-- Law 4: a raw radio link must not be charged UDP headers it never carries
local ip = P.budget("room", "tailscale")
local raw = P.budget("room", { bps = 5e6, ip = false, name = "raw" })
chk(raw.needed_kbps < ip.needed_kbps,
    "an IP medium must cost more than the same profile on a raw link")
chk(math.abs((ip.needed_kbps - raw.needed_kbps) - (28 * 8 * 50 / 1000 * 2)) < 0.1,
    "the difference is exactly the IPv4+UDP header, both directions")
print("  PASS  4 IP media are charged 28 B/datagram; raw links are not")

-- Law 5: incoherent overrides are refused, not quietly accepted
chk(not pcall(P.build, "studio", { codec = "nope" }), "unknown codec rejected")
chk(not pcall(P.build, "studio", { codec = "pcm-diag",
                                   transport = { mtu = 64, fec = { enabled = true } } }),
    "a burst that cannot fit the MTU is rejected")
chk(not pcall(P.build, "room", { codec = "opus-24",
                                 voice = { vad = { enabled = true } } }),
    "VAD gating on the music codec is rejected (it clips note decays)")
chk(not pcall(P.build, "nonexistent"), "unknown profile rejected")
print("  PASS  5 incoherent combinations are refused at build time")

-- Law 6: PTT is half-duplex and must budget as such
local half = P.budget("handheld", "sdr_fast")
local full = P.budget("handheld", "sdr_fast", { duplex = 2 })
chk(half.duplex == 1 and full.needed_kbps == half.needed_kbps * 2,
    "PTT budgets one direction; full duplex doubles it")
print("  PASS  6 PTT profiles budget half-duplex")

-- Law 7: diagnostics name the most likely first-contact fault
local cases = {
  { { datagrams_in = 0, frames_in = 0 }, "nothing-arrived" },
  { { frames_in = 100, rejected = 100 }, "wrong-channel" },
  { { frames_in = 100, malformed = 30 }, "mtu-or-mismatch" },
  { { frames_in = 100, fec_failed = 20, fec_corrected = 2 }, "fec-overwhelmed" },
  { { frames_in = 100, codec_mismatch = 4 }, "codec-mismatch" },
  { { popped = 100, late = 20 }, "buffer-too-shallow" },
  { { popped = 100, concealed = 30, malformed = 0 }, "loss-not-corruption" },
  { { sent = 200, suppressed = 0 }, "dtx-never-fires" },
}
for _, c in ipairs(cases) do
  local hits = P.diagnose(c[1])
  local found = false
  for _, h in ipairs(hits) do if h.id == c[2] then found = true end end
  chk(found, ("diagnose did not identify %q"):format(c[2]))
end
local healthy = P.diagnose({ frames_in = 1000, datagrams_in = 90, popped = 500,
                             rejected = 0, malformed = 0, concealed = 5,
                             sent = 200, suppressed = 80 })
chk(#healthy == 1 and healthy[1].id == "healthy",
    "a clean session must not invent a fault")
print(("  PASS  7 diagnostics identify %d fault signatures, silent when healthy"):format(#cases))

-- Law 8: diagnose accepts a Voice:stats() shape with nested transport stats
local nested = P.diagnose({ frames_in = 100, popped = 50,
                            transport = { malformed = 12, fec_corrected = 0,
                                          fec_failed = 0 } })
local ids = {}
for _, h in ipairs(nested) do ids[h.id] = true end
chk(ids["corruption-no-fec"] or ids["mtu-or-mismatch"],
    "nested transport counters are read")
print("  PASS  8 diagnose reads nested Voice:stats() transport counters")

if fail == 0 then
  print("ALL PROFILE LAWS HOLD — lua/dcf_profile.lua CERTIFIED")
  os.exit(0)
else
  print(("%d FAILURES"):format(fail)); os.exit(1)
end
