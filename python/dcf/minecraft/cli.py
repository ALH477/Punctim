# SPDX-License-Identifier: LGPL-3.0-only
"""`punctim mc` — bridge a Minecraft world's DeModFrame register to Punctim UDP peers.

    punctim mc --rcon 127.0.0.1:25575 --password-file ~/.rcon --peer 127.0.0.1:7777/proto
    punctim mc --fifo /run/minecraft-server.stdin \\
               --log /var/lib/minecraft/logs/latest.log --peer 127.0.0.1:7801/bare
    punctim mc --log ~/.local/share/PrismLauncher/instances/1.21.11/minecraft/logs/latest.log \\
               --egress chat --peer 127.0.0.1:7777/proto          # a single-player world, egress only
    punctim mc --bedrock-ws 127.0.0.1:19134 --peer 127.0.0.1:7777/proto   # vanilla Bedrock /connect

Every frame the world latches (STROBE_IN edge, /function dcf:tx, tx_from_words) goes to every
peer; every frame a peer sends is committed into the OUT lanes (needs a console).  Peers
carry a dialect: `/proto` (ProtoMessage MSG_FRAME=12: dcf_node.py, Go, Rust, C) or `/bare`
(17-B / 32-B SuperPack: Hermes mesh_mcp.py, JS, web).  Exit codes as `punctim io`.
"""
import argparse
import json
import signal
import sys
import threading
import time

from .. import transport as T
from ..bridge import Bridge
from . import factory

EXIT_OK, EXIT_IO, EXIT_USAGE, EXIT_UNSUPPORTED = 0, 1, 2, 3


def _peer(spec):
    hp, _, dialect = spec.partition("/")
    dialect = dialect or "bare"
    if dialect not in ("proto", "bare"):
        raise argparse.ArgumentTypeError("peer dialect must be proto or bare")
    host, _, port = hp.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("peer wants host:port[/proto|/bare]")
    return host, int(port), dialect


def _parser():
    ap = argparse.ArgumentParser(prog="punctim mc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("world (one console, or --log alone for chat egress)")
    src.add_argument("--rcon", metavar="HOST:PORT")
    src.add_argument("--password-file", metavar="FILE", help="RCON password (or $MC_RCON_PASSWORD)")
    src.add_argument("--fifo", metavar="PATH", help="server console FIFO / stdin pipe (needs --log)")
    src.add_argument("--bot", metavar="ARGV", help="JSON-lines console helper, '|'-separated argv")
    src.add_argument("--log", metavar="PATH", help="server or client latest.log")
    src.add_argument("--egress", choices=("console", "chat"))
    src.add_argument("--namespace", default="dcf")
    src.add_argument("--poll-hz", type=float, default=4.0)
    net = ap.add_argument_group("peers")
    net.add_argument("--peer", type=_peer, action="append", default=[], metavar="HOST:PORT[/proto|/bare]")
    net.add_argument("--bind", default="0.0.0.0:0", metavar="HOST:PORT",
                     help="UDP bind of the proto socket (where proto peers send frames to this world)")
    net.add_argument("--bind-bare", default="0.0.0.0:0", metavar="HOST:PORT",
                     help="UDP bind of the bare socket (default ephemeral; bare peers reply to it)")
    net.add_argument("--bedrock-ws", metavar="HOST:PORT", help="listen for a vanilla Bedrock /connect")
    net.add_argument("--bedrock-events", action="store_true", help="also subscribe BlockPlaced/BlockBroken")
    ap.add_argument("--seconds", type=float, help="stop after S seconds")
    ap.add_argument("--stats", action="store_true", help="one JSON stats line on stderr at exit")
    ap.add_argument("--verbose", "-v", action="store_true", help="log every relayed frame")
    return ap


def main(argv=None):
    a = _parser().parse_args(argv)
    if not (a.rcon or a.fifo or a.bot or a.log or a.bedrock_ws):
        print("punctim mc: give a world (--rcon/--fifo/--bot/--log) or --bedrock-ws", file=sys.stderr)
        return EXIT_USAGE
    transports = []
    try:
        if a.rcon or a.fifo or a.bot or a.log:
            transports.append(factory.build("mc", rcon=a.rcon, pass_file=a.password_file, fifo=a.fifo,
                                            log=a.log, bot=a.bot, egress=a.egress, ns=a.namespace,
                                            poll_hz=a.poll_hz))
        # One socket per dialect, each with its own bind: two sockets on one port would
        # split inbound datagrams between them (SO_REUSEPORT) and a proto datagram landing
        # on the bare socket is silently dropped.
        for dialect, bind in (("proto", a.bind), ("bare", a.bind_bare)):
            peers = [(h, p) for h, p, d in a.peer if d == dialect]
            if peers:
                host, _, port = bind.rpartition(":")
                transports.append(T.UdpTransport(f"udp-{dialect}", bind=(host or "0.0.0.0", int(port or 0)),
                                                 peers=peers, dialect=dialect))
        if a.bedrock_ws:
            from .bedrock_ws import BedrockWsTransport
            h, _, p = a.bedrock_ws.rpartition(":")
            transports.append(BedrockWsTransport("bedrock", (h or "0.0.0.0", int(p)),
                                                 namespace=a.namespace, events=a.bedrock_events))
    except T.MediumUnsupported as e:
        print(f"punctim mc: medium unsupported: {e}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except OSError as e:
        print(f"punctim mc: I/O error: {e}", file=sys.stderr)
        return EXIT_IO
    if len(transports) < 2:
        print("punctim mc: nothing to bridge — add --peer or --bedrock-ws", file=sys.stderr)
        return EXIT_USAGE

    def on_frame(frame, meta):
        if a.verbose:
            print(f"frame {frame.hex()} from {meta.get('transport')}", file=sys.stderr)

    bridge = Bridge(transports, route="flood", on_frame=on_frame)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    bridge.start()
    t0 = time.monotonic()
    try:
        while not stop.is_set():
            if a.seconds is not None and time.monotonic() - t0 >= a.seconds:
                break
            stop.wait(0.2)
    finally:
        bridge.stop()
    if a.stats:
        st = dict(bridge.stats() if callable(bridge.stats) else bridge.stats)
        st["transports"] = {t.name: {"sent": t.sent, "recv": t.recv} for t in transports}
        print(json.dumps(st, separators=(",", ":")), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
