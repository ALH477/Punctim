#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
"""Manual round trip on a real vanilla / Paper / Fabric server jar you supply (never in CI).

    python3 minecraft/tools/real_server_roundtrip.py --jar ~/server.jar [--java /path/to/java]

Writes eula.txt (you agree by running this), server.properties with enable-rcon + a random
password, installs the datapack into world/datapacks/dcf, boots the jar headless, and drives
the register over RCON via `punctim`'s own sidecar classes: build + loopback fixture, commit
the golden frame, strobe STROBE_IN with a redstone block, wait for tx_pending, verify w0..w5
and barrel 0's item count. Exit 0/1.
"""
import argparse
import os
import secrets
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "python", "MCP"))
import mclab_core as M  # noqa: E402
from dcf.minecraft.console import RconConsole  # noqa: E402
from dcf.minecraft.logtail import LogTail  # noqa: E402
from dcf.minecraft.rcon import RconClient  # noqa: E402
from dcf.minecraft.register import RegisterClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jar", required=True)
    ap.add_argument("--java", default="java")
    ap.add_argument("--dir", help="server directory (default: a temp dir)")
    ap.add_argument("--rcon-port", type=int, default=25575)
    ap.add_argument("--port", type=int, default=25565)
    ap.add_argument("--origin", nargs=3, type=int, default=[0, 64, 0])
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()
    d = a.dir or tempfile.mkdtemp(prefix="dcf-mc-")
    os.makedirs(d, exist_ok=True)
    pw = secrets.token_hex(12)
    with open(os.path.join(d, "eula.txt"), "w") as fh:
        fh.write("eula=true\n")
    with open(os.path.join(d, "server.properties"), "w") as fh:
        fh.write(f"server-ip=127.0.0.1\nserver-port={a.port}\nonline-mode=false\nenable-rcon=true\n"
                 f"rcon.port={a.rcon_port}\nrcon.password={pw}\nlevel-type=minecraft\\:flat\nlevel-name=world\n"
                 "spawn-protection=0\nview-distance=4\nmax-players=2\nenable-command-block=true\n")
    subprocess.check_call([sys.executable, os.path.join(ROOT, "minecraft", "datapack", "gen_datapack.py"),
                           "--out", os.path.join(d, "world", "datapacks", "dcf"), "--origin", *map(str, a.origin)])
    log = os.path.join(d, "logs", "latest.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    open(log, "a").close()
    tail = LogTail(log).start()
    proc = subprocess.Popen([a.java, "-Xmx2G", "-jar", os.path.abspath(a.jar), "nogui"], cwd=d,
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, text=True)
    rc = 1
    try:
        if tail.wait_for(r"Done \(", timeout=a.timeout) is None:
            print("FAIL: server did not start", file=sys.stderr)
            return 1
        reg = RegisterClient(RconConsole(RconClient("127.0.0.1", a.rcon_port, pw)), "dcf")
        c = reg.c
        golden, o = M.GOLDEN, tuple(a.origin)
        c.exec("function dcf:build")
        c.exec("function dcf:build_loopback")
        c.exec("function dcf:clear")
        reg.commit_rx(golden)                      # OUT barrels <- golden
        time.sleep(0.5)                            # comparators + wires settle
        s = M.lane_pos(o, 0, "strobe_in")
        c.exec(f"setblock {s[0] - 1} {s[1]} {s[2]} minecraft:redstone_block")   # strobe rising edge
        time.sleep(0.5)
        got = reg.poll_tx()
        c.exec(f"setblock {s[0] - 1} {s[1]} {s[2]} minecraft:air")
        if got is None:
            print("FAIL: the strobe did not latch (tx_pending stayed 0)", file=sys.stderr)
            return 1
        frame, valid = got
        if frame != golden or not valid:
            print(f"FAIL: latched {frame.hex()} valid={valid}, want {golden.hex()}", file=sys.stderr)
            return 1
        p0 = M.lane_pos(o, 0, "out")
        reply = c.exec(f"data get block {p0[0]} {p0[1]} {p0[2]} Items")
        print(f"PASS: golden frame round-tripped through the world's register over RCON; barrel 0: {reply[:80]}")
        rc = 0
    finally:
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            proc.wait(timeout=60)
        except Exception:
            proc.kill()
        tail.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
