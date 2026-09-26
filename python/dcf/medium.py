# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF-Medium runtime — the medium URI grammar, the medium factory and the ordered
reader -> frame gate -> writer pipeline behind ``punctim io`` (and ``dcf-bridge -t``).

A medium is named by a URI ``SCHEME[:k=v,...]`` (single-sourced here in ``parse_uri``,
mirrored by every language's ``punctim``)::

    file:path=a.dcf[,append=0|1][,follow=0|1][,mode=r|w|rw]     .dcf stream (byte-wise resync)
    stdio:                                                      .dcf stream on stdin/stdout
    hex:[path=f.hex][,append=0|1][,follow=0|1]                  34 hex chars per line
    udp:dialect=proto|bare,bind=host:port,peer=h:p|h:p,pair=1,flush_ms=20,ts=0|now,seq_start=1
    l2eth:if=eth0,ethertype=0x88B5,dst=ff:ff:ff:ff:ff:ff,mtu=1500,impl=raw|loop[,id=]
    loop:id=NAME                                                in-process broadcast
    hydra:in=DIR,out=DIR,profile=default|aux|melody|chime|nocturne,fec=none|rep3|conv,interleave=0|1,
          base_freq=,tone_spacing=,baud=,n_tones=,impl=tool|cffi,tx=,rx=
    afsk:in=DIR,out=DIR,profile=standard|handheld|aux-cable,fec=0|1      (audio: = alias)
    sdr:in=DIR,out=DIR,mod=gfsk    janus:in=DIR,out=DIR,pset=1,fs=48000,pset_file=,tx=,rx=
    mc:rcon=host:port,pass_file=F|fifo=PATH,log=PATH|bot=ARGV,egress=console|chat,ns=dcf,poll_hz=4
                                                                (a Minecraft world's register; Python-only)

Every scheme accepts ``name=``. ``|`` separates multiple values (``peer=``). The codecs
themselves are the certified reference in python/MCP/mediumlab_core.py; this module only
opens the media. Normative spec: Documentation/DCF_MEDIUM_SPEC.md.
"""
import json
import os
import sys
import time

for _mcp in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "MCP")):
    if os.path.isdir(_mcp) and _mcp not in sys.path:
        sys.path.insert(0, _mcp)
import mediumlab_core as M  # noqa: E402
import wirelab_core as wire  # noqa: E402

from . import transport as T  # noqa: E402
from .transport import MediumUnsupported  # noqa: E402,F401  (re-exported)

FRAME_LEN = 17


class UsageError(ValueError):
    """A malformed medium URI or option (``punctim`` exit code 2)."""


# ── the URI grammar ───────────────────────────────────────────────────────────
SCHEMES = {
    "file": ("path", "mode", "append", "follow", "in", "out"),
    "stdio": (),
    "hex": ("path", "mode", "append", "follow"),
    "udp": ("dialect", "bind", "peer", "pair", "flush_ms", "ts", "seq_start"),
    "l2eth": ("if", "ethertype", "dst", "mtu", "impl", "id", "flush_ms"),
    "loop": ("id",),
    "hydra": ("in", "out", "profile", "fec", "interleave", "base_freq", "tone_spacing",
              "baud", "n_tones", "impl", "tx", "rx"),
    "afsk": ("in", "out", "profile", "fec"),
    "audio": ("in", "out", "profile", "fec"),
    "sdr": ("in", "out", "mod"),
    "janus": ("in", "out", "pset", "fs", "pset_file", "tx", "rx"),
    "mc": ("rcon", "pass_file", "pass_env", "fifo", "log", "bot", "egress", "ns", "poll_hz"),
}
COMMON_KEYS = ("name",)
AFSK_PROFILE_NAMES = ("standard", "handheld", "aux-cable")


def parse_uri(spec):
    """``SCHEME[:k=v,...]`` -> (scheme, {k: v}). The scheme is case-insensitive; keys are
    validated against the scheme (plus ``name``); values are kept verbatim as strings
    (a later duplicate key wins; ``|`` inside a value separates multiple values — see
    ``multi``). Raises UsageError on an unknown scheme/key or an item without ``=``."""
    if not isinstance(spec, str) or not spec.strip():
        raise UsageError("empty medium URI")
    scheme, _, rest = spec.partition(":")
    scheme = scheme.strip().lower()
    if scheme not in SCHEMES:
        raise UsageError(f"unknown medium {scheme!r} (one of: {', '.join(SCHEMES)})")
    allowed = set(SCHEMES[scheme]) | set(COMMON_KEYS)
    kw = {}
    for item in rest.split(","):
        if not item:
            continue
        if "=" not in item:
            raise UsageError(f"{scheme}: bad item {item!r} (want key=value)")
        k, v = item.split("=", 1)
        k = k.strip()
        if k not in allowed:
            raise UsageError(f"{scheme}: unknown key {k!r} (keys: "
                             f"{', '.join(sorted(allowed))})")
        kw[k] = v
    return scheme, kw


def multi(value):
    """Split a ``|``-separated multi-value (empty parts dropped)."""
    return [v for v in (value or "").split("|") if v]


def finite(spec):
    """True iff ``punctim io --in spec`` is a finite input (read to EOF, never drops):
    stdio, hex without follow, and file without follow. Everything else is infinite
    (runs until --count / --seconds / SIGINT)."""
    scheme, kw = parse_uri(spec)
    if scheme == "stdio":
        return True
    if scheme in ("hex", "file"):
        return not _bool(kw.get("follow", "0"), "follow")
    return False


# ── value parsing ─────────────────────────────────────────────────────────────
def _bool(v, key):
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise UsageError(f"{key}={v!r}: want 0|1")


def _int(v, key):
    s = str(v).strip()
    try:
        return int(s[2:], 16) if s.lower().startswith("0x") else int(s, 10)
    except ValueError:
        raise UsageError(f"{key}={v!r}: want an integer (decimal or 0x hex)") from None


def _num(v, key):
    try:
        return float(v)
    except ValueError:
        raise UsageError(f"{key}={v!r}: want a number") from None


def _hostport(s, key):
    host, sep, port = s.rpartition(":")
    if not sep or not host:
        raise UsageError(f"{key}={s!r}: want host:port")
    return host, _int(port, key)


def _peers(v):
    # accepts host:port and the legacy id@host:port
    return [_hostport(p.split("@")[-1], "peer") for p in multi(v)]


def _mac(v):
    parts = v.replace("-", ":").split(":")
    if len(parts) != 6:
        raise UsageError(f"dst={v!r}: want a MAC aa:bb:cc:dd:ee:ff")
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError:
        raise UsageError(f"dst={v!r}: want a MAC aa:bb:cc:dd:ee:ff") from None


def _choice(v, key, choices):
    if v not in choices:
        raise UsageError(f"{key}={v!r}: want {'|'.join(choices)}")
    return v


# ── the transport factory (dcf-bridge -t, and punctim io's infinite media) ────
def make_transport(spec, direction=None):
    """Build a ``dcf.transport.Transport`` for a medium URI. ``direction`` is None for a
    bidirectional bridge port, or "in"/"out" for one side of ``punctim io`` (it picks the
    file/hex direction and their defaults: io reads without follow and writes truncating;
    the bridge keeps its historical tail-forever/append behaviour)."""
    scheme, kw = parse_uri(spec)
    name = kw.pop("name", scheme)
    g = kw.get

    if scheme == "file":
        path = g("path")
        bridge = direction is None
        follow = _bool(g("follow", "1" if bridge else "0"), "follow")
        append = _bool(g("append", "1" if bridge else "0"), "append")
        in_path, out_path = g("in"), g("out")
        if path:
            mode = g("mode", {"in": "r", "out": "w"}.get(direction, "rw"))
            _choice(mode, "mode", ("r", "w", "rw"))
            if "r" in mode:
                in_path = in_path or path
            if "w" in mode:
                out_path = out_path or path
        if direction == "in":
            out_path = None
        elif direction == "out":
            in_path = None
        if not (in_path or out_path):
            raise UsageError("file: needs path= (or in=/out=)")
        return T.FileTransport(name, out_path=out_path, in_path=in_path, follow=follow,
                               append=append)

    if scheme == "stdio":
        return T.StdioTransport(name, read=direction != "out", write=direction != "in")

    if scheme == "hex":
        mode = g("mode", {"in": "r", "out": "w"}.get(direction, "rw"))
        _choice(mode, "mode", ("r", "w", "rw"))
        if direction == "in":
            mode = "r"
        elif direction == "out":
            mode = "w"
        return T.HexTransport(name, path=g("path") or None, mode=mode,
                              follow=_bool(g("follow", "0"), "follow"),
                              append=_bool(g("append", "0"), "append"))

    if scheme == "udp":
        bind = _hostport(g("bind", "0.0.0.0:0"), "bind")
        return T.UdpTransport(name, bind=bind, peers=_peers(g("peer", "")),
                              dialect=_choice(g("dialect", "proto"), "dialect", ("proto", "bare")),
                              pair=_bool(g("pair", "1"), "pair"),
                              flush_ms=_int(g("flush_ms", "20"), "flush_ms"),
                              ts=_choice(g("ts", "0"), "ts", ("0", "now")),
                              seq_start=_int(g("seq_start", "1"), "seq_start"))

    if scheme == "l2eth":
        from . import l2eth
        impl = _choice(g("impl", "raw" if g("if") else "loop"), "impl", ("raw", "loop"))
        if impl == "loop":
            return l2eth.L2LoopbackTransport(name, T.loop_medium("l2eth/" + g("id", "l2eth")))
        if not g("if"):
            raise UsageError("l2eth:impl=raw needs if=<interface>")
        return l2eth.L2EthTransport(name, g("if"),
                                    ethertype=_int(g("ethertype", "0x88B5"), "ethertype"),
                                    mtu=_int(g("mtu", "1500"), "mtu"),
                                    dst_mac=_mac(g("dst", "ff:ff:ff:ff:ff:ff")))

    if scheme == "loop":
        return T.LoopbackTransport(name, T.loop_medium(g("id", "default")))

    if scheme == "hydra":
        opts = dict(out_dir=g("out"), in_dir=g("in"),
                    fec=_choice(g("fec", "conv"), "fec", ("none", "rep3", "conv")),
                    profile=_choice(g("profile", "default"), "profile", T.HYDRA_PROFILES),
                    interleave=None if g("interleave") is None
                    else int(_bool(g("interleave"), "interleave")))
        for k, conv in (("base_freq", _num), ("tone_spacing", _num), ("baud", _num),
                        ("n_tones", _int)):
            if g(k) is not None:
                v = conv(g(k), k)
                opts[k] = int(v) if isinstance(v, float) and v.is_integer() else v
        impl = _choice(g("impl", "tool"), "impl", ("tool", "cffi"))
        if impl == "cffi":
            return T.HydraCffiTransport(name, **opts)
        # HydraModem (M-FSK + soft-Viterbi FEC) via the frame_tx/frame_rx subprocess PHY.
        return T.HydraTransport(name, tx_bin=g("tx"), rx_bin=g("rx"), **opts)

    if scheme in ("afsk", "audio"):
        return T.AudioTransport(name, out_dir=g("out"), in_dir=g("in"),
                                profile=_choice(g("profile", "handheld"), "profile",
                                                AFSK_PROFILE_NAMES),
                                fec=_bool(g("fec", "0"), "fec"))

    if scheme == "sdr":
        return T.SdrTransport(name, out_dir=g("out"), in_dir=g("in"), mod=g("mod", "gfsk"))

    if scheme == "janus":
        # STANAG-4748 via the GPL janus-c reference (separate process). Carries the
        # frame as JANUS cargo; see Documentation/DCF_JANUS_SPEC.md.
        return T.JanusTransport(name, out_dir=g("out"), in_dir=g("in"),
                                pset_id=_int(g("pset", "1"), "pset"),
                                fs=_int(g("fs", "48000"), "fs"),
                                pset_file=g("pset_file"), tx_bin=g("tx"), rx_bin=g("rx"))

    if scheme == "mc":
        # A Minecraft world's DeModFrame register (Documentation/DCF_MINECRAFT_SPEC.md) over
        # RCON / the console FIFO / a bot helper, or chat-log egress only. Python-only.
        from .minecraft.factory import make_minecraft
        return make_minecraft(name, g, direction)

    raise UsageError(f"unknown medium {scheme!r}")  # pragma: no cover - parse_uri guards


# ── readers ───────────────────────────────────────────────────────────────────
class _Reader:
    finite = True
    skipped_bytes = 0
    bad_lines = 0
    invalid = 0          # medium units that carried no frame (bad datagram / payload)

    def close(self):
        pass


class _StreamReader(_Reader):
    """Finite .dcf stream (file without follow, or stdin): byte-wise resync scan."""

    def __init__(self, fp, owned):
        self._fp, self._owned = fp, owned
        self._sc = M.StreamScanner()
        self.tail_bytes = 0

    @property
    def skipped_bytes(self):
        return self._sc.skipped_bytes

    def __iter__(self):
        read = getattr(self._fp, "read1", self._fp.read)
        while True:
            chunk = read(65536)
            if not chunk:
                break
            yield from self._sc.feed(chunk)
        self.tail_bytes = len(self._sc.flush())

    def close(self):
        if self._owned:
            self._fp.close()


class _HexReader(_Reader):
    """Finite hex lines (file without follow, or stdin)."""

    def __init__(self, fp, owned):
        self._fp, self._owned = fp, owned

    def __iter__(self):
        for raw in self._fp:
            frames, bad = M.hex_decode(raw.decode("latin-1"))
            self.bad_lines += bad
            yield from frames

    def close(self):
        if self._owned:
            self._fp.close()


class _TransportReader(_Reader):
    """An infinite medium: a Transport delivering frames on its own receive thread."""
    finite = False

    def __init__(self, t):
        self.t = t

    def start(self, sink):
        self.t.start(lambda frame, meta: sink(frame))

    def stop(self):
        self.t.stop()

    @property
    def skipped_bytes(self):
        return getattr(self.t, "skipped_bytes", 0)

    @property
    def bad_lines(self):
        return getattr(self.t, "bad_lines", 0)

    @property
    def invalid(self):
        return getattr(self.t, "invalid_datagrams", 0)


def open_reader(spec):
    """Open the input side of ``punctim io``. Finite media (stdio, hex/file without
    follow) are iterated synchronously; the rest wrap a started Transport."""
    scheme, kw = parse_uri(spec)
    if scheme == "stdio":
        return _StreamReader(sys.stdin.buffer, owned=False)
    if scheme in ("file", "hex") and finite(spec):
        path = kw.get("path") or (kw.get("in") if scheme == "file" else None)
        if scheme == "file" and not path:
            raise UsageError("file: needs path=")
        fp = open(path, "rb") if path else sys.stdin.buffer
        return (_StreamReader if scheme == "file" else _HexReader)(fp, owned=bool(path))
    if scheme == "udp" and "bind" not in kw:
        raise UsageError("udp as input needs bind=host:port")
    if scheme == "mc" and not (kw.get("rcon") or kw.get("fifo") or kw.get("bot") or kw.get("log")):
        raise UsageError("mc as input needs rcon=, fifo=, bot= or log=")
    if scheme in ("hydra", "afsk", "audio", "sdr", "janus") and not kw.get("in"):
        raise UsageError(f"{scheme} as input needs in=<dir>")
    return _TransportReader(make_transport(spec, "in"))


# ── writers ───────────────────────────────────────────────────────────────────
class _Writer:
    def write(self, frame):
        raise NotImplementedError

    def poll(self):
        """Time-based flushes (bare-UDP lone frame, l2eth partial batch)."""

    def flush(self):
        pass

    def close(self):
        self.flush()


class _StreamWriter(_Writer):
    def __init__(self, fp, owned):
        self._fp, self._owned = fp, owned

    def write(self, frame):
        self._fp.write(frame)

    def flush(self):
        self._fp.flush()

    def close(self):
        self.flush()
        if self._owned:
            self._fp.close()


class _HexWriter(_StreamWriter):
    def write(self, frame):
        self._fp.write((bytes(frame).hex() + "\n").encode("ascii"))


class _TransportWriter(_Writer):
    """Synchronous writes through a Transport (no queue: never drops, never reorders)."""

    def __init__(self, t):
        self.t = t

    def write(self, frame):
        self.t.send_now(frame)

    def poll(self):
        self.t._idle()

    def flush(self):
        self.t.flush()

    def close(self):
        self.t.stop()                  # flushes, then closes sockets/files


class _L2Writer(_Writer):
    """l2eth: batch up to l2_capacity(mtu) frames per Ethernet payload; a partial batch
    goes out after flush_ms or at close. Frames that fail the gate cannot be SuperPacked
    (ValueError -> counted as invalid by the pipeline)."""

    def __init__(self, t, capacity, flush_ms):
        self.t = t
        self._cap = max(1, capacity)
        self._flush_s = flush_ms / 1000.0
        self._pending = []
        self._t0 = 0.0

    def write(self, frame):
        if not M.gate(frame):
            raise ValueError("l2eth cannot carry a frame that fails the gate")
        if not self._pending:
            self._t0 = time.monotonic()
        self._pending.append(bytes(frame))
        if len(self._pending) >= self._cap:
            self.flush()

    def poll(self):
        if self._pending and time.monotonic() - self._t0 >= self._flush_s:
            self.flush()

    def flush(self):
        if self._pending:
            batch, self._pending = self._pending, []
            self.t.send_batch(batch)

    def close(self):
        self.flush()
        self.t.stop()


def open_writer(spec):
    """Open the output side of ``punctim io``."""
    scheme, kw = parse_uri(spec)
    if scheme == "stdio":
        return _StreamWriter(sys.stdout.buffer, owned=False)
    if scheme in ("file", "hex"):
        path = kw.get("path") or (kw.get("out") if scheme == "file" else None)
        if scheme == "file" and not path:
            raise UsageError("file: needs path=")
        append = _bool(kw.get("append", "0"), "append")
        fp = open(path, "ab" if append else "wb") if path else sys.stdout.buffer
        return (_StreamWriter if scheme == "file" else _HexWriter)(fp, owned=bool(path))
    if scheme == "udp" and not kw.get("peer"):
        raise UsageError("udp as output needs peer=host:port")
    if scheme == "mc" and not (kw.get("rcon") or kw.get("fifo") or kw.get("bot")):
        raise UsageError("mc as output needs a console: rcon=, fifo= or bot=")
    if scheme in ("hydra", "afsk", "audio", "sdr", "janus") and not kw.get("out"):
        raise UsageError(f"{scheme} as output needs out=<dir>")
    t = make_transport(spec, "out")
    if scheme == "l2eth":
        return _L2Writer(t, M.l2_capacity(_int(kw.get("mtu", "1500"), "mtu")),
                         _int(kw.get("flush_ms", "20"), "flush_ms"))
    return _TransportWriter(t)


# ── the pipeline ──────────────────────────────────────────────────────────────
STATS_KEYS = ("in", "out", "frames_in", "frames_out", "invalid_frames", "skipped_bytes",
              "bad_lines", "dropped", "seconds")


def run_io(in_spec, out_spec, count=None, seconds=None, expect=None, validate=True,
           queue=256, stop=None):
    """The single-threaded ordered pipeline reader -> frame gate -> writer. Returns the
    stats dict (STATS_KEYS order). Finite inputs run to EOF and never drop; infinite
    inputs run until `count` frames were written, `seconds` elapsed, `stop` (a
    threading.Event) is set, or SIGINT — and, with `expect` but no `count`, until `expect`
    frames were written. Their frames cross a bounded FIFO of `queue` items (oldest shed,
    counted in `dropped`)."""
    t0 = time.monotonic()
    reader = open_reader(in_spec)
    try:
        writer = open_writer(out_spec)
    except BaseException:
        reader.close()
        raise
    st = {"frames_in": 0, "frames_out": 0, "invalid_frames": 0, "dropped": 0}
    limit = count
    if limit is None and expect is not None and not reader.finite:
        limit = expect
    deadline = None if seconds is None else t0 + seconds

    def handle(frame):
        st["frames_in"] += 1
        if validate and not M.gate(frame):
            st["invalid_frames"] += 1
            return
        try:
            writer.write(frame)
        except ValueError:                   # a medium that cannot carry this frame
            st["invalid_frames"] += 1
            return
        st["frames_out"] += 1

    def done():
        return limit is not None and st["frames_out"] >= limit

    try:
        if reader.finite:
            if not done():
                for fr in reader:
                    handle(fr)
                    if done() or (deadline is not None and time.monotonic() >= deadline) \
                            or (stop is not None and stop.is_set()):
                        break
        else:
            q = T.OutboundQueue(max(1, queue))
            reader.start(lambda fr: q.put(fr))
            try:
                while not done():
                    item = q.get()
                    if item is None:
                        writer.poll()
                        if (deadline is not None and time.monotonic() >= deadline) or \
                                (stop is not None and stop.is_set()):
                            break
                        time.sleep(0.002)
                        continue
                    handle(item)
            except KeyboardInterrupt:
                pass
            finally:
                reader.stop()
            while not done():                # frames already received are not dropped
                item = q.get()
                if item is None:
                    break
                handle(item)
            st["dropped"] = q.dropped
    except KeyboardInterrupt:
        pass
    finally:
        try:
            writer.close()
        finally:
            reader.close()
    return {"in": in_spec, "out": out_spec,
            "frames_in": st["frames_in"], "frames_out": st["frames_out"],
            "invalid_frames": st["invalid_frames"] + reader.invalid,
            "skipped_bytes": reader.skipped_bytes, "bad_lines": reader.bad_lines,
            "dropped": st["dropped"], "seconds": round(time.monotonic() - t0, 3)}


# ── certification against medium_vectors.json ────────────────────────────────
FAMILIES = ("stream", "hex", "udp_proto", "udp_bare", "l2eth", "hydra_symbols", "afsk_bits")


def find_vectors(explicit=None):
    """Locate medium_vectors.json: an explicit dir/file, then $PUNCTIM_VECTORS, then
    Documentation/ found by walking up from this file, then the python/MCP copy."""
    cands = []
    for base in (explicit, os.environ.get("PUNCTIM_VECTORS")):
        if base:
            cands.append(base if base.endswith(".json") else
                         os.path.join(base, "medium_vectors.json"))
            if explicit:
                break
    if not explicit:
        d = os.path.dirname(os.path.abspath(__file__))
        for _ in range(6):
            cands.append(os.path.join(d, "Documentation", "medium_vectors.json"))
            d = os.path.dirname(d)
        for mcp in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP"),
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "MCP")):
            cands.append(os.path.join(mcp, "medium_vectors.json"))
    for c in cands:
        if os.path.isfile(c):
            return os.path.normpath(c)
    raise FileNotFoundError("medium_vectors.json not found (use --vectors DIR or "
                            "$PUNCTIM_VECTORS)")


def _fx(hexes):
    return [bytes.fromhex(h) for h in hexes]


def _cert_stream(fam, _):
    for c in fam["cases"]:
        buf, want = bytes.fromhex(c["input"]), _fx(c["frames"])
        got = M.stream_decode(buf)
        if got != (want, c["skipped_bytes"], c["tail_bytes"]):
            return f"{c['name']}: stream_decode mismatch"
        for step in (1, 7, 16, 17, 64):
            sc = M.StreamScanner()
            fr = []
            for i in range(0, len(buf), step):
                fr += sc.feed(buf[i:i + step])
            if fr != want or sc.skipped_bytes != c["skipped_bytes"] or \
                    len(sc.flush()) != c["tail_bytes"]:
                return f"{c['name']}: StreamScanner mismatch at chunk {step}"
        if M.stream_decode(M.stream_encode(want)) != (want, 0, 0):
            return f"{c['name']}: stream_encode round trip"
    return None


def _cert_hex(fam, _):
    for c in fam["cases"]:
        if M.hex_encode(_fx(c["frames"])) != c["text"]:
            return f"{c['name']}: hex_encode mismatch"
        if M.hex_decode(c["decode_input"]) != (_fx(c["decoded"]), c["bad_lines"]):
            return f"{c['name']}: hex_decode mismatch"
    return None


def _cert_proto(fam, _):
    if fam["header_len"] != M.PROTO_HEADER_LEN or fam["msg_frame"] != M.MSG_FRAME or \
            fam["types"] != M.MSG_TYPES:
        return "type registry / header mismatch"
    for c in fam["cases"]:
        pl, dg = bytes.fromhex(c["payload"]), bytes.fromhex(c["datagram"])
        if int(c["ts_hex"], 16) != c["ts"]:
            return f"{c['name']}: ts_hex != ts"
        if M.proto_encode(c["type"], c["seq"], c["ts"], pl) != dg:
            return f"{c['name']}: proto_encode mismatch"
        if M.proto_decode(dg) != (c["type"], c["seq"], c["ts"], pl):
            return f"{c['name']}: proto_decode mismatch"
        fr = M.proto_frame_decode(dg)
        if (fr is not None) != c["accept_as_frame"]:
            return f"{c['name']}: accept_as_frame mismatch"
        if fr is not None and (fr != pl or M.proto_frame_encode(pl, c["seq"], c["ts"]) != dg):
            return f"{c['name']}: proto_frame round trip"
    return None


def _cert_bare(fam, _):
    for c in fam["cases"]:
        fr, dgs = _fx(c["frames"]), _fx(c["datagrams"])
        if M.bare_encode(fr) != dgs:
            return f"{c['name']}: bare_encode mismatch"
        if [f for d in dgs for f in M.bare_decode(d)] != fr:
            return f"{c['name']}: bare_decode mismatch"
    return None


def _cert_l2(fam, _):
    if bytes.fromhex(fam["filler"]) != M.L2_FILLER or fam["hdr"] != M.L2_HDR:
        return "filler / header mismatch"
    for c in fam["cases"]:
        fr, pl = _fx(c["frames"]), bytes.fromhex(c["payload"])
        if M.l2_batch(fr) != pl:
            return f"{c['name']}: l2_batch mismatch"
        if M.l2_unbatch(pl) != fr:
            return f"{c['name']}: l2_unbatch mismatch"
    return None


def _cert_hydra(fam, _):
    for name, prof in fam["profiles"].items():
        if {k: M.HYDRA_PROFILES[name][k] for k in M.HYDRA_USER_FIELDS} != prof:
            return f"profile {name} mismatch"
    for c in fam["cases"]:
        p = M.hydra_profile(c["profile"], fec=c["fec"], interleave=c["interleave"],
                            n_tones=c["n_tones"])
        if (p["coded_bits"], p["interleave_stride"], p["total_syms"]) != \
                (c["coded_bits"], c["interleave_stride"], c["total_syms"]):
            return f"{c['name']}: derived profile fields mismatch"
        f = bytes.fromhex(c["frame"])
        if M.hydra_symbols_encode(p, f) != c["symbols"]:
            return f"{c['name']}: symbols mismatch"
        if M.hydra_symbols_decode(p, c["symbols"]) != f:
            return f"{c['name']}: decode mismatch"
    return None


def _cert_afsk(fam, _):
    if fam["profiles"] != M.AFSK_PROFILES:
        return "profiles mismatch"
    for c in fam["cases"]:
        f = bytes.fromhex(c["frame"])
        b = M.afsk_bits_encode(f, c["profile"], c["fec"])
        if b != c["bits"] or len(b) != c["n_bits"]:
            return f"{c['name']}: bits mismatch"
        if M.afsk_bits_decode(b, c["profile"], c["fec"]) != f:
            return f"{c['name']}: decode mismatch"
    return None


_CERTS = {"stream": _cert_stream, "hex": _cert_hex, "udp_proto": _cert_proto,
          "udp_bare": _cert_bare, "l2eth": _cert_l2, "hydra_symbols": _cert_hydra,
          "afsk_bits": _cert_afsk}


def certify_vectors(vectors, families=None):
    """Check the reference codecs against a medium_vectors.json dict. Returns a list of
    (family, ok, n_cases, message) — family "anchors" first."""
    out = []
    a = vectors.get("anchors", {})
    bad = None
    try:
        af = M._afsk()
        checks = [
            (wire.crc16_ccitt(b"123456789"), a.get("crc_123456789")),
            (wire.crc16_ccitt(bytes(15)), a.get("crc_zero15")),
            (FRAME_LEN, a.get("frame_len")), (M.PROTO_HEADER_LEN, a.get("proto_header_len")),
            (M.MSG_FRAME, a.get("msg_frame")), (32, a.get("super_len")),
            (M.L2_HDR, a.get("l2_hdr")), (M.L2_FILLER.hex(), a.get("l2_filler")),
            (0x2DD4, a.get("hydra_sync_word")), (af.SYNC_WORD, a.get("afsk_sync")),
            (af.crc8(b"123456789"), a.get("afsk_crc8_123456789")),
        ]
        if any(x != y for x, y in checks):
            bad = "anchor mismatch"
        for b in vectors.get("basis", []):
            f = wire.encode(b["type"], b["seq"], b["src"], b["dst"],
                            bytes.fromhex(b["payload"]), b["ts"])
            if f.hex() != b["hex"]:
                bad = f"basis {b['id']} mismatch"
    except Exception as e:  # malformed vectors
        bad = f"{type(e).__name__}: {e}"
    out.append(("anchors", bad is None, len(vectors.get("basis", [])), bad or ""))
    fams = vectors.get("families", {})
    for name in (families or FAMILIES):
        if name not in _CERTS:
            raise UsageError(f"unknown family {name!r} (one of: {', '.join(FAMILIES)})")
        fam = fams.get(name)
        if fam is None:
            out.append((name, False, 0, "family missing from vectors"))
            continue
        try:
            msg = _CERTS[name](fam, vectors)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
        out.append((name, msg is None, len(fam.get("cases", [])), msg or ""))
    return out


def load_vectors(path):
    with open(path) as fh:
        return json.load(fh)


__all__ = ["parse_uri", "multi", "finite", "make_transport", "open_reader", "open_writer",
           "run_io", "certify_vectors", "find_vectors", "load_vectors", "UsageError",
           "MediumUnsupported", "SCHEMES", "FAMILIES", "STATS_KEYS"]
