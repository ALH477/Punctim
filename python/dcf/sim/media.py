# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""punctim sim -- media: the exact per-frame cost of every medium, and the modelled links.

EXACT is whatever follows from the byte-certified medium codecs in
python/MCP/mediumlab_core.py (Documentation/DCF_MEDIUM_SPEC.md):

  hydra:  total_syms / baud seconds per frame   (hydra_profile(): preamble + sync + FEC'd data)
  afsk:   n_bits / baud seconds per frame       (afsk_bits_encode(): preamble + 0x7E + body)
  udp:    ProtoMessage 34 B per frame (dialect=proto), or a 32 B SuperPack per pair / 17 B
          lone frame (dialect=bare) -- plus 28 B IPv4+UDP per datagram
  l2eth:  [n u16][SuperPack x ceil(n/2)] per batch, + 14 B Ethernet header, payload >= 46 B

MODEL is anything that needs a number the certificate cannot give: a link rate (the
`MEDIA` table is a mirror of lua/dcf_profile.lua M.media, asserted equal by the tests), a
measured figure, or a hardware judgement. A medium named by a bare link name
(ethernet, wifi, ..., janus) is costed like lua M.budget does it: frames SuperPack-paired,
+28 B per datagram iff the link is IP.
"""
import os
import sys

_MCP = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                     "MCP"))
if os.path.isdir(_MCP) and _MCP not in sys.path:
    sys.path.insert(0, _MCP)

import mediumlab_core as M  # noqa: E402
import wirelab_core as wire  # noqa: E402

from ..transport import OutboundQueue  # noqa: E402


class SimUsage(ValueError):
    """Bad simulator input (punctim exit code 2)."""


# ── constants (derived from the codecs where the codec defines them) ─────────────────────
FRAME_LEN = wire.FRAME_LEN                               # 17
FRAME_BITS = FRAME_LEN * 8                               # 136
_F = M.L2_FILLER                                         # any valid frame: costs are content-free
PROTO_FRAME_LEN = len(M.proto_frame_encode(_F, 1))       # 34 = 17 B header + 17 B frame
SUPER_LEN = len(M.bare_encode([_F, _F])[0])              # 32 = one SuperPack pair
LONE_LEN = len(M.bare_encode([_F])[0])                   # 17 = a lone bare frame
L2_HDR = M.L2_HDR                                        # 2  = the n_frames u16
IP_UDP_HDR = 28          # IPv4 (20) + UDP (8) per datagram -- lua/dcf_profile.lua M.budget
ETH_HDR = 14             # dst MAC + src MAC + EtherType
ETH_MIN_PAYLOAD = 46     # Ethernet pads a shorter payload (excl. preamble/FCS/IFG)
DEFAULT_MTU = 1500
QUEUE_MAX = OutboundQueue().maxlen                       # 256 frames, every dcf.transport

# ── modelled link budgets: a mirror of lua/dcf_profile.lua M.media (bits per second) ─────
MEDIA = {
    "ethernet":   {"bps": 1000e6, "ip": True,  "name": "cat5e / gigabit"},
    "wifi":       {"bps": 20e6,   "ip": True,  "name": "802.11 (congested)"},
    "tailscale":  {"bps": 5e6,    "ip": True,  "name": "Tailscale over broadband upstream"},
    "lte":        {"bps": 1e6,    "ip": True,  "name": "LTE upstream (weak signal)"},
    "sdr_fast":   {"bps": 100e3,  "ip": False, "name": "SDR, wideband GFSK"},
    "sdr_narrow": {"bps": 9600,   "ip": False, "name": "SDR, narrowband 9k6"},
    "hydramodem": {"bps": 1000,   "ip": False, "name": "HydraModem acoustic, 1000 baud"},
    "janus":      {"bps": 80,     "ip": False, "name": "JANUS underwater baseline"},
}

# The live-voice floor (lua M.MIN_LIVE_VOICE_BPS): DCF-Audio sends a descriptor + >= 1 data
# frame per 20 ms block, so even a 1-byte codec costs 2 frames x 17 B x 50 blocks/s x 8.
MIN_LIVE_VOICE_BPS = 2 * FRAME_LEN * 50 * 8              # 13600

# ── hardware classes (MODEL: a judgement, the basis is stated) ───────────────────────────
HW_CLASSES = {
    "mcu": (0, "datagram medium, no DSP: any microcontroller with a NIC / IP stack"),
    "sbc-1core": (1, "HydraModem runs real-time on one StarFive JH7110 U74 core at 1000 baud "
                     "per channel (CLAUDE.md, HydraModem); a gateway decoding N FDMA channels "
                     "needs N cores"),
    "desktop": (2, "numpy AFSK / SDR front end / multi-decoder: a desktop- or laptop-class "
                   "host"),
}

SCHEME_WORDS = ("udp", "l2eth", "hydra", "afsk", "audio", "sdr", "janus", "file", "stdio",
                "hex", "loop")


def _bool(v, what):
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise SimUsage(f"{what}={v!r}: want 0|1")


def bare_datagrams(n, pair=True):
    """Datagram lengths bare_encode() emits for n frames (pairs -> 32 B, lone -> 17 B)."""
    if not pair:
        return [LONE_LEN] * n
    return [SUPER_LEN] * (n // 2) + [LONE_LEN] * (n % 2)


def l2_payloads(n, mtu=DEFAULT_MTU):
    """Ethernet payload lengths l2_batch() emits for n frames, split at the MTU capacity."""
    cap = M.l2_capacity(mtu)
    if cap < 1:
        raise SimUsage(f"l2eth: mtu {mtu} holds no SuperPack")
    out = []
    while n > 0:
        k = min(n, cap)
        out.append(L2_HDR + SUPER_LEN * ((k + 1) // 2))
        n -= k
    return out


# ── media ───────────────────────────────────────────────────────────────────────────────
class Medium:
    """One candidate medium. `seconds(units)` is the time to put one adapter message on the
    medium, where `units` is its frame count per flush (a chained message flushes twice)."""
    kind = "base"            # analog | datagram | link
    exact_time = False       # True iff the airtime per frame is exact (analog media)
    shared = False           # a broadcast channel that needs a MAC (TDMA/FDMA/CSMA)
    frame_only = False       # carries only 17-B frames (a Pipe data lane rides 4 B/frame)
    ip = False
    scheme = ""
    hw = "mcu"
    hw_note = ""

    def __init__(self, key):
        self.key = key

    # time on the medium -----------------------------------------------------------------
    def seconds(self, units):
        return self.link_bytes(units) * 8.0 / self.bps

    def link_bytes(self, units):                                   # datagram / link media
        return sum(sum(self.datagrams(n)) for n in units)

    def datagrams(self, n):
        raise NotImplementedError

    @property
    def capacity_bps(self):
        """Frame-bit capacity: the link rate, or 136 bits per exact frame time."""
        return self.bps

    def json(self):
        return {"kind": self.kind, "scheme": self.scheme}


class Hydra(Medium):
    kind, exact_time, shared, frame_only, scheme = "analog", True, True, True, "hydra"
    hw = "sbc-1core"

    def __init__(self, key, kw):
        super().__init__(key)
        name = kw.get("profile", "default")
        ov = {}
        for k in ("fec", "interleave", "baud", "n_tones", "base_freq", "tone_spacing"):
            if k in kw:
                ov[k] = kw[k]
        try:
            if "interleave" in ov:
                ov["interleave"] = int(_bool(ov["interleave"], "interleave"))
            for k in ("baud", "base_freq", "tone_spacing"):
                if k in ov:
                    ov[k] = float(ov[k])
            if "n_tones" in ov:
                ov["n_tones"] = int(ov["n_tones"])
            self.p = M.hydra_profile(name, **ov)
        except ValueError as e:
            raise SimUsage(f"{key}: {e}") from None
        p = self.p
        self.fec = M.FEC_BY_ID[p["fec_mode"]]
        self.unit, self.count, self.rate = "symbols", p["total_syms"], p["baud"]
        self.t_frame = self.count / self.rate
        self.bps = FRAME_BITS / self.t_frame
        # the one profiled configuration (default: 1000 baud 2-FSK, 24 preamble symbols)
        self.profiled = (name == "default" and p["baud"] == 1000.0 and p["n_tones"] == 2
                         and p["preamble_syms"] == 24)
        self.hw_note = HW_CLASSES["sbc-1core"][1] + (
            "" if p["baud"] <= 1000.0 and p["n_tones"] == 2 else
            f"; NOTE {p['baud']:g} baud {p['n_tones']}-FSK is outside the profiled 1000-baud "
            "2-FSK case, so one-core real-time headroom is unverified")

    def seconds(self, units):
        return sum(units) * self.t_frame

    @property
    def capacity_bps(self):
        return self.bps

    def describe(self):
        p = self.p
        return (f"{self.count} symbols ({p['preamble_syms']} preamble + {p['sync_syms']} sync + "
                f"{p['data_syms']} data, fec={self.fec}) / {self.rate:g} baud "
                f"= {self.t_frame:.4f} s/frame")

    def json(self):
        p = self.p
        return {"kind": self.kind, "scheme": self.scheme, "profile": p["name"],
                "fec": self.fec, "interleave": p["interleave"], "n_tones": p["n_tones"],
                "unit": self.unit, "units_per_frame": self.count, "baud": self.rate,
                "preamble_syms": p["preamble_syms"], "sync_syms": p["sync_syms"],
                "data_syms": p["data_syms"], "coded_bits": p["coded_bits"],
                "airtime_per_frame_s": self.t_frame, "capacity_frames_per_s": 1 / self.t_frame,
                "capacity_bps": self.bps}


class Afsk(Medium):
    kind, exact_time, shared, frame_only, scheme = "analog", True, True, True, "afsk"
    hw = "desktop"
    hw_note = ("python/modem AFSK demodulates with numpy (not stdlib): a desktop- or "
               "laptop-class host")

    def __init__(self, key, kw):
        super().__init__(key)
        prof = kw.get("profile", "handheld")
        if prof not in M.AFSK_PROFILE_NAMES:
            raise SimUsage(f"{key}: unknown afsk profile {prof!r} "
                           f"({'|'.join(M.AFSK_PROFILE_NAMES)})")
        self.profile, self.rs = prof, _bool(kw.get("fec", "0"), f"{key}: fec")
        self.unit = "bits"
        self.count = len(M.afsk_bits_encode(_F, prof, self.rs))
        self.rate = float(M.AFSK_PROFILES[prof]["baud"])
        self.t_frame = self.count / self.rate
        self.bps = FRAME_BITS / self.t_frame

    def seconds(self, units):
        return sum(units) * self.t_frame

    def describe(self):
        return (f"{self.count} bits ({'RS' if self.rs else 'crc8'}, profile {self.profile}) / "
                f"{self.rate:g} baud = {self.t_frame:.4f} s/frame")

    def json(self):
        return {"kind": self.kind, "scheme": self.scheme, "profile": self.profile,
                "fec": int(self.rs), "unit": self.unit, "units_per_frame": self.count,
                "baud": self.rate, "airtime_per_frame_s": self.t_frame,
                "capacity_frames_per_s": 1 / self.t_frame, "capacity_bps": self.bps}


class Udp(Medium):
    kind, scheme, ip = "datagram", "udp", True
    hw_note = HW_CLASSES["mcu"][1]

    def __init__(self, key, kw, link):
        super().__init__(key)
        self.dialect = kw.get("dialect", "proto")
        if self.dialect not in ("proto", "bare"):
            raise SimUsage(f"{key}: dialect={self.dialect!r}: want proto|bare")
        self.pair = _bool(kw.get("pair", "1"), f"{key}: pair")
        self.link = link or "ethernet"
        if self.link not in MEDIA or not MEDIA[self.link]["ip"]:
            raise SimUsage(f"{key}: link={self.link!r} is not an IP link "
                           f"({', '.join(k for k, v in MEDIA.items() if v['ip'])})")
        self.bps = MEDIA[self.link]["bps"]

    def datagrams(self, n):
        if self.dialect == "proto":
            return [PROTO_FRAME_LEN + IP_UDP_HDR] * n
        return [d + IP_UDP_HDR for d in bare_datagrams(n, self.pair)]

    def describe(self):
        if self.dialect == "proto":
            return (f"{PROTO_FRAME_LEN} B ProtoMessage(type 12) per frame + {IP_UDP_HDR} B "
                    f"IPv4/UDP = {PROTO_FRAME_LEN + IP_UDP_HDR} B/frame")
        if not self.pair:
            return f"{LONE_LEN} B bare frame + {IP_UDP_HDR} B IPv4/UDP per frame"
        return (f"{SUPER_LEN} B SuperPack per 2 frames ({LONE_LEN} B lone) + {IP_UDP_HDR} B "
                "IPv4/UDP per datagram")

    def json(self):
        return {"kind": self.kind, "scheme": self.scheme, "dialect": self.dialect,
                "pair": int(self.pair), "link": self.link, "unit": "bytes",
                "bytes_per_frame": PROTO_FRAME_LEN if self.dialect == "proto" else None,
                "superpack_len": SUPER_LEN, "lone_len": LONE_LEN, "ip_udp_hdr": IP_UDP_HDR}


class L2Eth(Medium):
    kind, scheme = "datagram", "l2eth"
    hw_note = HW_CLASSES["mcu"][1]

    def __init__(self, key, kw, link):
        super().__init__(key)
        try:
            self.mtu = int(str(kw.get("mtu", DEFAULT_MTU)), 0)
        except ValueError:
            raise SimUsage(f"{key}: mtu={kw.get('mtu')!r}: want an integer") from None
        self.cap = M.l2_capacity(self.mtu)
        if self.cap < 1:
            raise SimUsage(f"{key}: mtu {self.mtu} holds no SuperPack")
        self.link = link or "ethernet"
        if self.link != "ethernet":
            raise SimUsage(f"{key}: raw L2 rides Ethernet only (link=ethernet)")
        self.bps = MEDIA[self.link]["bps"]

    def datagrams(self, n):
        return [max(ETH_MIN_PAYLOAD, b) + ETH_HDR for b in l2_payloads(n, self.mtu)]

    def describe(self):
        return (f"[n u16][{SUPER_LEN} B SuperPack x ceil(n/2)] per batch (<= {self.cap} "
                f"frames at mtu {self.mtu}) + {ETH_HDR} B Ethernet, payload >= "
                f"{ETH_MIN_PAYLOAD} B")

    def json(self):
        return {"kind": self.kind, "scheme": self.scheme, "mtu": self.mtu,
                "frames_per_batch_max": self.cap, "link": self.link, "unit": "bytes",
                "l2_hdr": L2_HDR, "superpack_len": SUPER_LEN, "eth_hdr": ETH_HDR,
                "eth_min_payload": ETH_MIN_PAYLOAD}


class Link(Medium):
    """A bare link name from MEDIA, costed like lua M.budget: SuperPack pairs, +28 B per
    datagram iff IP. The rate is modelled, so everything but the byte count is MODEL."""
    kind = "link"

    def __init__(self, key, name):
        super().__init__(key)
        self.link = name
        m = MEDIA[name]
        self.bps, self.ip, self.scheme = m["bps"], m["ip"], name
        self.shared = self.frame_only = not self.ip
        if self.ip:
            self.hw, self.hw_note = "mcu", HW_CLASSES["mcu"][1]
        elif name == "hydramodem":
            self.hw, self.hw_note = "sbc-1core", HW_CLASSES["sbc-1core"][1]
        elif name.startswith("sdr"):
            self.hw, self.hw_note = "desktop", ("SDR front end + host DSP "
                                                "(python/modem/sdr.py): desktop-class host")
        else:
            self.hw, self.hw_note = "desktop", ("JANUS rides the GPL janus-c reference as a "
                                                "separate process; not profiled on an SBC here")

    def datagrams(self, n):
        hdr = IP_UDP_HDR if self.ip else 0
        return [d + hdr for d in bare_datagrams(n)]

    def describe(self):
        tail = f" + {IP_UDP_HDR} B IPv4/UDP per datagram" if self.ip else ", no IP header"
        return (f"{SUPER_LEN} B SuperPack per 2 frames ({LONE_LEN} B lone){tail} "
                f"(lua M.budget costing)")

    def json(self):
        return {"kind": self.kind, "scheme": self.scheme, "link": self.link, "ip": self.ip,
                "unit": "bytes", "superpack_len": SUPER_LEN, "lone_len": LONE_LEN,
                "ip_udp_hdr": IP_UDP_HDR if self.ip else 0}


# ── field-test tier (Documentation/DCF_FIELD_USE.md) ────────────────────────────────────
def field_tier(med, nodes):
    """The DCF_FIELD_USE.md tier ladder that proves this medium before deployment."""
    mesh = "; T5 mesh + uplink (>= 3 nodes)" if nodes >= 3 else ""
    if isinstance(med, Hydra) or getattr(med, "link", None) == "hydramodem":
        t = ("T1 bench loopback -> T2 two interfaces, line cable "
             "(hydramodem/dcf-tools/field-test.sh; pass = PER < 1% with FEC)")
    elif isinstance(med, Afsk):
        t = ("T1/T1b acoustic loopback -> T2 wired coupling to the radios -> T3/T4 "
             "over the air; not for two-clock cabled links (300-baud AFSK has no "
             "fractional-symbol timing recovery)")
    elif med.kind == "link" and med.link.startswith("sdr"):
        t = "T1 IQ loopback (python/modem/sdr.py) -> T3 same-room OTA -> T4 field OTA"
    elif med.kind == "link" and med.link == "janus":
        t = "T1 janus-c WAV loopback -> T4 field (underwater)"
    else:
        t = "T1 software (punctim io over udp:/l2eth:impl=loop); no RF tiers for an IP medium"
    return t + mesh


# ── medium strings ───────────────────────────────────────────────────────────────────────
def _normalize(spec):
    """Strip the sim-only `link=` key and expand shorthands (udp:proto -> udp:dialect=proto,
    hydra:aux / afsk:handheld -> profile=...). Returns (uri, link or None)."""
    scheme, sep, rest = spec.strip().partition(":")
    scheme = scheme.strip().lower()
    link, items = None, []
    for it in (rest.split(",") if sep else []):
        it = it.strip()
        if not it:
            continue
        if "=" not in it:
            if scheme == "udp" and it in ("proto", "bare"):
                it = "dialect=" + it
            elif scheme in ("hydra", "afsk", "audio"):
                it = "profile=" + it
        elif it.split("=", 1)[0].strip() == "link":
            link = it.split("=", 1)[1].strip().lower()
            continue
        items.append(it)
    return scheme + (":" + ",".join(items) if items else ""), link


def parse_medium(spec):
    """A medium string -> Medium. `spec` is a punctim medium URI (dcf.medium.parse_uri grammar,
    plus the shorthands above and a sim-only `link=` for udp/l2eth), or a bare link name
    from MEDIA. SimUsage on anything else."""
    if not isinstance(spec, str) or not spec.strip():
        raise SimUsage("empty medium")
    key = spec.strip()
    if key.lower() in MEDIA:
        return Link(key, key.lower())
    from ..medium import parse_uri, UsageError
    uri, link = _normalize(key)
    try:
        scheme, kw = parse_uri(uri)
    except UsageError as e:
        raise SimUsage(f"{key}: {e} (or a link name: {', '.join(MEDIA)})") from None
    if link is not None and scheme not in ("udp", "l2eth"):
        raise SimUsage(f"{key}: link= applies to udp:/l2eth: only")
    if scheme == "hydra":
        return Hydra(key, kw)
    if scheme in ("afsk", "audio"):
        return Afsk(key, kw)
    if scheme == "udp":
        return Udp(key, kw, link)
    if scheme == "l2eth":
        return L2Eth(key, kw, link)
    if scheme == "janus":
        return Link(key, "janus")
    if scheme == "sdr":
        raise SimUsage(f"{key}: name the SDR link budget: sdr_fast or sdr_narrow")
    raise SimUsage(f"{key}: {scheme}: is not a physical medium (nothing to size); use one of "
                   f"hydra: afsk: udp: l2eth: or a link name ({', '.join(MEDIA)})")


def split_candidates(text):
    """Split a --candidates value. `;` separates when present; otherwise a comma starts a new
    candidate only before a token that names a scheme/link (so `hydra:profile=aux,fec=conv`
    keeps its own commas)."""
    if ";" in text:
        return [t.strip() for t in text.split(";") if t.strip()]
    out = []
    for tok in text.split(","):
        t = tok.strip()
        if not t:
            continue
        head = t.split(":", 1)[0].strip().lower()
        starts = ":" in t or head in MEDIA or head in SCHEME_WORDS
        if starts or not out:
            out.append(t)
        else:
            out[-1] += "," + t
    return out


__all__ = ["SimUsage", "MEDIA", "MIN_LIVE_VOICE_BPS", "QUEUE_MAX", "HW_CLASSES", "Medium",
           "Hydra", "Afsk", "Udp", "L2Eth", "Link", "parse_medium", "split_candidates",
           "field_tier", "bare_datagrams", "l2_payloads"]
