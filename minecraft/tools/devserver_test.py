#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
"""Real Paper 26.2 + Geyser + Floodgate (Oligarchy's dev runner) with the DCF plugin + datapack,
as your own user in a scratch directory — no system service, no root.

    python3 minecraft/tools/devserver_test.py [--oligarchy ~/Documents/oligarchy2/Oligarchy]
        [--plugin minecraft/build/dcf-minecraft-paper.jar] [--join <prism-instance>] [--keep]

  1. installs the plugin jar (a regular file: the runner only symlinks its own two), a
     config.yml in datapack mode whose peer is this harness, and the generated datapack
     into devworld/datapacks/dcf;
  2. spawns `nix run <oligarchy>#minecraft-server-dev -- --accept-eula --dir DIR` with a stdin
     PIPE (the console) and follows DIR/logs/latest.log;
  3. once the server is up: `function dcf:selftest` over the console; the datapack latches
     the golden frame; the plugin (datapack mode) polls tx_pending, ACKs and sends it here;
  4. ingress: this harness sends the golden frame to the plugin's UDP port; the plugin hands
     it to dcf:rx_commit; the console reads back w0..w5 and the barrel item counts;
  5. optional (--join): prints the `prismlauncher --launch <inst> --server 127.0.0.1:25565`
     command for you to run yourself; this script never starts your client.
Requires nix and network on first start (Paperclip downloads Mojang's jar). EULA: you agree
by running this (https://aka.ms/MinecraftEULA).
"""
import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "python", "MCP"))
import mclab_core as M  # noqa: E402
import mediumlab_core as ML  # noqa: E402
from dcf.minecraft.console import FifoConsole  # noqa: E402
from dcf.minecraft.logtail import LogTail  # noqa: E402
from dcf.minecraft.register import RegisterClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oligarchy", default=os.path.expanduser("~/Documents/oligarchy2/Oligarchy"))
    ap.add_argument("--plugin", default=os.path.join(ROOT, "minecraft", "build", "dcf-minecraft-paper.jar"))
    ap.add_argument("--dir", default=os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                                                  "oligarchy-minecraft-dev"))
    ap.add_argument("--origin", nargs=3, type=int, default=[0, 64, 0])
    ap.add_argument("--plugin-port", type=int, default=7810)
    ap.add_argument("--join", metavar="INSTANCE", help="print the prismlauncher command to join with this instance")
    ap.add_argument("--keep", action="store_true", help="leave the server running until Ctrl-C")
    ap.add_argument("--timeout", type=float, default=600.0)
    a = ap.parse_args()
    if not os.path.isfile(a.plugin):
        sys.exit(f"no plugin jar at {a.plugin}: run minecraft/build.sh first")

    d = a.dir
    os.makedirs(os.path.join(d, "plugins", "DcfMinecraft"), exist_ok=True)
    shutil.copy(a.plugin, os.path.join(d, "plugins", "dcf-minecraft-paper.jar"))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.5)
    with open(os.path.join(d, "plugins", "DcfMinecraft", "config.yml"), "w") as fh:
        fh.write(f'node-id: "0x00B1"\nbind: "127.0.0.1:{a.plugin_port}"\npeers:\n  - "127.0.0.1:{udp.getsockname()[1]}/proto"\n'
                 f"flush-ms: 20\nworld: devworld\norigin: [{a.origin[0]}, {a.origin[1]}, {a.origin[2]}]\n"
                 "mode: datapack\nnamespace: dcf\npulse-ticks: 4\nwatch:\n  enabled: false\n  min: [0, 0, 0]\n  max: [0, 0, 0]\n")
    dp = os.path.join(d, "devworld", "datapacks", "dcf")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "minecraft", "datapack", "gen_datapack.py"),
                        "--out", dp, "--origin", *map(str, a.origin)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    log = os.path.join(d, "logs", "latest.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    open(log, "a").close()
    tail = LogTail(log).start()

    cmd = ["nix", "run", f"{a.oligarchy}#minecraft-server-dev", "--", "--accept-eula", "--dir", d]
    print("starting:", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=open(os.path.join(d, "console.out"), "ab"),
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    console = FifoConsole(proc.stdin, tail)
    reg = RegisterClient(console, "dcf")
    try:
        print("waiting for Paper to finish starting (first start downloads Mojang's jar) ...")
        seen = []
        tail.subscribe(lambda line, m: seen.append(m) if "DCF node 0x" in m else None)
        if tail.wait_for(r"Done \(", timeout=a.timeout) is None:
            sys.exit("FAIL: the server did not come up")
        if not seen:
            print("warning: the plugin did not announce 'DCF node' before Done (is the jar loaded?)")
        else:
            print("plugin:", seen[0])
        if a.join:
            # Never start the user's client from here: print the command for their own terminal.
            print(f"server is up. To watch from your client run, in another terminal:\n"
                  f"    prismlauncher --launch {a.join} --server 127.0.0.1:25565")

        golden = M.GOLDEN
        words = M.frame_to_words(golden)
        # egress: the datapack's selftest latches the golden frame; the plugin sends it here
        console.exec("function dcf:selftest", reply=r"function 'dcf:selftest'", timeout=10)
        got = None
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                dg, _ = udp.recvfrom(2048)
            except socket.timeout:
                continue
            fr = ML.proto_frame_decode(dg)
            if fr == golden:
                got = fr
                break
        if got is None:
            # diagnostics before giving up: what did the world actually do?
            o = tuple(a.origin)
            p0, i0 = M.lane_pos(o, 0, "out"), M.lane_pos(o, 0, "in")
            diag = {"ack": reg.get("ack", M.OBJ_CTL), "tx_pending": reg.get("tx_pending", M.OBJ_CTL),
                    "tx_seq": reg.get("tx_seq", M.OBJ_CTL), "rx_seq": reg.get("rx_seq", M.OBJ_CTL),
                    "words": reg.read_words(),
                    "barrel0": console.exec(f"data get block {p0[0]} {p0[1]} {p0[2]}", reply=r"block data|No block", timeout=5)[:160],
                    "in0": console.exec(f"execute if block {i0[0]} {i0[1]} {i0[2]} minecraft:redstone_wire", reply=r"Test|not", timeout=5)[:80]}
            sys.exit(f"FAIL egress: the plugin never sent the golden frame over UDP; world state: {diag}")
        print(f"PASS egress: golden frame {golden.hex()} arrived over UDP (proto) from the plugin")
        # The plugin sets ack=1 and tx_pending=0; the datapack's tick consumes `ack` (pulses
        # ACK_OUT, then zeroes it), so the durable evidence is tx_pending having been cleared.
        deadline = time.time() + 5
        while time.time() < deadline and reg.get("tx_pending", M.OBJ_CTL) != 0:
            time.sleep(0.25)
        if reg.get("tx_pending", M.OBJ_CTL) != 0:
            sys.exit("FAIL: the plugin did not consume the latched frame (tx_pending still 1)")

        # ingress: golden -> plugin -> dcf:rx_commit -> words + barrels
        console.exec("function dcf:clear_words", reply=r"function 'dcf:clear_words'", timeout=10)
        udp.sendto(ML.proto_frame_encode(golden, 1), ("127.0.0.1", a.plugin_port))
        deadline = time.time() + 20
        while time.time() < deadline and reg.read_words() != words:
            time.sleep(0.5)
        if reg.read_words() != words:
            sys.exit("FAIL ingress: w0..w5 never became the golden words")
        p0 = M.lane_pos(tuple(a.origin), 0, "out")
        reply = console.exec(f"data get block {p0[0]} {p0[1]} {p0[2]} Items[0].count", reply=r"has the following block data", timeout=10)
        m = re.search(r"data: (\d+)", reply)
        if not m or int(m.group(1)) != min(64, M.frame_to_items(golden)[0]):
            sys.exit(f"FAIL ingress: barrel 0 count reply {reply!r}")
        print("PASS ingress: the plugin handed the golden frame to dcf:rx_commit; scoreboard + barrel 0 verified")
        print("dev server round trip: PASS")
        if a.keep:
            print("server still running (Ctrl-C to stop) ...")
            try:
                while proc.poll() is None:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            proc.wait(timeout=120)
        except Exception:
            proc.kill()
        tail.stop()


if __name__ == "__main__":
    main()
