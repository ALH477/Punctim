#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
"""Client-in-the-loop test with the user's own Prism Launcher instance.

    python3 minecraft/tools/prism_test.py --instance 1.21.11            # offline player, no account
    python3 minecraft/tools/prism_test.py --instance poo --mod minecraft/fabric/build/libs/dcf-minecraft-fabric-1.0.0.jar

What it does:
  1. copies a template world (default: the instance's "New World (1)") to saves/<world>
     and refuses unless level.dat says cheats are on (allowCommands);
  2. generates the conforming datapack with --autotest into saves/<world>/datapacks/dcf
     (and, with --mod, installs the Fabric jar + a config pointing at this harness);
  3. waits for YOU to open that world from your own launcher (`--launch` makes this script
     run prismlauncher itself; off by default so no other process touches your client);
  4. tails the instance's logs/latest.log: the datapack's dcf:selftest runs on world load
     (build + loopback fixture + golden frame through the barrels/comparators/wires),
     announces `DCF TX w0..w5` and `DCF SELFTEST PASS <hex>`; the harness decodes the
     chat egress and judges PASS/FAIL against the golden frame;
  5. with --mod, also sends the golden frame to the mod over UDP and waits for its
     `DCF RX w0..w5` echo (ingress), then `DCF TX` again after the mod's own latch.
Pure-vanilla single-player is egress-only (no console). Nothing here starts or stops your game.
"""
import argparse
import gzip
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "python", "MCP"))
import mclab_core as M  # noqa: E402
import mediumlab_core as ML  # noqa: E402
from dcf.minecraft.logtail import LogTail  # noqa: E402

PRISM = os.path.expanduser("~/.local/share/PrismLauncher/instances")


def instance_dir(inst):
    for sub in ("minecraft", ".minecraft"):
        d = os.path.join(PRISM, inst, sub)
        if os.path.isdir(d):
            return d
    sys.exit(f"no Prism instance {inst!r} under {PRISM}")


# ── a tiny NBT reader: enough to find Data.allowCommands in level.dat ──────────
def _read_nbt(buf):
    pos = [0]

    def u8():
        v = buf[pos[0]]
        pos[0] += 1
        return v

    def name():
        n = struct.unpack_from(">H", buf, pos[0])[0]
        pos[0] += 2
        s = buf[pos[0]:pos[0] + n].decode("utf-8", "replace")
        pos[0] += n
        return s

    def payload(t):
        if t == 1:
            return u8()
        if t == 2:
            v = struct.unpack_from(">h", buf, pos[0])[0]; pos[0] += 2; return v
        if t == 3:
            v = struct.unpack_from(">i", buf, pos[0])[0]; pos[0] += 4; return v
        if t == 4:
            v = struct.unpack_from(">q", buf, pos[0])[0]; pos[0] += 8; return v
        if t == 5:
            pos[0] += 4; return None
        if t == 6:
            pos[0] += 8; return None
        if t == 7:
            n = struct.unpack_from(">i", buf, pos[0])[0]; pos[0] += 4 + n; return None
        if t == 8:
            return name()
        if t == 9:
            et = u8()
            n = struct.unpack_from(">i", buf, pos[0])[0]; pos[0] += 4
            return [payload(et) for _ in range(n)]
        if t == 10:
            d = {}
            while True:
                et = u8()
                if et == 0:
                    return d
                k = name()
                d[k] = payload(et)
        if t == 11:
            n = struct.unpack_from(">i", buf, pos[0])[0]; pos[0] += 4 + 4 * n; return None
        if t == 12:
            n = struct.unpack_from(">i", buf, pos[0])[0]; pos[0] += 4 + 8 * n; return None
        raise ValueError(f"nbt tag {t}")

    t = u8()
    name()
    return payload(t)


def cheats_enabled(level_dat):
    with gzip.open(level_dat, "rb") as fh:
        root = _read_nbt(fh.read())
    data = root.get("Data", root)
    return bool(data.get("allowCommands", 0)), data.get("LevelName", "?")


