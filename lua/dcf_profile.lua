-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_profile.lua — end-to-end deployment profiles, link budget, diagnostics.
--
--  dcf_voice.lua and dcf_transport.lua each carry their own presets, chosen
--  independently. That is one preset too many degrees of freedom: `studio`
--  voice over an `acoustic` transport is nonsense, and nothing stopped you
--  writing it. A PROFILE pairs codec, jitter, DTX, transport and FEC into one
--  named deployment and refuses the incoherent combinations.
--
--  Profiles are named for what the radio is bolted to, not for a tier:
--
--    handheld   SAR / disaster / incident ground team — PTT, deep buffer
--    marine     vessel-to-vessel over VHF-class SDR — long paths, heavy FEC
--    expedition low-power off-grid — text-first, voice notes not live voice
--    studio     the Snake plane on cat5e — latency over everything else
--    room       hub-forwarded conference over IP / Tailscale
--    bench      diagnostics — one datagram per frame, nothing hidden
--
--  Two things this module exists to do before you plug anything in:
--
--    M.budget(profile, medium)   Will this profile FIT? A 20 ms voice block is
--                                the same bytes whether the medium is gigabit
--                                or a 1000-baud acoustic modem, and one of
--                                those cannot carry it. Find out here, not in
--                                the field.
--
--    M.diagnose(stats)           First contact between two real machines fails
--                                silently and identically for a dozen different
--                                reasons. This maps observed counters onto the
--                                cause, most likely first.
-- ============================================================================

local HERE = (debug.getinfo(1, "S").source:gsub("^@", "")):match("(.*/)") or "./"
local A = dofile(HERE .. "dcf_audio.lua")
local V = dofile(HERE .. "dcf_voice.lua")
local X = dofile(HERE .. "dcf_transport.lua")

local M = { audio = A, voice = V, transport = X }

-- ── Codec payload sizes, bytes per 20 ms block ──────────────────────────────
-- The wire cost of a profile follows from this number and nothing else:
-- frames = 1 + ceil(bytes/4), each 17 B.
M.codec_bytes = {
  ["opus-8"]   = 20,    -- narrowband speech, intelligible, cheapest real codec
  ["opus-12"]  = 30,
  ["opus-16"]  = 40,    -- the voice-chat default
  ["opus-24"]  = 60,    -- the DCF_AUDIO_SPEC music default
  ["pm"]       = 8,     -- Faust phase-mod parameters; synthesis, not speech
  ["pcm-diag"] = 120,   -- 6 kHz 8-bit, diagnostics only
}

-- ── Media capacities, bits per second ───────────────────────────────────────
-- Deliberately conservative and openly approximate: real radios vary. Override
-- with M.budget(profile, { bps = <measured> }) once you have measured yours,
-- which is the only number that actually counts.
-- `ip` matters: an IP medium pays 28 B of IPv4+UDP header per datagram, a raw
-- radio or acoustic link pays none. Counting UDP on a link that has no UDP
-- overstates the requirement by ~20% and would push a workable profile into
-- "impossible" for no reason.
M.media = {
  ethernet    = { bps = 1000e6, ip = true,  name = "cat5e / gigabit" },
  wifi        = { bps = 20e6,   ip = true,  name = "802.11 (congested)" },
  tailscale   = { bps = 5e6,    ip = true,  name = "Tailscale over broadband upstream" },
  lte         = { bps = 1e6,    ip = true,  name = "LTE upstream (weak signal)" },
  sdr_fast    = { bps = 100e3,  ip = false, name = "SDR, wideband GFSK" },
  sdr_narrow  = { bps = 9600,   ip = false, name = "SDR, narrowband 9k6" },
  hydramodem  = { bps = 1000,   ip = false, name = "HydraModem acoustic, 1000 baud" },
  janus       = { bps = 80,     ip = false, name = "JANUS underwater baseline" },
}

-- ── The live-voice floor (structural, not tunable) ───────────────────────────
-- DCF-Audio L2 sends 1 descriptor frame + ceil(payload/4) data frames per 20 ms
-- block. Even a codec producing ONE byte per block therefore costs 2 frames:
--
--   2 frames x 17 B = 34 B per 20 ms = 34 x 50 x 8 = 13.6 kbps
--
-- That is the floor for live voice over this adapter, before any codec, any
-- FEC, and any header. No configuration reaches below it, because the
-- descriptor is what makes reassembly possible. A medium slower than roughly
-- 14 kbps cannot carry a live DCF-Audio call at all — it carries text and async
-- voice notes, and a profile that claims otherwise is wrong.
M.MIN_LIVE_VOICE_BPS = 2 * 17 * 50 * 8   -- 13600

