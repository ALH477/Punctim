# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""End-to-end DCF-QKD harness: the full classical-plane flow a real QKD integration
performs, with no photonic hardware and no network.

    master SAE --enc_keys--> KME-A                        (ETSI GS QKD 014)
    master SAE --key-ID beacon: 4 x DeModFrame--> slave    (DCF: the out-of-scope bit)
    slave  SAE --dec_keys--> KME-B                        (ETSI GS QKD 014)
    assert master key material == slave key material

Then the negative tests that matter on a bench: CRC rejection of a single flipped bit,
single-delivery enforcement on a replayed key_ID, and the load-bearing export invariant
— no byte of any delivered key ever appears on the wire.

Self-contained run (loopback transport, deterministic, hardware-free):
    python3 python/dcf/qkd/demo.py --count 3

Against QuKayDee or vendor hardware (same code path, no changes):
    python3 python/dcf/qkd/demo.py --external \
        --kme-a https://kme-a.example:443 --kme-b https://kme-b.example:443 \
        --cert sae.crt --key sae.key --ca ca.crt

Exit code is 0 only if every check passes, so this drops straight into CI.
See Documentation/DCF_QKD_SPEC.md.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _mcp in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "MCP"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP")):
    if os.path.isdir(_mcp):
        sys.path.insert(0, _mcp)

from dcf.qkd.beacon import channel_id                      # noqa: E402
from dcf.qkd.etsi014 import Etsi014Client, KmeError        # noqa: E402
from dcf.qkd.mock_kme import KeyStore, serve               # noqa: E402
from dcf.qkd.sae import MasterSae, SlaveSae                # noqa: E402
from dcf.transport import LoopbackMedium, LoopbackTransport  # noqa: E402
from qkdlab_core import KeyIdReassembler, packetize, FRAGS  # noqa: E402

MASTER_SAE = "SAE-MASTER-01"
SLAVE_SAE = "SAE-SLAVE-01"
MASTER_NODE, SLAVE_NODE = 0x0001, 0x0002
CHANNEL = channel_id("qkd")
SETTLE_TIMEOUT = 5.0


class Report:
    """Collects pass/fail checks; renders as text or JSON."""

    def __init__(self, quiet=False):
        self.rows = []
        self.quiet = quiet

    def check(self, name, ok, detail=""):
        self.rows.append({"check": name, "ok": bool(ok), "detail": detail})
        if not self.quiet:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        return ok

    @property
    def failed(self):
        return sum(1 for r in self.rows if not r["ok"])

    def finish(self, as_json):
        passed = len(self.rows) - self.failed
        if as_json:
            print(json.dumps({"passed": passed, "total": len(self.rows),
                              "checks": self.rows}, indent=2))
        elif not self.quiet:
            print(f"\n{passed}/{len(self.rows)} checks passed")
        return 1 if self.failed else 0