def enable_cheats(level_dat):
    """Flip Data.allowCommands to 1 in place (a fixed-width TAG_Byte, so the raw NBT bytes can
    be patched without a writer). Only ever done to the COPIED test world."""
    with gzip.open(level_dat, "rb") as fh:
        raw = fh.read()
    key = b"\x01\x00\x0dallowCommands"          # TAG_Byte, name length 13, name
    i = raw.find(key)
    if i < 0:
        return False
    raw = raw[:i + len(key)] + b"\x01" + raw[i + len(key) + 1:]
    with gzip.open(level_dat, "wb") as fh:
        fh.write(raw)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", required=True, help="Prism instance id (folder name)")
    ap.add_argument("--world", default="dcf-test")
    ap.add_argument("--template", default="New World (1)", help="world folder to copy if --world is missing")
    ap.add_argument("--profile", help="Prism account profile name (an MSA account may pop a login)")
    ap.add_argument("--offline", metavar="NAME", default="dcftester",
                    help="launch offline with this player name (default; no account needed for single-player)")
    ap.add_argument("--mod", help="Fabric mod jar to install into mods/ (ingress test)")
    ap.add_argument("--origin", nargs=3, type=int, default=[0, 64, 0])
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the world to load + selftest")
    ap.add_argument("--launch", action="store_true",
                    help="also run `prismlauncher --launch ...` (default: only provision and tail; you open the "
                         "world yourself, from your own launcher, in your own session)")
    a = ap.parse_args()

    inst = instance_dir(a.instance)
    saves = os.path.join(inst, "saves")
    world = os.path.join(saves, a.world)
    if not os.path.isdir(world):
        tpl = os.path.join(saves, a.template)
        if not os.path.isdir(tpl):
            sys.exit(f"no world {a.world!r} and no template {a.template!r} in {saves}: create a flat world "
                     f"with cheats ON once, then re-run with --template <its folder>")
        shutil.copytree(tpl, world, ignore=shutil.ignore_patterns("session.lock"))
        print(f"copied template world {a.template!r} -> {a.world!r}")
    level_dat = os.path.join(world, "level.dat")
    ok, level_name = cheats_enabled(level_dat)
    if not ok and a.world != a.template and enable_cheats(level_dat):
        ok, _ = cheats_enabled(level_dat)
        print(f"enabled cheats (allowCommands=1) in the copied world {a.world!r}")
    if not ok:
        sys.exit(f"world {level_name!r} has cheats OFF (allowCommands=0): the datapack needs /function; "
                 "open it once with 'Allow Cheats' or use a different template")

    # the datapack (autotest: dcf:selftest runs on load). Any OTHER pack in the copied world
    # that also owns the `dcf` namespace (the old MineCraft/ demo, for one) would override our
    # functions by load order, so it is removed from the COPY.
    dp = os.path.join(world, "datapacks", "dcf")
    for other in os.listdir(os.path.join(world, "datapacks")) if os.path.isdir(os.path.join(world, "datapacks")) else []:
        od = os.path.join(world, "datapacks", other)
        if other != "dcf" and os.path.isdir(os.path.join(od, "data", "dcf")):
            shutil.rmtree(od)
            print(f"removed conflicting datapack {other!r} from the copied world (it also uses the dcf namespace)")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "minecraft", "datapack", "gen_datapack.py"),
                        "--out", dp, "--origin", *map(str, a.origin), "--autotest"], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    print(f"datapack -> {dp}")

    udp = None
    if a.mod:
        mods = os.path.join(inst, "mods")
        os.makedirs(mods, exist_ok=True)
        shutil.copy(a.mod, os.path.join(mods, os.path.basename(a.mod)))
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        cfgdir = os.path.join(inst, "config")
        os.makedirs(cfgdir, exist_ok=True)
        with open(os.path.join(cfgdir, "dcf-minecraft.properties"), "w") as fh:
            fh.write("node-id=0x00B1\nbind=127.0.0.1:7811\n"
                     f"peers=127.0.0.1:{udp.getsockname()[1]}/proto\nflush-ms=20\n"
                     f"origin={a.origin[0]} {a.origin[1]} {a.origin[2]}\nmode=datapack\nnamespace=dcf\npulse-ticks=4\n")
        print(f"mod -> {mods}; mod config peers -> this harness (udp {udp.getsockname()[1]})")

    log = os.path.join(inst, "logs", "latest.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    open(log, "a").close()
    tail = LogTail(log).start()
    seen = {"tx": [], "pass": None, "rx": [], "lines": 0}

    def on_line(line, m):
        seen["lines"] += 1
        fr = M.parse_chat_tx(m)
        if fr is not None:
            seen["tx"].append(fr)
        if "DCF SELFTEST PASS" in m:
            seen["pass"] = m
        if "DCF RX " in m:
            seen["rx"].append(m)

    tail.subscribe(on_line)

    cmd = ["prismlauncher", "--launch", a.instance, "--world", a.world]
    cmd += ["--profile", a.profile] if a.profile else ["--offline", a.offline]
    if a.launch:
        print("launching:", " ".join(cmd))
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        print(f"provisioned. Now open the world {a.world!r} in instance {a.instance!r} from YOUR launcher "
              f"(or run:  {' '.join(cmd)}  in another terminal); this process only tails the log.")

    golden = M.GOLDEN
    deadline = time.time() + a.timeout
    print("waiting for the world to load and dcf:selftest to announce ...")
    while time.time() < deadline and seen["pass"] is None:
        time.sleep(0.25)
    if seen["pass"] is None:
        tail.stop()
        sys.exit(f"FAIL: no 'DCF SELFTEST PASS' within {a.timeout:.0f}s ({seen['lines']} log lines seen)")
    egress = [f for f in seen["tx"] if f == golden]
    if not egress:
        tail.stop()
        sys.exit(f"FAIL: selftest passed in-world but the chat egress never decoded the golden frame; saw {[f.hex() for f in seen['tx']]}")
    print(f"PASS egress: {golden.hex()} decoded from the client log (chat egress), "
          f"in-world selftest: {seen['pass']}")

    if udp is not None:
        # ingress: golden frame -> mod (proto dialect) -> datapack rx_commit -> "DCF RX" echo
        dg = ML.proto_frame_encode(golden, 1)
        udp.sendto(dg, ("127.0.0.1", 7811))
        deadline = time.time() + 30
        want = "DCF RX " + " ".join(map(str, M.frame_to_words(golden)))
        while time.time() < deadline and not any(want in m for m in seen["rx"]):
            time.sleep(0.25)
        if not any(want in m for m in seen["rx"]):
            tail.stop()
            sys.exit("FAIL ingress: the mod never echoed 'DCF RX' for the golden frame")
        print("PASS ingress: the mod wrote the golden frame into the register (DCF RX echoed)")
    tail.stop()
    print("client-in-the-loop: PASS")


if __name__ == "__main__":
    main()