-- ── Profiles ────────────────────────────────────────────────────────────────
-- Each names its codec, its voice config and its transport config, plus the
-- media it is intended for. `voice` and `transport` are overlays applied on top
-- of the module defaults, not replacements.
M.profiles = {
  handheld = {
    summary = "SAR / disaster / incident ground team. PTT, deep buffer, FEC on.",
    -- Wideband, not 9k6 packet: a narrowband link sits below the live-voice
    -- floor above, so live audio on it is not a tuning problem, it is arithmetic.
    codec = "opus-12", media = "sdr_fast", ptt = true, live_voice = true,
    voice = {
      jitter = { target_ms = 120, min_ms = 60, max_ms = 500, grow_ms = 40,
                 resync_ahead_ms = 1000, late_policy = "accept" },
      plc = { max_consecutive = 12, fade = 0.8 },
      -- PTT already segments speech, so VAD only trims the tail.
      vad = { enabled = true, threshold = 0.03, hangover_ms = 300 },
    },
    transport = { batch = "superpack", fec = { enabled = true, parity = 16 }, mtu = 512 },
    notes = "PTT sidesteps acoustic echo cancellation entirely — the single "
         .. "largest reason casual voice chat needs a headset. On a handheld "
         .. "with a speaker-mic it is also what operators already expect.",
  },

  marine = {
    summary = "Vessel-to-vessel over VHF-class SDR. Long paths, heavy FEC.",
    codec = "opus-8", media = "sdr_fast", ptt = true, live_voice = true,
    voice = {
      jitter = { target_ms = 200, min_ms = 100, max_ms = 800, grow_ms = 60,
                 resync_ahead_ms = 1500, late_policy = "accept" },
      plc = { max_consecutive = 16, fade = 0.85 },
      vad = { enabled = true, threshold = 0.035, hangover_ms = 400 },
    },
    transport = { batch = "superpack", fec = { enabled = true, parity = 32 }, mtu = 512 },
    notes = "Multipath over water is bursty rather than uniform, so parity is "
         .. "doubled and the buffer is deep. Plaintext by law on amateur "
         .. "allocations (FCC Part 97.113(a)(4)); treat the channel as public.",
  },

  expedition = {
    summary = "Low-power off-grid. Text-first; voice notes, not live voice.",
    codec = "opus-8", media = "hydramodem", ptt = true, live_voice = false,
    voice = {
      jitter = { target_ms = 400, min_ms = 200, max_ms = 2000, resync_ahead_ms = 3000,
                 late_policy = "accept" },
      plc = { max_consecutive = 20, fade = 0.9 },
      vad = { enabled = true, threshold = 0.04, hangover_ms = 500 },
    },
    transport = { batch = "superpack", fec = { enabled = true, parity = 32 }, mtu = 255 },
    notes = "Live voice does NOT fit here and the profile says so — see "
         .. "live_voice=false and M.budget. Record, encode, forward, play: an "
         .. "async voice note crosses a 1000-baud link in minutes, where a live "
         .. "call cannot cross it at all.",
  },

  studio = {
    summary = "The Snake plane on cat5e. Latency over everything else.",
    codec = "opus-24", media = "ethernet", ptt = false, live_voice = true,
    voice = {
      jitter = { target_ms = 20, min_ms = 20, max_ms = 40, adaptive = false,
                 resync_ahead_ms = 200 },
      plc = { max_consecutive = 3, fade = 0.5 },
      vad = { enabled = false },        -- never gate a musician's silence
    },
    transport = { batch = "superpack", fec = { enabled = false }, mtu = 1200 },
    notes = "VAD off: a held note under the threshold must not be suppressed, "
         .. "and a gate that clips a decay is worse than the bandwidth it saves. "
         .. "Alignment keys on the grandmaster clock (dcf_snake.lua), never on "
         .. "the 24-bit frame timestamp, which wraps every ~16.7 s.",
  },

  room = {
    summary = "Hub-forwarded conference over IP / Tailscale.",
    codec = "opus-16", media = "tailscale", ptt = false, live_voice = true,
    voice = {
      jitter = { target_ms = 60, max_ms = 300, grow_ms = 40, resync_ahead_ms = 500 },
      vad = { enabled = true, threshold = 0.02, hangover_ms = 200 },
    },
    transport = { batch = "superpack", fec = { enabled = false }, mtu = 1200 },
    notes = "Forward through a hub rather than meshing: uplink is one stream "
         .. "instead of N-1. Without echo cancellation every participant on "
         .. "open speakers will feed back — headsets or push-to-talk until "
         .. "dm.audio grows AEC.",
  },

  bench = {
    summary = "Diagnostics. One datagram per frame; nothing batched or hidden.",
    codec = "pcm-diag", media = "ethernet", ptt = false, live_voice = true,
    voice = { jitter = { target_ms = 20, adaptive = false }, vad = { enabled = false } },
    transport = { batch = "none", fec = { enabled = false } },
    notes = "Every frame is its own datagram so a capture shows them one to "
         .. "one. Roughly 2.4x the link cost of a batched profile — correct for "
         .. "a packet capture, wrong for anything else.",
  },
}