def _wait_for(pred, timeout=SETTLE_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def run(args):
    rep = Report(quiet=args.json)
    servers = []
    log = (lambda *a: None) if args.json else print

    if args.external:
        kme_a_url, kme_b_url = args.kme_a, args.kme_b
        log(f"external KMEs: {kme_a_url} / {kme_b_url}\n")
    else:
        store = KeyStore()
        servers = [serve("KME-A", "KME-B", args.port_a, store, quiet=True),
                   serve("KME-B", "KME-A", args.port_b, store, quiet=True)]
        kme_a_url = f"http://127.0.0.1:{args.port_a}"
        kme_b_url = f"http://127.0.0.1:{args.port_b}"
        time.sleep(0.2)
        log(f"mock KMEs up: {kme_a_url} / {kme_b_url}  (shared keystore)\n")

    tls = dict(certfile=args.cert, keyfile=args.key, cafile=args.ca, insecure=args.insecure)
    # Real KMEs derive SAE identity from the mTLS client cert.  The mock has no cert to
    # read over plain http, so it accepts an explicit header instead.
    hdr = (lambda sae: {} if args.external else {"X-SAE-ID": sae})
    mc = Etsi014Client(kme_a_url, MASTER_SAE, **tls, extra_headers=hdr(MASTER_SAE))
    sc = Etsi014Client(kme_b_url, SLAVE_SAE, **tls, extra_headers=hdr(SLAVE_SAE))

    # One in-process broadcast wire; a passive tap records every byte that crosses it.
    medium = LoopbackMedium()
    t_master = LoopbackTransport("master", medium)
    t_slave = LoopbackTransport("slave", medium)
    t_tap = LoopbackTransport("tap", medium)
    wire = []
    t_tap.start(lambda frame, meta=None: wire.append(bytes(frame)))

    master = MasterSae(mc, SLAVE_SAE, t_master, MASTER_NODE, CHANNEL)
    slave = SlaveSae(sc, MASTER_SAE, t_slave, SLAVE_NODE, CHANNEL)
    master.beacon.start()      # starts the tx thread; loopback never echoes the sender
    slave.start()

    try:
        # ── 1. status ────────────────────────────────────────────────────────
        log("1. ETSI 014 status")
        try:
            st = master.status()
            rep.check("KME-A status reachable", True,
                      f"source={st.get('source_KME_ID')} stored={st.get('stored_key_count')} "
                      f"key_size={st.get('key_size')}")
        except KmeError as exc:
            rep.check("KME-A status reachable", False, str(exc))
            return rep.finish(args.json)

        # ── 2 + 3. master requests keys and beacons each key_ID over DCF ──────
        log("\n2. master SAE -> enc_keys")
        try:
            keys = master.request_keys(number=args.count, size=args.size)
        except KmeError as exc:
            rep.check("enc_keys", False, str(exc))
            return rep.finish(args.json)
        rep.check(f"received {args.count} key(s)", len(keys) == args.count,
                  f"{keys[0].bits} bits each" if keys else "none returned")
        if not keys:
            return rep.finish(args.json)

        log(f"\n3. key_ID beacon over DeModFrame ({FRAGS} frames x 17B, "
            f"loopback transport, channel 0x{CHANNEL:04X})")
        for i, k in enumerate(keys):
            epoch = master.announce(k.key_ID)
            if i == 0 and not args.json:
                _wait_for(lambda: len(wire) >= FRAGS)
                log(f"     key_ID {k.key_ID}  (epoch {epoch})")
                for j, f in enumerate(wire[:FRAGS]):
                    seq = int.from_bytes(f[2:4], "big")
                    log(f"     frag {j}  {f.hex()}  "
                        f"(type={f[1] & 0x0F} seq=0x{seq:04X} epoch={seq >> 2} "
                        f"payload={f[8:12].hex()})")

        ok = _wait_for(lambda: len(slave.keys) >= args.count)
        rep.check(f"{args.count} key_ID(s) reassembled from {FRAGS} frames each",
                  ok and master.beacon.pending == 0 and slave.beacon.pending == 0,
                  f"{len(slave.keys)}/{args.count}, no dangling fragments")
        if slave.errors:
            rep.check("slave dec_keys", False, str(slave.errors[0]))

        matched = sum(1 for kid, kb in master.keys.items() if slave.keys.get(kid) == kb)
        rep.check("master and slave key material identical",
                  matched == args.count, f"{matched}/{args.count} byte-for-byte")

        # ── 4. negative tests ────────────────────────────────────────────────
        log("\n4. negative tests")
        good = packetize(keys[0].key_ID, 63, 1, MASTER_NODE, CHANNEL)[0]
        corrupt = bytearray(good)
        corrupt[9] ^= 0x01                      # flip one payload bit
        r = KeyIdReassembler()
        rep.check("CRC rejects a single flipped bit", r.push(bytes(corrupt)) == [],
                  "corrupt frame dropped, never fatal")

        try:
            sc.get_key_with_ids(MASTER_SAE, [keys[0].key_ID])
            rep.check("replayed key_ID refused (single-delivery)", False, "key served twice")
        except KmeError as exc:
            rep.check("replayed key_ID refused (single-delivery)", "401" in str(exc),
                      str(exc)[:90])

        # The load-bearing export invariant: the wire carries key_IDs, never keys.
        blob = b"".join(wire)
        leaked = [kid for kid, kb in master.keys.items() if kb in blob]
        rep.check("no key material on the wire", not leaked,
                  f"{len(wire)} frames captured, {len(blob)} bytes scanned"
                  if not leaked else f"LEAKED {leaked}")

        return rep.finish(args.json)
    finally:
        slave.stop()
        master.beacon.stop()
        t_tap.stop()
        master.forget()
        slave.forget()
        for s in servers:
            s.shutdown()
            s.server_close()      # release the listening socket, not just the loop


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dcf-qkd-demo",
                                 description="DCF <-> ETSI GS QKD 014 end-to-end harness")
    ap.add_argument("--count", type=int, default=3, help="keys to exercise (default 3)")
    ap.add_argument("--size", type=int, default=256, help="key size in bits (default 256)")
    ap.add_argument("--port-a", type=int, default=8010)
    ap.add_argument("--port-b", type=int, default=8020)
    ap.add_argument("--external", action="store_true", help="use real/remote KMEs")
    ap.add_argument("--kme-a", default="", help="KME-A base URL (with --external)")
    ap.add_argument("--kme-b", default="", help="KME-B base URL (with --external)")
    ap.add_argument("--cert", default=None, help="SAE client certificate (mTLS)")
    ap.add_argument("--key", default=None, help="SAE private key (mTLS)")
    ap.add_argument("--ca", default=None, help="CA bundle used to verify the KME")
    ap.add_argument("--insecure", action="store_true", help="skip KME cert verification")
    ap.add_argument("--json", action="store_true", help="machine-readable output for CI")
    args = ap.parse_args(argv)
    if args.external and not (args.kme_a and args.kme_b):
        ap.error("--external requires --kme-a and --kme-b")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
