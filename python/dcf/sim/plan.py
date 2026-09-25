# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""punctim sim -- size the medium and hardware a DCF system needs.

    punctim sim (--spec system.json | flags) [--json] [--candidates m1,m2,...]

Input: node count, a primary medium plus candidates, a latency target, the offered traffic per
adapter (audio / game / text / sstv / sense, and a DCF-Pipe bulk object), the MAC and the node
power. Output: which candidate medium satisfies it, the exact airtime / duty cycle / queue /
MAC / Pipe plan, and a hardware-class recommendation.

Every row is tagged. EXACT rows are arithmetic on the certified codecs (medium symbol and bit
counts, byte-exact datagram sizes, adapter fragmentation, ceil(N/W) Pipe rounds, the sense/mac.py
slot geometry). MODEL rows rest on a stated basis the certificate cannot give (a link rate from
lua/dcf_profile.lua M.media, the measured HydraModem airtime, node power, hardware class).
A candidate passes iff duty < 80%, every interactive adapter's message fits the latency target,
the live-voice floor holds when audio is requested, and (on a shared channel) the sense TDMA
cycle fits the report interval. The recommendation is the passing candidate with the cheapest
hardware class (mcu < sbc-1core < desktop), then the least link load, then list order.

Exit: 0 ok (even when nothing passes -- that is an answer), 1 I/O error, 2 bad input.
"""
import argparse
import json
import math
import os
import sys

from .media import (SimUsage, MEDIA, MIN_LIVE_VOICE_BPS, QUEUE_MAX, HW_CLASSES, FRAME_LEN,
                    IP_UDP_HDR, ETH_HDR, ETH_MIN_PAYLOAD, DEFAULT_MTU, Hydra, L2Eth,
                    parse_medium, split_candidates, field_tier)
from .traffic import build_traffic, pipe_plan, num, CODEC_BYTES
from ..sense import mac as sense_mac
from ..sense import model as sense_model

EXIT_OK, EXIT_IO, EXIT_USAGE = 0, 1, 2
DEFAULT_MEDIUM = "hydra:profile=default,fec=conv"
DEFAULT_CANDIDATES = ["udp:proto", "udp:bare", "l2eth", "hydra:profile=default,fec=conv",
                      "hydra:profile=aux,fec=conv", "afsk:profile=handheld,fec=1",
                      "sdr_narrow", "janus"]
MACS = ("tdma", "fdma", "csma", "dedicated")
DUTY_MAX = 0.80
TOP_KEYS = ("nodes", "medium", "candidates", "latency_ms", "traffic", "mac", "guard_ms",
            "energy")
ENERGY_KEYS = ("node_mw",)


# ══ input ═══════════════════════════════════════════════════════════════════════════════
def normalize(spec):
    """A raw spec dict -> the validated system dict (defaults filled). SimUsage on bad input."""
    if not isinstance(spec, dict):
        raise SimUsage("spec: want a JSON object")
    bad = sorted(set(spec) - set(TOP_KEYS))
    if bad:
        raise SimUsage(f"spec: unknown key(s) {', '.join(bad)} (keys: {', '.join(TOP_KEYS)})")
    nodes = num(spec.get("nodes", 4), "nodes", 1, integer=True)
    cands = spec.get("candidates")
    given = cands is not None
    if cands is None:
        cands = [] if spec.get("medium") else list(DEFAULT_CANDIDATES)
    elif isinstance(cands, str):
        cands = split_candidates(cands)
    elif not (isinstance(cands, list) and all(isinstance(c, str) for c in cands)):
        raise SimUsage("candidates: want a list of medium strings")
    medium = spec.get("medium") or (cands[0] if given and cands else DEFAULT_MEDIUM)
    if not isinstance(medium, str):
        raise SimUsage("medium: want a medium string")
    lat = spec.get("latency_ms")
    if lat is not None:
        lat = num(lat, "latency_ms", 0, lo_open=True)
    mac = spec.get("mac", "tdma")
    if mac not in MACS:
        raise SimUsage(f"mac: {mac!r} (one of: {', '.join(MACS)})")
    guard_ms = num(spec.get("guard_ms", 50), "guard_ms", 0)
    energy = spec.get("energy") or {}
    if not isinstance(energy, dict):
        raise SimUsage("energy: want an object")
    ebad = sorted(set(energy) - set(ENERGY_KEYS))
    if ebad:
        raise SimUsage(f"energy: unknown key(s) {', '.join(ebad)} (keys: node_mw)")
    node_mw = num(energy.get("node_mw", 70), "energy.node_mw", 0, lo_open=True)
    order = [medium] + [c for c in cands if c.strip() != medium.strip()]
    return {"nodes": nodes, "medium": medium.strip(), "candidates": [c.strip() for c in order],
            "latency_ms": lat, "mac": mac, "guard_ms": guard_ms, "node_mw": node_mw,
            "traffic": spec.get("traffic") or {}}


# ══ evaluation ══════════════════════════════════════════════════════════════════════════
def fdma_plan(med, nodes):
    """FDMA over HydraModem tone channels, validated by sense/mac.Fdma (its Nyquist rule):
    channel k at base_freq + k * n_tones * tone_spacing."""
    p = med.p
    spacing = p["n_tones"] * p["tone_spacing"]

    def mk(n):
        return sense_mac.Fdma(n, base0=p["base_freq"], spacing=spacing,
                              tone_spacing=p["tone_spacing"], baud=p["baud"],
                              n_tones=p["n_tones"], sample_rate=p["sample_rate"])
    maxch = 0
    for n in range(1, 1025):
        try:
            mk(n)
        except ValueError:
            break
        maxch = n
    ch = max(1, min(nodes, maxch))
    f = mk(ch)
    return {"max_channels": maxch, "channels": ch, "nodes_per_channel": -(-nodes // ch),
            "spacing_hz": spacing, "base_freq_first_hz": f.profile_of(0)["base_freq"],
            "base_freq_last_hz": f.profile_of(ch - 1)["base_freq"], "gateway_cores": ch}


def evaluate(med, adapters, sysd, pipe):
    """One candidate -> its evaluation dict (checks + the numbers behind them)."""
    nodes, mac = sysd["nodes"], sysd["mac"]
    by = {a.name: a for a in adapters}
    F = sum(a.frames_per_s for a in adapters)
    ev = {"tag": "exact" if med.exact_time else "model", "kind": med.kind,
          "hardware_class": med.hw, "frames_per_s": F}
    channels, basis = 1, "one shared channel"
    if med.shared:
        if mac == "fdma" and isinstance(med, Hydra):
            ev["fdma"] = fdma_plan(med, nodes)
            channels, basis = ev["fdma"]["channels"], "FDMA tone channels (sense/mac.Fdma)"
        elif mac == "dedicated":
            channels, basis = nodes, "one line per node (dedicated)"
    else:
        basis = "switched / IP link: no MAC"
    ev["channels"], ev["channel_basis"] = channels, basis
    if not med.exact_time:
        ev["link"], ev["link_bps"] = getattr(med, "link", med.scheme), med.bps
        ev["offered_bps"] = sum(a.msgs_per_s * med.link_bytes(a.units) * 8 for a in adapters)
    busy = sum(a.msgs_per_s * med.seconds(a.units) for a in adapters)
    ev["duty_cycle"] = duty = busy / channels
    ev["capacity_bps"] = med.capacity_bps
    ev["msg_seconds"] = {a.name: med.seconds(a.units) for a in adapters}
    if not med.exact_time:
        ev["msg_link_bytes"] = {a.name: med.link_bytes(a.units) for a in adapters}
    ev["latency_s"] = {a.name: ev["msg_seconds"][a.name] for a in adapters if a.interactive}
    reasons = []
    if F > 0 and duty >= DUTY_MAX:
        reasons.append(f"duty {pct(duty)} >= {DUTY_MAX:.0%}")
    late = [f"{a.name} {secs(ev['latency_s'][a.name])} > {a.latency_ms:g} ms"
            for a in adapters if a.interactive and a.latency_ms is not None
            and ev["latency_s"][a.name] > a.latency_ms / 1000.0 + 1e-12]
    ev["latency_ok"] = not late
    if late:
        reasons.append("latency " + ", ".join(late))
    if "audio" in by:
        ev["voice_floor_met"] = med.capacity_bps >= MIN_LIVE_VOICE_BPS
        if not ev["voice_floor_met"]:
            reasons.append(f"{bps(med.capacity_bps)} below the {MIN_LIVE_VOICE_BPS} bit/s "
                           "live-voice floor")
    if "sense" in by and med.shared and mac in ("tdma", "fdma"):
        s = by["sense"]
        guard = sysd["guard_ms"] / 1000.0
        air = med.seconds(s.units)
        slot = air + 2 * guard
        per_ch = -(-nodes // channels)
        t = sense_mac.Tdma(num_slots=per_ch, slot_dur=slot, guard=guard)
        lo, hi = t.window(0)
        iv = s.extra["interval_s"]
        ev["tdma"] = {"slot_airtime_s": air, "guard_s": guard, "slot_s": slot,
                      "window_s": hi - lo, "slots_per_cycle": per_ch, "cycle_s": t.frame_dur,
                      "interval_s": iv, "capacity_nodes_per_channel": int(iv // slot),
                      "fits": t.frame_dur <= iv + 1e-12}
        if not ev["tdma"]["fits"]:
            reasons.append(f"TDMA cycle {secs(t.frame_dur)} > {iv:g} s interval")
    if mac == "csma" and med.shared:
        c = sense_mac.Csma()
        ev["csma"] = {"slot_s": c.slot_dur, "max_backoff_slots": c.max_backoff_slots,
                      "max_backoff_s": (c.max_backoff_slots - 1) * c.slot_dur}
    if F > 0:
        fin = F / channels
        fout = fin / duty if duty > 0 else None
        ev["queue"] = {"in_frames_per_s": fin, "out_frames_per_s": fout,
                       "overflow_s": QUEUE_MAX / (fin - fout) if duty > 1 else None}
    if pipe:
        ev["pipe"] = pipe_on(med, pipe)
    ev["pass"] = not reasons
    ev["reasons"] = reasons
    return ev


def pipe_on(med, pipe):
    """The Pipe data lane on one medium. Frame-only media carry each chunk datagram as 4-B
    DATA payloads (the basis of DCF_PIPE_SPEC's ~8-12 app-B/s HydraModem figure); datagram
    media send one datagram per chunk. Control frames and round-trip turnaround excluded."""
    if med.frame_only:
        per = [(-(-d["len"] // 4), d["count"]) for d in pipe["datagrams"]]
        return {"tag": "exact" if med.exact_time else "model",
                "carriage": "4 B per 17-B DATA frame",
                "frames": sum(f * c for f, c in per),
                "seconds": sum(med.seconds([f]) * c for f, c in per)}
    if isinstance(med, L2Eth):
        link = sum((max(ETH_MIN_PAYLOAD, d["len"]) + ETH_HDR) * d["count"]
                   for d in pipe["datagrams"])
        over = any(d["len"] > med.mtu for d in pipe["datagrams"])
        mtu = med.mtu
    else:
        link = sum((d["len"] + IP_UDP_HDR) * d["count"] for d in pipe["datagrams"])
        over = any(d["len"] + IP_UDP_HDR > DEFAULT_MTU for d in pipe["datagrams"])
        mtu = DEFAULT_MTU
    return {"tag": "model", "carriage": "one datagram per chunk", "link_bytes": link,
            "seconds": link * 8.0 / med.bps, "exceeds_mtu": over, "mtu": mtu}


def build_plan(sysd):
    """The validated system -> the full plan (exact / model / recommendation + objects)."""
    meds = [parse_medium(c) for c in sysd["candidates"]]
    primary = meds[0]
    adapters, pipe_spec = build_traffic(sysd["traffic"], sysd["nodes"], sysd["latency_ms"])
    pipe = None
    if pipe_spec is not None:
        pipe = pipe_plan(pipe_spec, "hydramodem" if primary.frame_only else "lan")
    evals = [(m, evaluate(m, adapters, sysd, pipe)) for m in meds]
    return {"system": sysd, "media": meds, "primary": primary, "adapters": adapters,
            "pipe": pipe, "evals": evals, "recommendation": recommend(evals, adapters, sysd)}


def recommend(evals, adapters, sysd):
    passing = [(i, m, e) for i, (m, e) in enumerate(evals) if e["pass"]]
    rejected = {m.key: e["reasons"] for m, e in evals if not e["pass"]}
    if not passing:
        i, m, e = min(((i, m, e) for i, (m, e) in enumerate(evals)),
                      key=lambda t: (len(t[2]["reasons"]), t[2]["duty_cycle"], t[0]))
        reason = (f"no candidate meets every constraint; closest is {m.key} "
                  f"({'; '.join(e['reasons'])})")
        if all(ev.get("voice_floor_met") is False for _, ev in evals):
            reason += (f". Every candidate is below the {MIN_LIVE_VOICE_BPS} bit/s "
                       "live-voice floor: carry text and async voice notes instead "
                       "(lua M.budget)")
        return {"medium": None, "hardware_class": None, "field_tier": None,
                "reason": reason, "closest": m.key, "passing": [], "rejected": rejected}
    i, m, e = min(passing, key=lambda t: (HW_CLASSES[t[1].hw][0], t[2]["duty_cycle"], t[0]))
    why = ([f"duty {pct(e['duty_cycle'])} < {DUTY_MAX:.0%}"] if adapters else
           ["no traffic requested, so only hardware class ranks the candidates"])
    if sysd["latency_ms"] is not None or any(a.latency_ms is not None for a in adapters
                                             if a.interactive):
        why.append("meets every latency target")
    if "voice_floor_met" in e:
        why.append(f"above the {MIN_LIVE_VOICE_BPS} bit/s live-voice floor")
    if "tdma" in e:
        why.append(f"TDMA cycle {secs(e['tdma']['cycle_s'])} fits {e['tdma']['interval_s']:g} s")
    reason = (f"cheapest hardware class ({m.hw}) among {len(passing)} passing of "
              f"{len(evals)} candidates; " + ", ".join(why))
    return {"medium": m.key, "hardware_class": m.hw,
            "field_tier": field_tier(m, sysd["nodes"]), "reason": reason,
            "passing": [pm.key for _, pm, _ in passing], "rejected": rejected}


# ══ JSON ════════════════════════════════════════════════════════════════════════════════
def _clean(v):
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return float(f"{v:.10g}")
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _pick(d, keys):
    return {k: d[k] for k in keys if k in d}


EV_EXACT = ("hardware_class", "channels", "channel_basis", "frames_per_s", "duty_cycle",
            "capacity_bps", "msg_seconds", "latency_s", "latency_ok", "voice_floor_met", "tdma",
            "fdma", "csma", "queue", "pass", "reasons")


def to_json(plan):
    sysd = plan["system"]
    adapters = plan["adapters"]
    exact = {
        "system": {k: sysd[k] for k in ("nodes", "medium", "candidates", "latency_ms", "mac",
                                        "guard_ms")},
        "traffic": {a.name: a.json() for a in adapters},
        "frames_per_s": sum(a.frames_per_s for a in adapters),
        "min_live_voice_bps": MIN_LIVE_VOICE_BPS,
        "queue_max": QUEUE_MAX,
        "largest_burst": None,
        "media": {},
        "candidates": {},
        "pipe": None,
        "pipe_media": {},
    }
    if adapters:
        big = max(adapters, key=lambda a: max(a.units))
        exact["largest_burst"] = {"adapter": big.name, "frames": max(big.units),
                                  "exceeds_queue": max(big.units) > QUEUE_MAX}
    model = {
        "link_budgets": {k: dict(v) for k, v in MEDIA.items()},
        "duty_limit": DUTY_MAX,
        "candidates": {},
        "pipe": {},
        "measured_airtime": {},
        "energy": None,
        "comparison": None,
        "sense_model": None,
        "hardware": {},
        "field_tier": {},
    }
    for m, e in plan["evals"]:
        mj = m.json()
        if not m.exact_time:
            mj["msg_link_bytes"] = e["msg_link_bytes"]
        exact["media"][m.key] = mj
        if m.exact_time:
            exact["candidates"][m.key] = _pick(e, EV_EXACT)
        else:
            model["candidates"][m.key] = _pick(e, EV_EXACT + ("link", "link_bps",
                                                              "offered_bps"))
        if "pipe" in e:
            (exact["pipe_media"] if e["pipe"]["tag"] == "exact" else model["pipe"])[m.key] = \
                e["pipe"]
        if isinstance(m, Hydra) and m.profiled:
            model["measured_airtime"][m.key] = measured(m)
        model["hardware"][m.key] = {"class": m.hw, "rank": HW_CLASSES[m.hw][0],
                                    "basis": m.hw_note}
        model["field_tier"][m.key] = field_tier(m, sysd["nodes"])
    if plan["pipe"]:
        exact["pipe"] = plan["pipe"]
    en = energy(plan)
    if en:
        model["energy"], model["comparison"], model["sense_model"] = (
            en["energy"], en["comparison"], en["sense_model"])
    return _clean({"exact": exact, "model": model, "recommendation": plan["recommendation"]})


def measured(m):
    air = sense_model.AIRTIME_S[m.fec]
    return {"measured_s": air, "exact_s": m.t_frame, "lead_tail_s": air - m.t_frame,
            "basis": "dcf/sense/model.py AIRTIME_S (frame_tx WAV length / 48 kHz)"}


def energy(plan):
    """Sense energy for the primary medium (MODEL): node power x time awake per reading."""
    by = {a.name: a for a in plan["adapters"]}
    if "sense" not in by:
        return None
    s, sysd, med = by["sense"], plan["system"], plan["primary"]
    iv, k = s.extra["interval_s"], s.extra["readings_per_node"]
    ch = plan["evals"][0][1]["channels"]                   # the primary medium's channels
    m = sense_model.model("conv", iv, ch, sysd["node_mw"], guard_s=sysd["guard_ms"] / 1000.0)
    comp = {"lora_mj_per_reading": m["lora_energy_per_reading_mj"],
            "rs485_mj_per_reading": m["rs485_energy_per_reading_mj"],
            "basis": "dcf/sense/model.py LORA/RS485 constants, order of magnitude"}
    out = {"energy": None, "comparison": comp, "sense_model": None}
    per_day = 86400.0 / iv * k
    if isinstance(med, Hydra) and med.profiled:
        m = sense_model.model(med.fec, iv, ch, sysd["node_mw"],
                              guard_s=sysd["guard_ms"] / 1000.0)
        e = m["energy_per_reading_mj"] * k
        basis = (f"{sysd['node_mw']:g} mW x measured {m['airtime_s']:g} s/reading "
                 "(dcf/sense/model.py)")
        out["sense_model"] = _pick(m, ("airtime_s", "slot_s", "nodes_per_channel", "channels",
                                       "total_nodes", "superframe_s", "duty_cycle"))
        out["sense_model"]["basis"] = "dcf/sense/model.py: measured airtime + 1 guard per slot"
    elif med.frame_only:
        e = sysd["node_mw"] * med.seconds(s.units)
        basis = (f"{sysd['node_mw']:g} mW x {'exact' if med.exact_time else 'modelled'} "
                 f"airtime {med.seconds(s.units):.4g} s (no measured lead/tail for this medium)")
    else:
        out["energy"] = {"medium": med.key, "mj_per_node_cycle": None,
                         "basis": "not derivable: NIC/radio power of an IP medium is not "
                                  "modelled"}
        return out
    out["energy"] = {"medium": med.key, "interval_s": iv, "mj_per_node_cycle": e,
                     "readings_per_node_per_day": per_day,
                     "j_per_node_per_day": e * per_day / k / 1000.0, "basis": basis}
    return out


# ══ text report ═════════════════════════════════════════════════════════════════════════
def pct(d):
    v = d * 100.0
    if v >= 100:
        return f"{v:,.0f}%"
    if v >= 0.01:
        return f"{v:.2f}%"
    if v == 0:
        return "0%"
    return f"{v:.2g}%"


def secs(s):
    if s is None:
        return "-"
    if s < 1e-3:
        return f"{s * 1e6:.1f} us"
    if s < 1:
        return f"{s * 1e3:.1f} ms"
    if s < 120:
        return f"{s:.3f} s"
    if s < 7200:
        return f"{s / 60:.1f} min ({s:,.0f} s)"
    return f"{s / 3600:.2f} h ({s:,.0f} s)"


def bps(b):
    if b >= 1e9:
        return f"{b / 1e9:g} Gbit/s"
    if b >= 1e6:
        return f"{b / 1e6:.4g} Mbit/s"
    if b >= 1e4:
        return f"{b / 1e3:.4g} kbit/s"
    return f"{b:,.0f} bit/s"


def rate(f):
    if f >= 1000:
        return f"{f:,.0f}"
    return f"{f:,.3f}" if f >= 0.001 else f"{f:.3g}"


def render_text(plan):
    sysd, adapters, pipe = plan["system"], plan["adapters"], plan["pipe"]
    evals, primary = plan["evals"], plan["primary"]
    W = max(len(m.key) for m in plan["media"])
    L = []
    add = L.append
    lat = (f"latency <= {sysd['latency_ms']:g} ms" if sysd["latency_ms"] is not None
           else "no latency target")
    add(f"punctim sim: {sysd['nodes']} nodes | mac {sysd['mac']} | guard "
        f"{sysd['guard_ms']:g} ms | {lat} | primary {primary.key}")
    add("tags: EXACT = arithmetic on the certified codecs; MODEL = stated basis, not certified")

    # ── EXACT ──
    add("")
    add("== EXACT " + "=" * 78)
    F = sum(a.frames_per_s for a in adapters)
    if adapters:
        add("traffic: adapter fragmentation 1 + ceil(len/4) frames/message (certified "
            "packetizers), one flush per message")
        lw = max(len(a.label) for a in adapters)
        for a in adapters:
            mid = f"-> {a.frames_per_msg:>5} {a.frame_type} frames/msg"
            add(f"EXACT  {a.name:<6} {a.label:<{lw}}  {mid}  = {rate(a.frames_per_s):>12} "
                "frames/s")
        add(f"EXACT  {'total':<6} {'':<{lw}}  {'':<{len(mid)}}  = {rate(F):>12} frames/s")
        if any(a.name == "audio" for a in adapters):
            add(f"EXACT  voice floor: 2 frames x {FRAME_LEN} B x 50 blocks/s x 8 = "
                f"{MIN_LIVE_VOICE_BPS} bit/s (lua M.MIN_LIVE_VOICE_BPS)")
        big = max(adapters, key=lambda a: max(a.units))
        if max(big.units) > QUEUE_MAX:
            add(f"EXACT  burst: one {big.name} message is {max(big.units)} frames > "
                f"OutboundQueue({QUEUE_MAX}); pace the sender (a burst this size sheds "
                "on any medium slower than the producer)")
        else:
            add(f"EXACT  burst: largest message {max(big.units)} frames ({big.name}) <= "
                f"OutboundQueue({QUEUE_MAX})")
    else:
        add("traffic: none requested (add --audio-blocks/--text-bps/--sense-interval/... "
            "or a traffic object)")

    add("")
    add("per-frame cost (python/MCP/mediumlab_core.py)")
    for m in plan["media"]:
        tag = "EXACT" if m.kind != "link" else "MODEL"
        add(f"{tag:<6} {m.key:<{W}}  {m.describe()}")

    ex = [(m, e) for m, e in evals if m.exact_time]
    if ex:
        add("")
        add("candidates on exact airtime (analog media)")
        for m, e in ex:
            _cand_block(add, "EXACT", m, e, sysd, adapters, W)

    if any("tdma" in e or "fdma" in e or "csma" in e for _, e in evals):
        m, e = evals[0]
        if "fdma" in e:
            f = e["fdma"]
            add("")
            add(f"FDMA plan for {m.key} (sense/mac.Fdma, channel k at base_freq + k x "
                f"{f['spacing_hz']:g} Hz)")
            add(f"EXACT  {f['channels']} channel(s) of max {f['max_channels']} below "
                f"Nyquist ({f['base_freq_first_hz']:g} .. {f['base_freq_last_hz']:g} Hz), "
                f"{f['nodes_per_channel']} node(s)/channel; gateway runs {f['gateway_cores']} "
                "decoder(s)")
        if "tdma" in e:
            t = e["tdma"]
            add("")
            add(f"TDMA plan for {m.key} (sense/mac.Tdma: guard trims both slot edges)")
            add(f"EXACT  slot = {secs(t['slot_airtime_s'])} airtime + 2 x "
                f"{secs(t['guard_s'])} guard = {secs(t['slot_s'])}; guarded window "
                f"{secs(t['window_s'])}")
            add(f"EXACT  cycle = {t['slots_per_cycle']} slots x {secs(t['slot_s'])} = "
                f"{secs(t['cycle_s'])} {'<=' if t['fits'] else '>'} {t['interval_s']:g} s "
                f"interval -> {'fits' if t['fits'] else 'DOES NOT FIT'} (capacity "
                f"{t['capacity_nodes_per_channel']} nodes/channel)")
        if "csma" in e:
            c = e["csma"]
            add("")
            add(f"CSMA on {m.key} (sense/mac.Csma defaults)")
            add(f"EXACT  backoff <= ({c['max_backoff_slots']} - 1) x {c['slot_s']:g} s = "
                f"{c['max_backoff_s']:g} s per retry; no slot plan (collisions depend on "
                "traffic)")

    if pipe:
        add("")
        add(f"DCF-Pipe object {pipe['bytes']:,} B, profile {pipe['profile']} (chunk "
            f"{pipe['chunk_size']}, nparity {pipe['nparity']}, window {pipe['credit_window']})")
        add(f"EXACT  {pipe['chunks']:,} chunks, ceil({pipe['chunks']}/"
            f"{pipe['credit_window']}) = {pipe['rounds']:,} rounds on a clean link; data lane "
            f"{pipe['data_lane_bytes']:,} B ({pipe['chunk_hdr_len']}-B header"
            f"{' + DCF-FEC wrap' if pipe['nparity'] else ''} = +{pipe['overhead_bytes']:,} B)")
        for m, e in evals:
            p = e["pipe"]
            if p["tag"] == "exact":
                add(f"EXACT  {m.key:<{W}}  {p['frames']:,} frames ({p['carriage']}) x "
                    f"{m.t_frame:.4f} s = {secs(p['seconds'])} (data lane only)")

    if F > 0:
        add("")
        add(f"queue (OutboundQueue maxlen {QUEUE_MAX} frames, one transport carrying the "
            "offered load per channel)")
        for m, e in ex:
            add(f"EXACT  {m.key:<{W}}  {_queue(e)}")

    # ── MODEL ──
    add("")
    add("== MODEL " + "=" * 78)
    add("link budgets (lua/dcf_profile.lua M.media; ip = pays 28 B IPv4/UDP per datagram)")
    items = [f"{k} {bps(v['bps'])}{' ip' if v['ip'] else ''}" for k, v in MEDIA.items()]
    add("MODEL  " + ", ".join(items[:4]))
    add("MODEL  " + ", ".join(items[4:]))
    md = [(m, e) for m, e in evals if not m.exact_time]
    if md:
        add("")
        add("candidates on a modelled link rate (byte counts EXACT, rate MODEL)")
        for m, e in md:
            _cand_block(add, "MODEL", m, e, sysd, adapters, W)
    if pipe:
        pm = [(m, e["pipe"]) for m, e in evals if e["pipe"]["tag"] == "model"]
        if pm:
            add("")
            add("DCF-Pipe on modelled links (data lane only; control and round trips excluded)")
            for m, p in pm:
                if "frames" in p:
                    add(f"MODEL  {m.key:<{W}}  {p['frames']:,} frames ({p['carriage']}) at "
                        f"{bps(m.bps)} = {secs(p['seconds'])}")
                else:
                    warn = (f"; chunk datagram exceeds the {p['mtu']}-B MTU (lower chunk_size)"
                            if p["exceeds_mtu"] else "")
                    add(f"MODEL  {m.key:<{W}}  {p['link_bytes']:,} B on the link at "
                        f"{bps(m.bps)} = {secs(p['seconds'])}{warn}")
    ms = [(m, measured(m)) for m, _ in evals if isinstance(m, Hydra) and m.profiled]
    if ms:
        add("")
        for m, r in ms:
            add(f"MODEL  measured airtime {m.key}: {r['measured_s']:g} s/frame "
                f"(dcf/sense/model.py) vs exact {r['exact_s']:.4f} s -> "
                f"+{secs(r['lead_tail_s'])} lead/tail silence")
    en = energy(plan)
    if en:
        add("")
        e = en["energy"]
        if e and e["mj_per_node_cycle"] is not None:
            add(f"MODEL  energy on {e['medium']}: {e['mj_per_node_cycle']:.1f} mJ per node "
                f"per {e['interval_s']:g} s = {e['j_per_node_per_day']:.1f} J/node/day at "
                f"{e['readings_per_node_per_day']:,.0f} readings/day")
            add(f"{'':<6} basis: {e['basis']}")
        elif e:
            add(f"MODEL  energy on {e['medium']}: {e['basis']}")
        c = en["comparison"]
        add(f"MODEL  vs LoRa SF7 {c['lora_mj_per_reading']:.1f} mJ/reading, RS-485 "
            f"{c['rs485_mj_per_reading']:.2f} mJ/reading ({c['basis']})")
        sm = en["sense_model"]
        if sm:
            add(f"MODEL  sense/model.py capacity: {sm['nodes_per_channel']} nodes/channel "
                f"(slot {secs(sm['slot_s'])} = measured airtime + 1 guard), superframe "
                f"{secs(sm['superframe_s'])}")
    add("")
    add("hardware class (mcu < sbc-1core < desktop) and field-test tier "
        "(Documentation/DCF_FIELD_USE.md)")
    seen = set()
    for m, e in evals:
        add(f"MODEL  {m.key:<{W}}  {m.hw}" + (f"  [{e['fdma']['gateway_cores']} gateway "
                                                f"cores for {e['fdma']['channels']} FDMA "
                                                "channels]" if "fdma" in e else ""))
        if m.hw_note not in seen:
            seen.add(m.hw_note)
            add(f"{'':<6} {'':<{W}}    basis: {m.hw_note}")
    add(f"MODEL  tier for {primary.key}: {field_tier(primary, sysd['nodes'])}")

    # ── RECOMMENDATION ──
    r = plan["recommendation"]
    add("")
    add("== RECOMMENDATION " + "=" * 69)
    if r["medium"]:
        add(f"RECOMMEND  {r['medium']} on {r['hardware_class']} hardware: {r['reason']}")
        if r["medium"] != primary.key:
            add(f"           tier: {r['field_tier']}")
    else:
        add(f"RECOMMEND  none: {r['reason']}")
    for k, why in r["rejected"].items():
        add(f"  rejected {k}: {'; '.join(why)}")
    return "\n".join(L)


def _queue(e):
    q = e.get("queue")
    if not q:
        return "no offered load"
    if q["overflow_s"] is not None:
        return (f"in {rate(q['in_frames_per_s'])} > out {rate(q['out_frames_per_s'])} "
                f"frames/s -> {QUEUE_MAX} full in {secs(q['overflow_s'])}, then sheds")
    return (f"in {rate(q['in_frames_per_s'])} <= out {rate(q['out_frames_per_s'])} frames/s "
            "-> drains (never overflows in steady state)")


def _cand_block(add, tag, m, e, sysd, adapters, W):
    ok = lambda b: "ok" if b else "FAIL"          # noqa: E731
    head = f"{tag:<6} {m.key:<{W}}  [{e['channel_basis']}"
    if e["channels"] > 1:
        head += f", {e['channels']} channels"
    head += f"; {m.hw}]" + ("  PASS" if e["pass"] else "  FAIL")
    add(head)
    pad = " " * 9
    if adapters:
        extra = (f" of {bps(e['link_bps'])} ({e['link']}): offered {bps(e['offered_bps'])}"
                 if tag == "MODEL" else "")
        add(f"{pad}duty     {pct(e['duty_cycle'])}{extra} -> "
            f"{ok(e['duty_cycle'] < DUTY_MAX)} (limit {DUTY_MAX:.0%})")
    if e["latency_s"]:
        parts = ", ".join(f"{k} {secs(v)}" for k, v in e["latency_s"].items())
        tgt = [a for a in adapters if a.interactive and a.latency_ms is not None]
        verdict = (f" -> {ok(e['latency_ok'])} (target "
                   + ", ".join(f"{a.name} {a.latency_ms:g} ms" for a in tgt) + ")"
                   if tgt else " (no target)")
        add(f"{pad}latency  one message on the medium: {parts}{verdict}")
    if "voice_floor_met" in e:
        add(f"{pad}floor    {bps(e['capacity_bps'])} "
            f"{'>=' if e['voice_floor_met'] else '<'} {MIN_LIVE_VOICE_BPS} bit/s live-voice "
            f"floor -> {ok(e['voice_floor_met'])}")
    if "tdma" in e:
        t = e["tdma"]
        add(f"{pad}tdma     {t['slots_per_cycle']} x {secs(t['slot_s'])} = "
            f"{secs(t['cycle_s'])} cycle {'<=' if t['fits'] else '>'} {t['interval_s']:g} s "
            f"-> {ok(t['fits'])}")
    if tag == "MODEL" and e.get("queue"):
        add(f"{pad}queue    {_queue(e)}")


# ══ CLI ═════════════════════════════════════════════════════════════════════════════════
def _parser():
    ap = argparse.ArgumentParser(
        prog="punctim sim",
        description="Size the medium and hardware a DCF system needs: exact airtime / duty "
                    "cycle / queue / MAC / Pipe plan from the certified codecs, plus modelled "
                    "link budgets, energy and a hardware-class recommendation.",
        epilog="media: hydra:[profile=default|aux,fec=none|rep3|conv,baud=,n_tones=,...] "
               "afsk:[profile=handheld|standard|aux-cable,fec=0|1] udp:proto|bare[,link=wifi] "
               "l2eth:[mtu=1500] or a link name: " + " ".join(MEDIA) + ".  "
               "exit: 0 ok, 1 I/O error, 2 bad input.")
    ap.add_argument("--spec", metavar="FILE", help="system description (JSON)")
    ap.add_argument("--json", action="store_true", help="emit {exact, model, recommendation}")
    ap.add_argument("--nodes", type=int)
    ap.add_argument("--medium", metavar="URI", help="primary medium planned in detail")
    ap.add_argument("--candidates", action="append", metavar="M1,M2,...",
                    help="candidate media (commas inside a URI are kept; ';' also separates)")
    ap.add_argument("--latency-ms", type=float)
    ap.add_argument("--mac", choices=MACS)
    ap.add_argument("--guard-ms", type=float)
    ap.add_argument("--node-mw", type=float, help="node active power for energy (mW)")
    g = ap.add_argument_group("traffic")
    g.add_argument("--audio-blocks", type=float, metavar="N", help="audio blocks/s per stream")
    g.add_argument("--audio-payload", type=int, metavar="B", help="codec bytes/block (<=124)")
    g.add_argument("--audio-codec", choices=sorted(CODEC_BYTES))
    g.add_argument("--audio-streams", type=int, metavar="N")
    g.add_argument("--game-rate", type=float, metavar="N", help="game messages/s per node")
    g.add_argument("--game-payload", type=int, metavar="B")
    g.add_argument("--text-bps", type=float, metavar="B", help="text bytes/s (aggregate)")
    g.add_argument("--text-msg-bytes", type=int, metavar="B")
    g.add_argument("--sstv-per-hour", type=float, metavar="N")
    g.add_argument("--sstv-bytes", type=int, metavar="B")
    g.add_argument("--sense-interval", type=float, metavar="S", help="sense report interval")
    g.add_argument("--sense-readings", type=int, metavar="N", help="readings per node/interval")
    g.add_argument("--pipe-bytes", type=int, metavar="B", help="one DCF-Pipe object")
    g.add_argument("--pipe-profile", metavar="NAME", help="lan | hydramodem | sneakernet")
    return ap


_FLAG_TRAFFIC = {
    "audio_blocks": ("audio", "blocks_per_s"), "audio_payload": ("audio", "payload_b"),
    "audio_codec": ("audio", "codec"), "audio_streams": ("audio", "streams"),
    "game_rate": ("game", "msgs_per_s"), "game_payload": ("game", "payload_b"),
    "text_bps": ("text", "bytes_per_s"), "text_msg_bytes": ("text", "msg_bytes"),
    "sstv_per_hour": ("sstv", "images_per_h"), "sstv_bytes": ("sstv", "bytes"),
    "sense_interval": ("sense", "interval_s"), "sense_readings": ("sense", "readings_per_node"),
    "pipe_bytes": ("pipe", "bytes"), "pipe_profile": ("pipe", "profile"),
}


def load_system(a):
    """argparse namespace -> validated system dict (spec file, then flags on top)."""
    spec = {}
    if a.spec:
        with open(a.spec) as fh:                      # OSError -> exit 1
            text = fh.read()
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as e:
            raise SimUsage(f"{a.spec}: invalid JSON: {e}") from None
        if not isinstance(spec, dict):
            raise SimUsage(f"{a.spec}: want a JSON object")
    spec = json.loads(json.dumps(spec))               # private deep copy
    for flag, key in (("nodes", "nodes"), ("medium", "medium"), ("latency_ms", "latency_ms"),
                      ("mac", "mac"), ("guard_ms", "guard_ms")):
        v = getattr(a, flag)
        if v is not None:
            spec[key] = v
    if a.candidates:
        spec["candidates"] = [c for v in a.candidates for c in split_candidates(v)]
    if a.node_mw is not None:
        if not isinstance(spec.get("energy", {}), dict):
            raise SimUsage("energy: want an object")
        spec.setdefault("energy", {})["node_mw"] = a.node_mw
    tr = spec.get("traffic")
    if tr is None:
        tr = spec["traffic"] = {}
    if not isinstance(tr, dict):
        raise SimUsage("traffic: want an object")
    for flag, (ad, key) in _FLAG_TRAFFIC.items():
        v = getattr(a, flag)
        if v is None:
            continue
        sect = tr.get(ad)
        if sect is None:
            sect = tr[ad] = {}
        if not isinstance(sect, dict):
            raise SimUsage(f"traffic.{ad}: want an object")
        if ad == "audio" and key in ("payload_b", "codec"):
            sect.pop("codec" if key == "payload_b" else "payload_b", None)
        sect[key] = v
    return normalize(spec)


def _quiet_stdout():
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:                           # --help -> 0, a usage error -> 2
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    if not a.spec and all(v is None for k, v in vars(a).items() if k not in ("spec", "json")):
        _parser().print_usage(sys.stderr)
        print("punctim sim: describe the system: --spec FILE or flags, e.g. "
              "--nodes 4 --sense-interval 60 (see --help)", file=sys.stderr)
        return EXIT_USAGE
    try:
        plan = build_plan(load_system(a))
        out = (json.dumps(to_json(plan), indent=2, sort_keys=False) if a.json
               else render_text(plan))
    except SimUsage as e:
        print(f"punctim sim: {e}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as e:
        print(f"punctim sim: {e}", file=sys.stderr)
        return EXIT_IO
    try:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        _quiet_stdout()
        return EXIT_IO
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