-- ── Coherence ───────────────────────────────────────────────────────────────
-- Reject the combinations that are individually legal and jointly wrong.
local function coherence_errors(p, name)
  local errs = {}
  local bytes = M.codec_bytes[p.codec]
  if not bytes then
    errs[#errs + 1] = ("unknown codec %q"):format(tostring(p.codec))
    return errs
  end

  local frames = 1 + math.ceil(bytes / 4)
  if frames > 32 then
    errs[#errs + 1] = ("codec %s needs %d frames; DCF-Audio L2 caps a block at 32 "
      .. "(31 data fragments, 124 B)"):format(p.codec, frames)
  end

  local t = p.transport or {}
  local mtu = t.mtu or 1200
  local burst = frames * 17
  local parity = (t.fec and t.fec.enabled) and (t.fec.parity or 16) * 2 + 16 or 0
  if burst + parity > mtu then
    errs[#errs + 1] = ("%s: a %d B burst plus %d B of FEC overhead exceeds mtu %d "
      .. "— the burst will be split, adding a datagram per block")
      :format(name, burst, parity, mtu)
  end

  if p.codec == "pcm-diag" and (t.fec and t.fec.enabled) then
    errs[#errs + 1] = "pcm-diag is a diagnostic codec; wrapping it in FEC hides "
      .. "the raw link behaviour you enabled it to observe"
  end

  local vad = (p.voice and p.voice.vad) or {}
  if p.codec == "opus-24" and vad.enabled then
    errs[#errs + 1] = "opus-24 is the music profile; VAD gating will clip note "
      .. "decays below the threshold"
  end

  if p.live_voice == false and (p.voice and p.voice.jitter
      and (p.voice.jitter.target_ms or 0) < 200) then
    errs[#errs + 1] = "profile declares live_voice=false but carries a live-call "
      .. "jitter target; async voice notes do not need one"
  end

  return errs
end

--- Build a validated { voice = <cfg>, transport = <cfg>, ... } for a profile.
--- Overrides are merged on top and re-validated, so a field tweak cannot quietly
--- produce an incoherent deployment.
function M.build(name, overrides)
  local p = M.profiles[name] or error("unknown profile: " .. tostring(name))
  local merged = {}
  for k, v in pairs(p) do merged[k] = v end
  if overrides then
    for k, v in pairs(overrides) do
      if type(v) == "table" and type(merged[k]) == "table" then
        local t = {}
        for a, b in pairs(merged[k]) do t[a] = b end
        for a, b in pairs(v) do t[a] = b end
        merged[k] = t
      else
        merged[k] = v
      end
    end
  end

  local errs = coherence_errors(merged, name)
  if #errs > 0 then
    error(("profile %q is incoherent:\n  - %s"):format(name, table.concat(errs, "\n  - ")))
  end

  local vcfg = V.configure(merged.voice or {})
  local tcfg = X.configure(merged.transport or {})
  return {
    name = name, summary = merged.summary, notes = merged.notes,
    codec = merged.codec, codec_bytes = M.codec_bytes[merged.codec],
    media = merged.media, ptt = merged.ptt, live_voice = merged.live_voice,
    voice = vcfg, transport = tcfg,
  }
end

function M.check(name)
  local ok, err = pcall(M.build, name)
  return ok, (not ok) and tostring(err) or nil
end

-- ── Link budget ─────────────────────────────────────────────────────────────
--- Will this profile fit? medium is a key from M.media, or { bps = n, name = s }.
--- Returns a table with the measured requirement and a verdict. This measures
--- through the real transport rather than modelling it, so batching and FEC
--- overhead are counted as they will actually be sent.
function M.budget(name, medium, opts)
  opts = opts or {}
  local p = M.build(name)
  local m = type(medium) == "string"
    and (M.media[medium] or error("unknown medium: " .. medium))
    or medium
  local duplex = opts.duplex or (p.ptt and 1 or 2)   -- PTT is half-duplex
  local peers = opts.peers or 1                      -- extra streams to receive

  local udp = (m.ip == false) and 0 or 28
  local r = X.report(p.codec_bytes, p.transport, { block_ms = 20, udp_overhead = udp })
  local one_way = r.kbps
  local needed = one_way * duplex * peers
  local capacity = m.bps / 1000
  local headroom = capacity > 0 and (capacity - needed) / capacity or -1

  local verdict, advice
  if needed <= capacity * 0.5 then
    verdict = "fits"
    advice = "comfortable — room for retries and a second stream"
  elseif needed <= capacity * 0.85 then
    verdict = "tight"
    advice = "works on a clean link; a retransmit burst will stall it"
  elseif needed <= capacity then
    verdict = "marginal"
    advice = "no headroom. Drop to a cheaper codec or accept text-only voice notes"
  else
    verdict = "impossible"
    local cheapest = math.huge
    for cname, cbytes in pairs(M.codec_bytes) do
      local rr = X.report(cbytes, p.transport, { block_ms = 20, udp_overhead = udp })
      if rr.kbps * duplex * peers <= capacity and rr.kbps < cheapest then
        cheapest = rr.kbps
        advice = ("try codec %q (%.1f kbps)"):format(cname, rr.kbps)
      end
    end
    if cheapest == math.huge then
      if capacity * 1000 < M.MIN_LIVE_VOICE_BPS then
        advice = ("below the %d kbps live-voice floor: DCF-Audio always sends a "
          .. "descriptor plus at least one data frame per 20 ms block, so no codec "
          .. "reaches this link. Carry TEXT and async voice notes here")
          :format(M.MIN_LIVE_VOICE_BPS // 1000)
      else
        advice = "no codec fits at this FEC setting; reduce parity or mtu"
      end
    end
  end

  return {
    profile = name, codec = p.codec, medium = m.name or "custom",
    frames_per_block = r.frames, datagrams_per_block = r.datagrams,
    one_way_kbps = one_way, needed_kbps = needed,
    capacity_kbps = capacity, headroom = headroom,
    duplex = duplex, peers = peers,
    verdict = verdict, advice = advice,
    live_voice_declared = p.live_voice,
    above_floor = capacity * 1000 >= M.MIN_LIVE_VOICE_BPS,
    -- A profile that declares live_voice and does not fit is a bug in the
    -- profile, not a surprise in the field.
    consistent = (p.live_voice == true) == (verdict ~= "impossible"),
  }
end

-- ── First-contact diagnostics ───────────────────────────────────────────────
-- Two machines that cannot hear each other fail identically for a dozen
-- different reasons. Ordered most-likely-first for a first bring-up.
local RULES = {
  {
    id = "nothing-arrived",
    when = function(s) return (s.datagrams_in or 0) == 0 and (s.frames_in or 0) == 0 end,
    say = "No datagrams arrived at all. This is below the protocol: check the "
       .. "interface is up, the peer address and port, and that the firewall "
       .. "opens the port ON THAT INTERFACE. On Tailscale confirm both hosts "
       .. "are in the tailnet and any ACL admits the port.",
  },
  {
    id = "wrong-channel",
    when = function(s) return (s.rejected or 0) > 0 and (s.rejected or 0) >= (s.frames_in or 0) * 0.9 end,
    say = "Frames are arriving and being REJECTED — the peers are on different "
       .. "channels. The channel is crc16 of the passphrase, so a single "
       .. "character of difference lands you somewhere else entirely. Compare "
       .. "the resolved 0xNNNN on both ends, not the passphrase.",
  },
  {
    id = "mtu-or-mismatch",
    when = function(s) return (s.malformed or 0) > 0 and (s.fec_failed or 0) == 0 end,
    say = "Datagrams parse partially — trailing bytes or bad containers. The two "
       .. "ends are almost certainly on different transport presets (one "
       .. "batching, one not), or the path MTU is below the configured mtu and "
       .. "something is fragmenting. Set both ends to the same profile first.",
  },
  {
    id = "fec-overwhelmed",
    when = function(s) return (s.fec_failed or 0) > (s.fec_corrected or 0) end,
    say = "FEC is failing more often than it succeeds: damage exceeds its "
       .. "correcting power. Raise fec.parity (16 -> 32), shorten the burst with "
       .. "a smaller mtu, or move to a cheaper codec so each block carries less.",
  },
  {
    id = "corruption-no-fec",
    when = function(s) return (s.malformed or 0) > 0 and (s.fec_corrected or 0) == 0
                              and (s.fec_failed or 0) == 0 and (s.frames_in or 0) > 0 end,
    say = "Containers are being rejected by their joint CRC with no FEC enabled, "
       .. "so damage is detected and discarded rather than repaired. On a "
       .. "physical radio or acoustic link, enable FEC — that is the difference "
       .. "between concealment hiding the loss and correction removing it.",
  },
  {
    id = "codec-mismatch",
    when = function(s) return (s.codec_mismatch or 0) > 0 end,
    say = "The peer is sending a codec this node is not configured for. Frames "
       .. "decode by the codec the PACKET declares, so audio still plays, but "
       .. "concealment quality suffers. Align the profile on both ends.",
  },
  {
    id = "buffer-too-shallow",
    when = function(s) return (s.late or 0) > (s.popped or 0) * 0.05 end,
    say = "More than 5% of packets arrive after their playout slot. Raise "
       .. "jitter.target_ms, or set late_policy = \"accept\" to play them anyway "
       .. "— on a real radio link, late audio beats no audio.",
  },
  {
    id = "loss-not-corruption",
    when = function(s) return (s.concealed or 0) > (s.popped or 0) * 0.10
                              and (s.malformed or 0) == 0 end,
    say = "Over 10% of blocks are concealed with no corruption seen — datagrams "
       .. "are being ERASED, not damaged. FEC cannot help with erasure. Look for "
       .. "congestion, a duty-cycle limit on the radio, or an over-MTU path.",
  },
  {
    id = "starved",
    when = function(s) return (s.starved or 0) > (s.popped or 0) * 0.20 end,
    say = "The playout loop is running dry. Either the sender is silent (DTX "
       .. "working as intended — check the DTX ratio before chasing this), or "
       .. "playout is being called faster than 1 per block_ms.",
  },
  {
    id = "dtx-never-fires",
    when = function(s) return (s.sent or 0) > 50 and (s.suppressed or 0) == 0 end,
    say = "DTX never suppressed a block across the whole session, so silence is "
       .. "costing full bandwidth. The VAD threshold is likely below the noise "
       .. "floor of this microphone — raise vad.threshold until an idle room "
       .. "stops transmitting.",
  },
}

--- Map observed counters onto likely causes, most probable first. Pass any mix
--- of Voice:stats(), transport stats and jitter stats — missing keys are simply
--- not matched.
function M.diagnose(stats)
  local s = {}
  for k, v in pairs(stats or {}) do s[k] = v end
  if type(stats) == "table" and type(stats.transport) == "table" then
    for k, v in pairs(stats.transport) do if s[k] == nil then s[k] = v end end
  end

  local found = {}
  for _, rule in ipairs(RULES) do
    local ok, hit = pcall(rule.when, s)
    if ok and hit then found[#found + 1] = { id = rule.id, say = rule.say } end
  end
  if #found == 0 then
    found[1] = { id = "healthy",
                 say = "No fault signature matched. If audio still sounds wrong, "
                    .. "the problem is below DCF: sample rate, device buffer size, "
                    .. "or acoustic echo." }
  end
  return found
end

--- Render a budget table for every profile against its intended medium.
--- The honest preflight: run it before you carry anything into the field.
function M.report()
  local order = { "studio", "room", "handheld", "marine", "expedition", "bench" }
  local rows = {}
  for _, name in ipairs(order) do
    local p = M.profiles[name]
    local b = M.budget(name, p.media)
    rows[#rows + 1] = b
  end
  return rows
end

return M
