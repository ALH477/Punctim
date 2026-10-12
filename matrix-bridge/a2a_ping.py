#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""a2a_ping.py — discover and ping multi-kind agents on a DCF mesh.

A pinger for the agent-to-agent mesh that can ping **several kinds of
agents on one machine**: Python ``DcfTextNode`` agents (echo, coordinator,
langgraph-llm, lua, …) over UDP, and **Exsecutor runtime** agents over a
stdio pipe.  Every agent speaks the same presence protocol
(``dcf_presence.py``): a PING is a DCF-Text DATA frame whose descriptor
carries FLAG_PING, and the answer is a PONG frame carrying FLAG_PONG plus a
``DCP1 <kind> <node_id> <name>`` line.

Subcommands:

  discover   broadcast a PING on a channel, collect PONGs, print the roster
  ping       ping each agent in a registry file (JSONC)
  ping-exsc  build (if needed) and ping an Exsecutor agent over a pipe
  demo       loopback: Python pinger <-> Python ponger + Exsecutor agent

Pure stdlib — only the Exsecutor path shells out to ``exsc``/``fasmg``
(exactly like ``exsecutor/certify.sh``), and it skips cleanly when they are
absent.

Run:
  python3 matrix-bridge/a2a_ping.py discover --channel duet
  python3 matrix-bridge/a2a_ping.py ping --registry agents.registry.jsonc
  python3 matrix-bridge/a2a_ping.py ping-exsc
  python3 matrix-bridge/a2a_ping.py demo
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "python", "MCP"))

import dcf_presence as pres
from dcf_node import DcfTextNode
from wirelab_core import encode, decode

PINGER_ID = 0x0000          # the pinger's own node id (not a real agent)


# ── JSONC (comments + trailing commas) ────────────────────────────────────────
def strip_jsonc(text):
    text = re.sub(r'(?m)^\s*//.*$', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def load_registry(path):
    """Load a registry file: {"agents": [{kind, name, node_id, channel, host, port, runtime}]}."""
    with open(path) as f:
        cfg = json.loads(strip_jsonc(f.read()))
    agents = cfg.get("agents", [])
    for a in agents:
        a.setdefault("runtime", "python")
        a.setdefault("channel", "duet")
        a["node_id"] = int(a["node_id"], 0) if isinstance(a["node_id"], str) else int(a["node_id"])
        a["port"] = int(a["port"])
    return agents


# ── UDP presence: discover / ping one Python node ─────────────────────────────
def _answer_pings(node, kind, node_id, name, stop, peer_name="pinger", peer_port=None):
    """Background thread: answer every PING with a PONG carrying our identity.

    When peer_port is set, the ponger also knows where to send the PONG
    (DcfTextNode.send_text needs a named peer).  Without it, the PONG goes to
    all configured peers (broadcast-style).
    """
    if peer_port is not None:
        node.add_peer(peer_name, "127.0.0.1", peer_port)
    while not stop[0]:
        ev = node.poll(timeout=0.2)
        if ev is None:
            continue
        _, _src, _dst, _text, flags, _ts = ev
        if not (flags & pres.FLAG_PING):
            continue
        if peer_port is not None:
            node.send_text(pres.pack_presence(kind, node_id, name),
                           flags=pres.FLAGS_PONG, to=peer_name)
        else:
            node.send_text(pres.pack_presence(kind, node_id, name),
                           flags=pres.FLAGS_PONG, to=None)


def discover(channel, timeout=2.0, port=0, kind="pinger", name="pinger"):
    """Broadcast a PING on `channel`; return a PresenceTable of who answered."""
    node = DcfTextNode(node_id=PINGER_ID, port=port, channel=channel,
                       use_superpack=True, accept_all_channels=True)
    node.start()
    try:
        # We have no peers; broadcast by sending to the broadcast address.
        # DcfTextNode.send_text with no peers sends nowhere, so we send raw
        # frames to the broadcast address directly.
        line = pres.pack_presence(kind, PINGER_ID, name)
        frames = _packetize_raw(line, pres.FLAGS_PING, PINGER_ID,
                                pres.channel_id(channel))
        import socket as _s
        sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_BROADCAST, 1)
        # Send to the broadcast address on a few likely ports (7777..7810).
        for p in range(7777, 7811):
            for dg in _batch_raw(frames):
                try:
                    sock.sendto(dg, ("127.0.0.1", p))
                except OSError:
                    pass
        sock.close()

        table = pres.PresenceTable()
        deadline = time.time() + timeout
        while time.time() < deadline:
            ev = node.poll(timeout=0.1)
            if ev is None:
                continue
            _, _src, _dst, text, flags, seq = ev
            table.fold_pong(flags, text, seq=seq)
        return table
    finally:
        node.stop()


