# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF bridge — relay DeModFrames between transports to form arbitrary network shapes.

Put a Bridge on a node with several `dcf.transport.Transport`s (UDP, audio, SDR, file)
and frames flow between the media: an acoustic island bridged to an RF backbone bridged
to a UDP/Starlink uplink. Each transport's own bounded queue decouples the wildly
different rates (Mbps UDP vs ~300-baud acoustic), so the bridge just enqueues.

Routing:
  * **flood** (mesh default) — forward every new frame to all other transports; a rolling
    **dedup** set (keyed by the frame's src/dst/seq/crc) makes multi-hop loop-free, since
    the 17-byte quantum has no TTL field.
  * **egress** — reverse-path *learning* (a frame's src is reachable on the transport it
    arrived from) routes a unicast frame to the transport that reaches its dst; an unknown
    (off-grid) dst goes to the configured **uplink** transport — the Starlink/cellular
    backhaul. The mesh-wide orientation is computed by `meshlab_core.egress_routes`
    (see `dcf.modem`'s uplink demo); the bridge datapath is the per-node realization.
"""
import os
import sys
import threading
import time

for _mcp in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "MCP")):
    if os.path.isdir(_mcp):
        sys.path.insert(0, _mcp)
import wirelab_core as wire  # noqa: E402

if __package__ in (None, ""):     # allow `python3 python/dcf/bridge.py` as well as `-m dcf.bridge`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "dcf"

from .transport import frame_key, FRAME_LEN

BROADCAST = 0xFFFF
CTRL_TYPE = 3            # CTRL frames (mesh/audio control) ride the priority lane


class Bridge:
    def __init__(self, transports, route="flood", uplink=None, on_frame=None,
                 dedup_ttl=30.0, dedup_max=8192):
        self.transports = {t.name: t for t in transports}
        if uplink and uplink not in self.transports:
            raise ValueError(f"uplink {uplink!r} is not one of {list(self.transports)}")
        self.route = route
        self.uplink = uplink
        self.on_frame = on_frame
        self._ttl, self._max = dedup_ttl, dedup_max
        self._seen = {}                 # frame_key -> monotonic ts
        self._fwd = {}                  # dst src_id -> (transport_name, ts)  (learned)
        self._lock = threading.Lock()
        self.relayed = 0
        self.deduped = 0

    # lifecycle -----------------------------------------------------------------
    def start(self):
        for t in self.transports.values():
            t.start(lambda f, m: self._on_frame(m.get("transport"), f, m))
        return self

    def stop(self):
        for t in self.transports.values():
            t.stop()

    def inject(self, frame):
        """Originate a frame from this node (e.g. a local beacon/text)."""
        self._record(frame)
        self._forward(None, bytes(frame))

    # datapath ------------------------------------------------------------------
    def _on_frame(self, tname, frame, meta):
        if not self._record(frame, learn_from=tname):
            self.deduped += 1
            return
        if self.on_frame:
            self.on_frame(frame, meta)
        self._forward(tname, frame)

    def _record(self, frame, learn_from=None):
        """Register a frame; return False if it is a duplicate (loop). Learns the reverse
        path when `learn_from` is given."""
        key = frame_key(frame)
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            if key in self._seen:
                return False
            self._seen[key] = now
            if learn_from is not None:
                try:
                    self._fwd[wire.decode(bytes(frame))["src"]] = (learn_from, now)
                except Exception:
                    pass
            return True

    def _evict(self, now):
        if len(self._seen) > self._max:
            for k, ts in list(self._seen.items()):
                if now - ts > self._ttl:
                    del self._seen[k]
        # hard cap so a flood can't grow memory unbounded
        if len(self._seen) > self._max:
            for k in list(self._seen)[: len(self._seen) - self._max]:
                del self._seen[k]

    def _forward(self, incoming, frame):
        try:
            d = wire.decode(bytes(frame))
            dst, prio = d["dst"], (d["frame_type"] == CTRL_TYPE)
        except Exception:
            dst, prio = BROADCAST, False
        for name in self._targets(incoming, dst):
            self.transports[name].send(frame, priority=prio)
            self.relayed += 1

    def _targets(self, incoming, dst):
        others = [n for n in self.transports if n != incoming]
        if self.route == "flood" or dst == BROADCAST:
            return others
        # egress / unicast: a learned dst goes to its transport; an unknown dst egresses.
        with self._lock:
            known = self._fwd.get(dst)
        if known and known[0] != incoming and known[0] in self.transports:
            return [known[0]]
        if self.uplink and self.uplink != incoming:
            return [self.uplink]
        return others                   # fallback: flood

    @property
    def stats(self):
        return {"relayed": self.relayed, "deduped": self.deduped,
                "backlog": {n: t.backlog for n, t in self.transports.items()},
                "dropped": {n: t.dropped for n, t in self.transports.items()}}


# ── demo: a frame crosses media multi-hop — A(audio) → B(audio→sdr) → C(sdr) ───
def _demo():
    import tempfile
    from .transport import AudioTransport, SdrTransport
    d = tempfile.mkdtemp()
    link1, link2 = os.path.join(d, "link1"), os.path.join(d, "link2")   # the two hops
    arrived = []

    # Node A: originates a frame, transmits over its acoustic link (-> link1).
    A = Bridge([AudioTransport("A.audio", out_dir=link1, profile="handheld")]).start()
    # Node B: the bridge — recovers from the acoustic link, re-transmits over SDR (-> link2).
    B = Bridge([AudioTransport("B.audio", in_dir=link1, profile="handheld"),
                SdrTransport("B.sdr", out_dir=link2, mod="gfsk")]).start()
    # Node C: recovers from the SDR link; final delivery.
    C = Bridge([SdrTransport("C.sdr", in_dir=link2, mod="gfsk")],
               on_frame=lambda f, m: arrived.append(f)).start()

    fr = wire.encode(3, 1, 0x00A1, BROADCAST, b"SOS!", 0)
    print("DCF bridge — multi-hop across media:  A(acoustic) -> B(acoustic->SDR) -> C(SDR)")
    A.inject(fr)
    time.sleep(1.5)
    ok = bool(arrived) and arrived[0] == fr
    print(f"  frame {fr.hex()}")
    print(f"  arrived at C across 2 media + 2 hops: {'YES' if ok else 'no'}")
    print(f"  B (the bridge) stats: {B.stats}")
    for n in (A, B, C):
        n.stop()
    return 0 if ok else 1


# ── CLI: compose a node's shape from transports ───────────────────────────────
def _make_transport(spec):
    """Build a transport from a medium URI — delegates to the single-sourced DCF-Medium
    grammar in ``dcf.medium`` (Documentation/DCF_MEDIUM_SPEC.md). Historical strings
    (``udp:bind=,peer=``, ``audio:``, ``sdr:``, ``janus:``, ``hydra:``, ``file:in=,out=``)
    keep working; a bridge ``file:`` port tails its spool and appends, as before."""
    from .medium import make_transport, UsageError, MediumUnsupported
    try:
        return make_transport(spec, direction=None)
    except (UsageError, MediumUnsupported) as e:
        raise SystemExit(f"dcf-bridge: {e}")


_TRANSPORT_HELP = (
    "medium URI SCHEME:k=v,... (repeatable): "
    "udp:dialect=proto|bare,bind=HOST:PORT,peer=HOST:PORT|... | "
    "file:path=F.dcf (or in=,out=) | stdio: | hex:[path=F] | loop:id=N | "
    "l2eth:if=IF|impl=loop | hydra:in=,out=,profile=default|aux,fec=none|rep3|conv | "
    "afsk:in=,out=,profile=,fec=0|1 (audio: alias) | sdr:in=,out=,mod= | "
    "janus:in=,out=,pset= — see Documentation/DCF_MEDIUM_SPEC.md")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="dcf-bridge", description="Bridge DeModFrames across transports.",
        epilog="example:\n"
               "  dcf-bridge -t 'udp:bind=0.0.0.0:9100,peer=base:9100' \\\n"
               "             -t 'audio:in=/tmp/rx,out=/tmp/tx,profile=handheld' \\\n"
               "             --route egress --uplink udp --text SOS --seconds 3\n"
               "  dcf-bridge --demo        # multi-hop across media, no hardware",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--transport", action="append", default=[], metavar="TYPE:k=v,...",
                    help=_TRANSPORT_HELP)
    ap.add_argument("--route", choices=["flood", "egress"], default="flood")
    ap.add_argument("--uplink", default=None, help="transport name toward backhaul (egress)")
    ap.add_argument("--inject", metavar="HEX", help="originate a 17-byte frame (hex)")
    ap.add_argument("--text", help="originate a frame carrying up to 4 bytes of text")
    ap.add_argument("--seconds", type=float, default=None, help="run N seconds then exit")
    ap.add_argument("--demo", action="store_true", help="run the no-hardware multi-hop demo")
    a = ap.parse_args(argv)
    if a.demo:
        return _demo()
    if not a.transport:
        ap.error("need at least one --transport (or --demo)")
    br = Bridge([_make_transport(s) for s in a.transport], route=a.route, uplink=a.uplink,
                on_frame=lambda f, m: print(f"  recv {f.hex()} via {m.get('transport')}"))
    br.start()
    try:
        if a.inject or a.text:
            fr = (bytes.fromhex(a.inject) if a.inject
                  else wire.encode(3, 1, 0x00A1, BROADCAST, a.text.encode()[:4].ljust(4, b"\x00"), 0))
            br.inject(fr)
            print(f"injected {fr.hex()}")
        time.sleep(a.seconds) if a.seconds is not None else _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        br.stop()
        print(f"bridge stats: {br.stats}")
    return 0


def _wait_forever():
    while True:
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
