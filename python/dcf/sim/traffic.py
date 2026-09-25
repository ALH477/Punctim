# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""punctim sim -- traffic: what each adapter puts on the wire, counted by the certified codecs.

Every adapter message is fragmented by its own byte-certified packetizer (python/MCP/
{audio,game,text,sstv}lab_core.py, dcf/sense/schema.py), so frames/message is exact:

  audio  CTRL  1 + ceil(len/4) frames per 20 ms block, len <= 124 B   (DCF_AUDIO_SPEC.md)
  game   DATA  1 + ceil(len/4) frames per message,    len <= 124 B   (DCF_GAME_SPEC.md)
  text   DATA  1 + ceil(len/4) frames per message,    len <= 4092 B  (DCF_TEXT_SPEC.md)
  sstv   DATA  1 + ceil(len/4) frames per image,      len <= 8188 B  (DCF_SSTV_SPEC.md)
  sense  DATA  1 bare frame per reading                              (DCF_SENSE_SPEC.md)

Longer text / images chain across messages (FLAG_MORE), each with its own descriptor. One
message = one flush on a datagram medium. DCF-Pipe is bulk, not a rate: `pipe_plan` gives its
chunks, its ceil(N/W) rounds on a clean link, and its exact data-lane bytes.

Rate semantics: audio = blocks/s per stream x streams; game = messages/s per node x nodes;
text = aggregate bytes/s, as messages of msg_bytes (default: one message per second);
sstv = aggregate images/hour; sense = readings per node per interval x nodes.
"""
import functools
import math

from .media import SimUsage

import audiolab_core as AUD  # noqa: E402  (media.py put python/MCP on sys.path)
import gamelab_core as GAME  # noqa: E402
import textlab_core as TEXT  # noqa: E402
import sstvlab_core as SSTV  # noqa: E402

# Codec payload bytes per 20 ms block -- a mirror of lua/dcf_profile.lua M.codec_bytes.
CODEC_BYTES = {"opus-8": 20, "opus-12": 30, "opus-16": 40, "opus-24": 60, "pm": 8,
               "pcm-diag": 120}
DEFAULT_CODEC = "opus-16"          # "the voice-chat default" (lua M.codec_bytes)

ADAPTERS = ("audio", "game", "text", "sstv", "sense")
KEYS = {
    "audio": ("blocks_per_s", "payload_b", "codec", "streams", "latency_ms"),
    "game": ("msgs_per_s", "payload_b", "latency_ms"),
    "text": ("bytes_per_s", "msg_bytes", "latency_ms"),
    "sstv": ("images_per_h", "bytes"),
    "sense": ("readings_per_node", "interval_s", "latency_ms"),
    "pipe": ("bytes", "profile", "chunk_size", "nparity", "credit_window"),
}
INTERACTIVE = ("audio", "game", "text", "sense")     # judged against a latency target


@functools.lru_cache(maxsize=None)
def frames_per_message(adapter, nbytes):
    """Frames the certified packetizer emits for ONE message of `nbytes` payload bytes."""
    z = bytes(nbytes)
    if adapter == "audio":
        return len(AUD.packetize(AUD.CODEC_OPUS, z, 1, 0, 1, 2))
    if adapter == "game":
        return len(GAME.packetize(GAME.GMSG_EVENT, z, 1, 0, 1, 2))
    if adapter == "text":
        return len(TEXT.packetize(z, 1, 0, 1, 2))
    if adapter == "sstv":
        return len(SSTV.packetize(z, 1, 0, 1, 2))
    if adapter == "sense":
        from ..sense.schema import encode_reading, SENSORS
        frame = encode_reading(1, min(SENSORS), 0.0)          # one reading = one bare frame
        return len(frame) // 17
    raise KeyError(adapter)


FRAME_TYPE = {"audio": "CTRL", "game": "DATA", "text": "DATA", "sstv": "DATA",
              "sense": "DATA"}
MAX_PAYLOAD = {"audio": AUD.MAX_PAYLOAD, "game": GAME.MAX_PAYLOAD, "text": TEXT.MAX_PAYLOAD,
               "sstv": SSTV.MAX_PAYLOAD}


class Adapter:
    """One adapter's offered load. `units` = frame count of each flush in one message unit
    (more than one only when a long text/image chains across messages)."""

    def __init__(self, name, msg_bytes, msgs_per_s, label, latency_ms=None, extra=None):
        self.name = name
        self.frame_type = FRAME_TYPE[name]
        self.msg_bytes = msg_bytes
        self.msgs_per_s = msgs_per_s
        self.label = label
        self.latency_ms = latency_ms
        self.extra = extra or {}
        if name == "sense":                  # a node's readings go out in its one slot
            self.units = [frames_per_message("sense", 0) * self.extra["readings_per_node"]]
        else:
            cap = MAX_PAYLOAD[name]
            parts = [cap] * (msg_bytes // cap) + ([msg_bytes % cap] if msg_bytes % cap else [])
            self.units = [frames_per_message(name, b) for b in (parts or [0])]

    @property
    def frames_per_msg(self):
        return sum(self.units)

    @property
    def frames_per_s(self):
        return self.frames_per_msg * self.msgs_per_s

    @property
    def interactive(self):
        return self.name in INTERACTIVE

    def json(self):
        d = {"frame_type": self.frame_type, "msg_bytes": self.msg_bytes,
             "frames_per_msg": self.frames_per_msg, "flushes_per_msg": len(self.units),
             "msgs_per_s": self.msgs_per_s, "frames_per_s": self.frames_per_s,
             "latency_target_ms": self.latency_ms if self.interactive else None}
        d.update(self.extra)
        return d


# ── validation helpers ──────────────────────────────────────────────────────────────────
def num(v, what, lo=None, hi=None, integer=False, lo_open=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (
            isinstance(v, float) and not math.isfinite(v)):
        raise SimUsage(f"{what}: want a number, got {v!r}")
    if integer and (isinstance(v, float) and not v.is_integer()):
        raise SimUsage(f"{what}: want an integer, got {v!r}")
    if integer:
        v = int(v)
    if lo is not None and (v <= lo if lo_open else v < lo):
        raise SimUsage(f"{what}: {v!r} must be {'>' if lo_open else '>='} {lo}")
    if hi is not None and v > hi:
        raise SimUsage(f"{what}: {v!r} must be <= {hi}")
    return v


def _known(section, d):
    if not isinstance(d, dict):
        raise SimUsage(f"traffic.{section}: want an object, got {d!r}")
    bad = sorted(set(d) - set(KEYS[section]))
    if bad:
        raise SimUsage(f"traffic.{section}: unknown key(s) {', '.join(bad)} "
                       f"(keys: {', '.join(KEYS[section])})")


def build_traffic(traffic, nodes, latency_ms):
    """Validate the `traffic` object -> ([Adapter...] in ADAPTERS order, pipe dict or None)."""
    if traffic is None:
        traffic = {}
    if not isinstance(traffic, dict):
        raise SimUsage(f"traffic: want an object, got {traffic!r}")
    bad = sorted(set(traffic) - set(KEYS))
    if bad:
        raise SimUsage(f"traffic: unknown adapter(s) {', '.join(bad)} "
                       f"(one of: {', '.join(KEYS)})")
    out = []
    for name in ADAPTERS:
        if name not in traffic or traffic[name] is None:
            continue
        t = traffic[name]
        _known(name, t)
        lat = t.get("latency_ms", latency_ms)
        if lat is not None:
            lat = num(lat, f"traffic.{name}.latency_ms", 0, lo_open=True)
        if name == "audio":
            if "payload_b" in t and "codec" in t:
                raise SimUsage("traffic.audio: give payload_b or codec, not both")
            codec = t.get("codec", None if "payload_b" in t else DEFAULT_CODEC)
            if codec is not None and codec not in CODEC_BYTES:
                raise SimUsage(f"traffic.audio.codec: {codec!r} (one of: "
                               f"{', '.join(CODEC_BYTES)})")
            pb = CODEC_BYTES[codec] if codec else num(t["payload_b"], "traffic.audio.payload_b",
                                                      0, AUD.MAX_PAYLOAD, integer=True)
            bps = num(t.get("blocks_per_s", 50), "traffic.audio.blocks_per_s", 0, lo_open=True)
            streams = num(t.get("streams", 1), "traffic.audio.streams", 1, integer=True)
            lbl = (f"{pb} B/block{' (' + codec + ')' if codec else ''} x {bps:g} blocks/s x "
                   f"{streams} stream{'s' if streams != 1 else ''}")
            out.append(Adapter("audio", pb, bps * streams, lbl, lat,
                               {"blocks_per_s": bps, "streams": streams, "codec": codec}))
        elif name == "game":
            pb = num(t.get("payload_b", GAME.SNAPSHOT_LEN), "traffic.game.payload_b", 0,
                     GAME.MAX_PAYLOAD, integer=True)
            r = num(t.get("msgs_per_s", 20), "traffic.game.msgs_per_s", 0, lo_open=True)
            out.append(Adapter("game", pb, r * nodes,
                               f"{pb} B/msg x {r:g} msg/s/node x {nodes} nodes", lat,
                               {"msgs_per_s_per_node": r}))
        elif name == "text":
            bps = num(t.get("bytes_per_s", 20), "traffic.text.bytes_per_s", 0, lo_open=True)
            mb = t.get("msg_bytes")
            mb = (max(1, math.ceil(bps)) if mb is None else
                  num(mb, "traffic.text.msg_bytes", 1, integer=True))
            out.append(Adapter("text", mb, bps / mb,
                               f"{bps:g} B/s as {mb} B messages", lat,
                               {"bytes_per_s": bps}))
        elif name == "sstv":
            n = num(t.get("bytes", SSTV.MAX_PAYLOAD), "traffic.sstv.bytes", 0, integer=True)
            iph = num(t.get("images_per_h", 1), "traffic.sstv.images_per_h", 0, lo_open=True)
            out.append(Adapter("sstv", n, iph / 3600.0, f"{n} B/image x {iph:g} images/h",
                               None, {"images_per_h": iph}))
        elif name == "sense":
            k = num(t.get("readings_per_node", 1), "traffic.sense.readings_per_node", 1,
                    integer=True)
            iv = num(t.get("interval_s", 60), "traffic.sense.interval_s", 0, lo_open=True)
            out.append(Adapter("sense", 3 * k, nodes / iv,
                               f"{k} reading{'s' if k != 1 else ''}/node every {iv:g} s x "
                               f"{nodes} nodes", lat,
                               {"readings_per_node": k, "interval_s": iv}))
    pipe = traffic.get("pipe")
    if pipe is not None:
        _known("pipe", pipe)
    return out, pipe


# ── DCF-Pipe (bulk) ─────────────────────────────────────────────────────────────────────
def pipe_plan(spec, default_profile):
    """Exact Pipe arithmetic for one object: chunks N, ceil(N/W) rounds on a clean link, and
    the data-lane datagrams [(length, count)] (6-B chunk header + payload, FEC-wrapped by the
    certified DCF-FEC layer iff nparity > 0)."""
    import pipelab_core as pc
    import feclab_core as fec
    from ..pipe.protocol import PROFILES
    prof = spec.get("profile", default_profile)
    if prof not in PROFILES:
        raise SimUsage(f"traffic.pipe.profile: {prof!r} (one of: {', '.join(PROFILES)})")
    p = dict(PROFILES[prof])
    for k in ("chunk_size", "nparity", "credit_window"):
        if k in spec:
            p[k] = num(spec[k], f"traffic.pipe.{k}", 0 if k == "nparity" else 1, integer=True)
    if p["nparity"] > 254:
        raise SimUsage("traffic.pipe.nparity: want 0..254 (RS(255) parity bytes)")
    nbytes = num(spec.get("bytes", 1 << 20), "traffic.pipe.bytes", 0, integer=True)
    n = pc.num_chunks(nbytes, p["chunk_size"])
    rounds = -(-n // p["credit_window"]) if n else 0

    def dgram(sz):
        body = sz if p["nparity"] == 0 else len(fec.encode_message(bytes(sz), p["nparity"]))
        return pc.CHUNK_HDR_LEN + body

    sizes = []
    if n:
        last = nbytes - (n - 1) * p["chunk_size"]
        if n > 1:
            sizes.append((dgram(p["chunk_size"]), n - 1))
        sizes.append((dgram(last), 1))
    lane = sum(ln * c for ln, c in sizes)
    return {"bytes": nbytes, "profile": prof, "chunk_size": p["chunk_size"],
            "nparity": p["nparity"], "credit_window": p["credit_window"], "chunks": n,
            "rounds": rounds, "chunk_hdr_len": pc.CHUNK_HDR_LEN,
            "datagrams": [{"len": ln, "count": c} for ln, c in sizes],
            "data_lane_bytes": lane,
            "overhead_bytes": lane - nbytes}