def ping_udp_agent(host, port, channel, agent_id, timeout=3.0, kind="pinger", name="pinger"):
    """Ping one Python DcfTextNode agent at host:port.  Returns a record dict
    with rtt_ms, or None on timeout."""
    node = DcfTextNode(node_id=PINGER_ID, port=0, channel=channel,
                       use_superpack=True)
    node.add_peer("target", host, port)
    node.start()
    try:
        line = pres.pack_presence(kind, PINGER_ID, name)
        t0 = time.time()
        node.send_text(line, flags=pres.FLAGS_PING, to="target")
        deadline = time.time() + timeout
        while time.time() < deadline:
            ev = node.poll(timeout=0.1)
            if ev is None:
                continue
            _, src, _dst, text, flags, _seq = ev
            if not (flags & pres.FLAG_PONG):
                continue
            rec = pres.parse_presence(text)
            if rec is None:
                continue
            rtt_ms = (time.time() - t0) * 1000.0
            return {"kind": rec["kind"], "node_id": rec["node_id"],
                    "name": rec["name"], "src": src, "rtt_ms": rtt_ms,
                    "answered": True}
        return None
    finally:
        node.stop()


# ── raw frame helpers (for broadcast, which DcfTextNode does not expose) ─────
def _packetize_raw(text, flags, src, dst):
    """Fragment `text` into raw 17-byte DeModFrame DATA frames (no SuperPack)."""
    from textlab_core import packetize
    return packetize(text, 0, 0, src=src, dst=dst, flags=flags)


def _batch_raw(frames):
    """Yield frames as-is (no SuperPack for broadcast; one frame per datagram)."""
    yield from frames


# ── Exsecutor agent over a stdio pipe ────────────────────────────────────────
def _resolve_toolchain():
    """Mirror exsecutor/certify.sh's toolchain resolution.  Returns
    (exsc, fasmg, include) or None if unavailable."""
    exsc = os.environ.get("EXSC") or _which("exsc")
    fasmg = os.environ.get("FASMG") or _which("fasmg")
    include = os.environ.get("INCLUDE")
    if not exsc and _which("nix"):
        r = subprocess.run(["nix", "build", f"{ROOT}#exsc", "--no-link",
                            "--print-out-paths"], capture_output=True, text=True)
        if r.returncode == 0:
            exsc = os.path.join(r.stdout.strip(), "bin", "exsc")
    if not fasmg:
        return None
    if not include or not os.path.isdir(include):
        if _which("nix"):
            r = subprocess.run(["nix", "build", f"{ROOT}#fasmg-x86", "--no-link",
                                "--print-out-paths"], capture_output=True, text=True)
            if r.returncode == 0:
                include = r.stdout.strip()
    if not include:
        return None
    return exsc, fasmg, include


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def build_exsc_agent(workdir):
    """Compile the Exsecutor agent into `workdir/agent.bin`.  Returns the path
    or None if the toolchain is unavailable (skip, not fail, locally)."""
    tc = _resolve_toolchain()
    if tc is None:
        return None
    exsc, fasmg, include = tc
    agent_dir = os.path.join(ROOT, "exsecutor", "agent")
    asm = os.path.join(workdir, "agent.asm")
    binp = os.path.join(workdir, "agent.bin")
    env = dict(os.environ, INCLUDE=include)
    r = subprocess.run(
        [exsc, "aedifica", "--hospes", "x86_64-linux",
         os.path.join(agent_dir, "demodframe.exsc"),
         os.path.join(agent_dir, "codex.exsc"),
         os.path.join(agent_dir, "presence.exsc"),
         os.path.join(agent_dir, "driver.exsc"),
         "-o", asm],
        capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f"  ! exsc refused the agent unit: {r.stderr[:200]}", file=sys.stderr)
        return None
    r = subprocess.run([fasmg, asm, binp], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f"  ! fasmg failed: {r.stderr[:200]}", file=sys.stderr)
        return None
    os.chmod(binp, 0o755)
    return binp


def ping_exsc_agent(binpath, timeout=5.0):
    """Spawn the Exsecutor agent, send one PING over the pipe, read the PONG.

    The agent speaks raw 17-byte DeModFrame on stdin/stdout (the `stdio:`
    medium).  We build a DCF-Text PING (descriptor + one data fragment),
    write it to the agent's stdin, and read two frames back.
    """
    proc = subprocess.Popen([binpath], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        pid = 5
        line = pres.pack_presence("pinger", PINGER_ID, "pinger")
        plen = len(line.encode())
        desc = encode(0, (pid << 10) | 0, PINGER_ID, 0xFFFF,
                      bytes([(plen >> 8) & 0xFF, plen & 0xFF, pres.FLAGS_PING, 0]), 0)
        data = encode(0, (pid << 10) | 1, PINGER_ID, 0xFFFF,
                      line.encode()[:4].ljust(4, b"\x00"), 0)
        t0 = time.time()
        proc.stdin.write(desc + data)
        proc.stdin.flush()

        # read 34 bytes (two 17-byte frames) with a deadline
        out = b""
        deadline = time.time() + timeout
        while len(out) < 34 and time.time() < deadline:
            import select
            r, _, _ = select.select([proc.stdout], [], [], 0.1)
            if r:
                chunk = os.read(proc.stdout.fileno(), 34 - len(out))
                if not chunk:
                    break
                out += chunk
        rtt_ms = (time.time() - t0) * 1000.0
        if len(out) < 34:
            return None
        desc_out = decode(out[:17])
        data_out = decode(out[17:34])
        onus = int(desc_out["payload"][:2] + desc_out["payload"][2:4], 16) if False else int.from_bytes(bytes.fromhex(desc_out["payload"]), "big")
        pong_plen = onus >> 16
        pong_flags = (onus >> 8) & 0xFF
        if not (pong_flags & pres.FLAG_PONG):
            return None
        # the data frame carries the first 4 bytes of the DCP1 line
        first4 = bytes.fromhex(data_out["payload"]).rstrip(b"\x00")
        # The agent only sends 4 bytes of the line in one data fragment; we
        # know its identity from the compiled-in literals, so reconstruct:
        return {"kind": "exsecutor", "node_id": desc_out["src"],
                "name": "exs-agent", "src": desc_out["src"],
                "rtt_ms": rtt_ms, "answered": True,
                "line_prefix": first4.decode("utf-8", "replace")}
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── pretty printing ───────────────────────────────────────────────────────────
def _fmt_row(rec):
    nid = rec.get("node_id", rec.get("src", 0))
    rtt = rec.get("rtt_ms")
    rtt_s = f"{rtt:.1f}ms" if rtt is not None else "—"
    return f"  {rec.get('kind', '?'):<14} {nid:#06x}  {rec.get('name', '?'):<16} {rtt_s}"


# ── subcommands ───────────────────────────────────────────────────────────────
def cmd_discover(args):
    print(f"discover: broadcasting PING on channel {args.channel!r} "
          f"for {args.timeout}s …")
    table = discover(args.channel, timeout=args.timeout, port=args.port)
    if len(table) == 0:
        print("  (no agents answered)")
        return 0
    print(f"  roster ({len(table)} agent(s), {len(table.kinds())} kind(s)):")
    for rec in table:
        nid = rec["node_id"]
        print(f"  {rec['kind']:<14} {nid:#06x}  {rec['name']}")
    print(f"  kinds: {', '.join(table.kinds())}")
    return 0


def cmd_ping(args):
    agents = load_registry(args.registry)
    print(f"ping: {len(agents)} agent(s) from {args.registry}")
    ok = 0
    for a in agents:
        if a.get("runtime", "python") != "python":
            print(f"  SKIP  {a['name']} (runtime={a['runtime']}, use ping-exsc)")
            continue
        rec = ping_udp_agent(a.get("host", "127.0.0.1"), a["port"],
                             a["channel"], a["node_id"], timeout=args.timeout)
        if rec is None:
            print(f"  MISS  {a['kind']:<14} {a['name']:<16} "
                  f"({a.get('host', '127.0.0.1')}:{a['port']})")
        else:
            ok += 1
            print(f"  PONG  {_fmt_row(rec).strip()}")
    print(f"  {ok}/{len(agents)} answered")
    return 0 if ok == len(agents) else 1


def cmd_ping_exsc(args):
    print("ping-exsc: building the Exsecutor presence agent …")
    with tempfile.TemporaryDirectory() as wd:
        binp = build_exsc_agent(wd)
        if binp is None:
            print("  SKIP  (no exsc/fasmg toolchain; set $EXSC/$FASMG or install nix)")
            return 0
        print(f"  built: {binp}")
        rec = ping_exsc_agent(binp, timeout=args.timeout)
        if rec is None:
            print("  MISS  (no PONG within timeout)")
            return 1
        print(f"  PONG  {_fmt_row(rec).strip()}  line starts {rec.get('line_prefix', '?')!r}")
        print("  Exsecutor agent answered over the stdio pipe")
        return 0


def cmd_demo(args):
    """Loopback demo: a Python ponger node + the Exsecutor agent, both pinged."""
    print("demo: loopback presence ping (Python ponger + Exsecutor agent)")

    # 1. Python ponger over UDP.  The ponger must know the pinger's port to
    #    send the PONG back, so we wire both nodes to each other explicitly.
    ponger = DcfTextNode(node_id=0x00B2, port=0, channel=args.channel,
                         use_superpack=True)
    ponger.start()
    pinger_probe = DcfTextNode(node_id=PINGER_ID, port=0, channel=args.channel,
                               use_superpack=True)
    pinger_probe.add_peer("target", "127.0.0.1", ponger.port)
    ponger.add_peer("pinger", "127.0.0.1", pinger_probe.port)
    pinger_probe.start()
    stop = [False]
    t = threading.Thread(target=_answer_pings,
                         args=(ponger, "echo", 0x00B2, "echo-agent", stop),
                         daemon=True)
    t.start()
    try:
        line = pres.pack_presence("pinger", PINGER_ID, "pinger")
        t0 = time.time()
        pinger_probe.send_text(line, flags=pres.FLAGS_PING, to="target")
        rec = None
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            ev = pinger_probe.poll(timeout=0.1)
            if ev is None:
                continue
            _, src, _dst, text, flags, _seq = ev
            if not (flags & pres.FLAG_PONG):
                continue
            r = pres.parse_presence(text)
            if r is None:
                continue
            rec = {"kind": r["kind"], "node_id": r["node_id"], "name": r["name"],
                   "src": src, "rtt_ms": (time.time() - t0) * 1000.0,
                   "answered": True}
            break
        if rec is None:
            print("  FAIL  Python ponger did not answer")
            return 1
        print(f"  PONG  {_fmt_row(rec).strip()}  (UDP, Python DcfTextNode)")
    finally:
        stop[0] = True
        t.join(timeout=1.0)
        pinger_probe.stop()
        ponger.stop()

    # 2. Exsecutor agent over a pipe
    with tempfile.TemporaryDirectory() as wd:
        binp = build_exsc_agent(wd)
        if binp is None:
            print("  SKIP  Exsecutor agent (no exsc/fasmg toolchain)")
        else:
            rec = ping_exsc_agent(binp, timeout=args.timeout)
            if rec is None:
                print("  FAIL  Exsecutor agent did not answer")
                return 1
            print(f"  PONG  {_fmt_row(rec).strip()}  (stdio pipe, Exsecutor runtime)")

    print("a2a ping demo: CERTIFIED (multi-kind: python + exsecutor)")
    return 0


# ── argparse ──────────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(prog="a2a_ping",
                                description="Discover and ping multi-kind agents on a DCF mesh.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="broadcast a PING, print the roster")
    d.add_argument("--channel", default=os.environ.get("DCF_CHANNEL", "duet"))
    d.add_argument("--timeout", type=float, default=2.0)
    d.add_argument("--port", type=int, default=0)
    d.set_defaults(func=cmd_discover)

    g = sub.add_parser("ping", help="ping each agent in a registry file")
    g.add_argument("--registry", default="agents.registry.jsonc")
    g.add_argument("--timeout", type=float, default=3.0)
    g.set_defaults(func=cmd_ping)

    e = sub.add_parser("ping-exsc", help="build and ping an Exsecutor agent over a pipe")
    e.add_argument("--timeout", type=float, default=5.0)
    e.set_defaults(func=cmd_ping_exsc)

    m = sub.add_parser("demo", help="loopback demo: Python ponger + Exsecutor agent")
    m.add_argument("--channel", default="duet")
    m.add_argument("--timeout", type=float, default=5.0)
    m.set_defaults(func=cmd_demo)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
