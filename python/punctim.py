#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""punctim — the DCF medium tool (Python reference implementation).

Reads DeModFrames from any medium and emits them on any other, byte-deterministically;
encodes/decodes single frames; certifies the medium codecs against the golden vectors.
The same CLI exists in C, Rust, Go and Node (Documentation/DCF_MEDIUM_SPEC.md §punctim)::

    punctim version [--json]
    punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
                    [--no-validate] [--stats] [--queue N]
    punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
    punctim decode  (HEX | --stdin) [--json]
    punctim certify [--vectors DIR] [--family NAME ...]
    punctim sim     ...

Exit codes: 0 ok · 1 I/O error · 2 usage · 3 medium unsupported · 4 certification failed
· 5 invalid frame · 6 --expect not met.

Determinism rule (normative): for finite inputs, `punctim io` in any language produces
byte-identical output for identical input and URI; `udp:dialect=proto` needs ts=0 (default).

    printf 'd31312340001ffffdeadbeefab12cd24c0\\n' | punctim io --in hex: --out file:path=a.dcf
    punctim io --in file:path=a.dcf --out udp:peer=127.0.0.1:9100
    punctim io --in udp:bind=0.0.0.0:9100 --out hex: --expect 1 --seconds 5
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
for _mcp in (os.path.join(_HERE, "MCP"), os.path.join(_HERE, "dcf", "MCP")):
    if os.path.isdir(_mcp):
        sys.path.insert(0, _mcp)
        break

import wirelab_core as wire  # noqa: E402

EXIT_OK, EXIT_IO, EXIT_USAGE, EXIT_UNSUPPORTED, EXIT_CERT, EXIT_INVALID, EXIT_EXPECT = range(7)
IMPL = "python"
MEDIA = ("file", "stdio", "hex", "udp", "l2eth", "loop", "hydra", "afsk", "audio", "sdr",
         "janus")


def _version():
    pp = os.path.join(_HERE, "pyproject.toml")
    if os.path.isfile(pp):
        with open(pp) as fh:
            m = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M)
        if m:
            return m.group(1)
    try:
        from importlib.metadata import version
        return version("demod-dcf")
    except Exception:
        pass
    try:
        import dcf
        return dcf.__version__
    except Exception:
        return "0"


def _num(s, what, lo, hi):
    t = str(s).strip()
    try:
        v = int(t[2:], 16) if t.lower().startswith("0x") else int(t, 10)
    except ValueError:
        raise _Usage(f"{what}: {s!r} is not an integer (decimal or 0x hex)") from None
    if not lo <= v <= hi:
        raise _Usage(f"{what}: {v} out of range {lo}..{hi}")
    return v


class _Usage(Exception):
    pass


# ── version ───────────────────────────────────────────────────────────────────
def cmd_version(a):
    v = _version()
    if a.json:
        from dcf.medium import FAMILIES
        print(json.dumps({"name": "punctim", "version": v, "impl": IMPL, "media": list(MEDIA),
                          "families": list(FAMILIES)}, separators=(",", ":")))
    else:
        print(f"punctim {v} ({IMPL})")
    return EXIT_OK


# ── io ────────────────────────────────────────────────────────────────────────
def cmd_io(a):
    from dcf.medium import run_io, UsageError, MediumUnsupported
    try:
        st = run_io(a.inp, a.out, count=a.count, seconds=a.seconds, expect=a.expect,
                    validate=not a.no_validate, queue=a.queue)
    except UsageError as e:
        print(f"punctim io: {e}", file=sys.stderr)
        return EXIT_USAGE
    except MediumUnsupported as e:
        print(f"punctim io: medium unsupported: {e}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except BrokenPipeError:
        _quiet_stdout()
        return EXIT_IO
    except OSError as e:
        print(f"punctim io: I/O error: {e}", file=sys.stderr)
        return EXIT_IO
    if a.stats:
        print(json.dumps(st, separators=(",", ":")), file=sys.stderr)
    if a.expect is not None and st["frames_out"] != a.expect:
        print(f"punctim io: expected {a.expect} frames, wrote {st['frames_out']}",
              file=sys.stderr)
        return EXIT_EXPECT
    return EXIT_OK


def _quiet_stdout():
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass


# ── encode ────────────────────────────────────────────────────────────────────
def cmd_encode(a):
    t = _num(a.type, "--type", 0, 15)
    seq = _num(a.seq, "--seq", 0, 0xFFFF)
    src = _num(a.src, "--src", 0, 0xFFFF)
    dst = _num(a.dst, "--dst", 0, 0xFFFF)
    ts = _num(a.ts, "--ts", 0, (1 << 64) - 1) & 0xFFFFFF       # ts_us is 24-bit on the wire
    if a.payload is not None:
        p = a.payload.strip()
        if len(p) != 8 or not all(c in "0123456789abcdefABCDEF" for c in p):
            raise _Usage("--payload wants exactly 8 hex digits (4 bytes)")
        payload = bytes.fromhex(p)
    else:
        b = a.text.encode("utf-8")
        if len(b) > 4:
            raise _Usage("--text is at most 4 UTF-8 bytes (zero-padded)")
        payload = b.ljust(4, b"\x00")
    sys.stdout.write(wire.encode(t, seq, src, dst, payload, ts).hex() + "\n")
    return EXIT_OK


# ── decode ────────────────────────────────────────────────────────────────────
DECODE_KEYS = ("hex", "valid", "syndrome", "frame_type", "frame_type_name", "seq", "src",
               "dst", "payload", "ts_us", "crc")


def decode_record(text):
    """The decode record for one hex string: wirelab_core.decode() fields + hex/valid/
    syndrome (+ error when invalid). Fields are read raw from a 17-byte word even when
    the gate fails."""
    s = text.strip(" \t\r\v\f")
    if len(s) % 2 or not all(c in "0123456789abcdefABCDEF" for c in s):
        return {"hex": s, "valid": False, "error": "not hex"}
    b = bytes.fromhex(s)
    if len(b) != wire.FRAME_LEN:
        return {"hex": b.hex(), "valid": False, "error": f"length {len(b)} != 17"}
    rec = {"hex": b.hex(), "valid": True, "syndrome": wire.syndrome(b),
           "frame_type": b[1] & 0x0F,
           "frame_type_name": wire.FRAME_TYPES.get(b[1] & 0x0F, f"0x{b[1] & 0x0F:X}"),
           "seq": int.from_bytes(b[2:4], "big"), "src": int.from_bytes(b[4:6], "big"),
           "dst": int.from_bytes(b[6:8], "big"), "payload": b[8:12].hex(),
           "ts_us": int.from_bytes(b[12:15], "big"), "crc": int.from_bytes(b[15:17], "big")}
    try:
        wire.decode(b)
    except ValueError as e:
        rec["valid"] = False
        rec["error"] = str(e)
    return rec


def _human(rec):
    if "syndrome" not in rec:
        return f"invalid ({rec['error']}) {rec['hex']}"
    head = "valid" if rec["valid"] else f"invalid ({rec['error']})"
    return (f"{head} type={rec['frame_type']} ({rec['frame_type_name']}) seq={rec['seq']} "
            f"src={rec['src']} dst={rec['dst']} payload={rec['payload']} ts_us={rec['ts_us']} "
            f"crc=0x{rec['crc']:04x} syndrome=0x{rec['syndrome']:04x}")


def cmd_decode(a):
    if a.stdin == (a.hex is not None):
        raise _Usage("decode wants exactly one of HEX or --stdin")
    if a.stdin:
        items = []
        for line in sys.stdin.buffer:
            s = line.decode("latin-1").strip(" \t\r\v\f\n")
            if s and not s.startswith("#"):
                items.append(s)
    else:
        items = [a.hex]
    rc = EXIT_OK
    for it in items:
        rec = decode_record(it)
        if not rec["valid"]:
            rc = EXIT_INVALID
        print(json.dumps(rec, separators=(",", ":")) if a.json else _human(rec))
    return rc


# ── certify ───────────────────────────────────────────────────────────────────
def cmd_certify(a):
    from dcf.medium import certify_vectors, find_vectors, load_vectors, UsageError
    try:
        path = find_vectors(a.vectors)
        vec = load_vectors(path)
    except (OSError, ValueError) as e:
        print(f"punctim certify: {e}", file=sys.stderr)
        return EXIT_IO
    try:
        results = certify_vectors(vec, a.family)
    except UsageError as e:
        print(f"punctim certify: {e}", file=sys.stderr)
        return EXIT_USAGE
    print(f"vectors: {path}")
    total, failed = 0, 0
    for fam, ok, n, msg in results:
        total += n if fam != "anchors" else 0
        if ok:
            print(f"PASS {fam} ({n} {'basis frames' if fam == 'anchors' else 'cases'})")
        else:
            failed += 1
            print(f"FAIL {fam}: {msg}")
    if failed:
        print(f"CERTIFICATION FAILED ({failed} famil{'y' if failed == 1 else 'ies'})")
        return EXIT_CERT
    print(f"ALL MEDIUM VECTORS PASS ({total} cases, impl {IMPL})")
    return EXIT_OK


# ── sim (WP-Sim plugs in as dcf.sim.main(argv)) ───────────────────────────────
def cmd_sim(argv):
    try:
        from dcf.sim import main as sim_main
    except ImportError:
        print("sim: not yet implemented (WP-Sim)", file=sys.stderr)
        return EXIT_USAGE
    return sim_main(argv)


# ── argv ──────────────────────────────────────────────────────────────────────
def _parser():
    ap = argparse.ArgumentParser(
        prog="punctim",
        description="DCF medium tool: move DeModFrames between any two media, "
                    "deterministically (Documentation/DCF_MEDIUM_SPEC.md).",
        epilog="media: file: stdio: hex: udp: l2eth: loop: hydra: afsk: (audio:) sdr: janus:\n"
               "exit: 0 ok, 1 I/O, 2 usage, 3 medium unsupported, 4 cert failed, "
               "5 invalid frame, 6 --expect not met",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", metavar="{version,io,encode,decode,certify,sim}")
    sub.required = True

    v = sub.add_parser("version", help="print the version")
    v.add_argument("--json", action="store_true")

    io = sub.add_parser("io", help="read frames from one medium, write them to another")
    io.add_argument("--in", dest="inp", required=True, metavar="URI")
    io.add_argument("--out", required=True, metavar="URI")
    io.add_argument("--count", type=_nonneg, metavar="N", help="stop after N frames written")
    io.add_argument("--seconds", type=float, metavar="S", help="stop after S seconds")
    io.add_argument("--expect", type=_nonneg, metavar="N",
                    help="exit 6 unless exactly N frames were written (an infinite input "
                         "without --count stops at N)")
    io.add_argument("--no-validate", action="store_true",
                    help="pass frames that fail the gate (debug; not deterministic-normative)")
    io.add_argument("--stats", action="store_true", help="one JSON stats line on stderr")
    io.add_argument("--queue", type=_pos, default=256, metavar="N",
                    help="bounded queue for infinite inputs (default 256)")

    e = sub.add_parser("encode", help="encode one DeModFrame -> 34 hex + newline")
    e.add_argument("--type", required=True)
    e.add_argument("--seq", required=True)
    e.add_argument("--src", required=True)
    e.add_argument("--dst", required=True)
    g = e.add_mutually_exclusive_group(required=True)
    g.add_argument("--payload", metavar="HEX8")
    g.add_argument("--text", metavar="S")
    e.add_argument("--ts", default="0", help="24-bit timestamp (us)")

    d = sub.add_parser("decode", help="decode a frame (hex) -> fields; exit 5 if invalid")
    d.add_argument("hex", nargs="?")
    d.add_argument("--stdin", action="store_true", help="decode one hex frame per line")
    d.add_argument("--json", action="store_true")

    c = sub.add_parser("certify", help="certify the medium codecs vs medium_vectors.json")
    c.add_argument("--vectors", metavar="DIR", help="dir holding medium_vectors.json "
                                                    "(default: $PUNCTIM_VECTORS or Documentation/)")
    c.add_argument("--family", nargs="+", metavar="NAME")

    sub.add_parser("sim", help="size the hardware a system needs (WP-Sim)", add_help=False)
    return ap


def _nonneg(s):
    v = int(s, 0)
    if v < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return v


def _pos(s):
    v = int(s, 0)
    if v < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return v


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "sim":
        return cmd_sim(argv[1:])
    a = _parser().parse_args(argv)               # argparse exits 2 on a usage error
    try:
        return {"version": cmd_version, "io": cmd_io, "encode": cmd_encode,
                "decode": cmd_decode, "certify": cmd_certify}[a.cmd](a)
    except _Usage as e:
        print(f"punctim {a.cmd}: {e}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:
        _quiet_stdout()
        return EXIT_IO


if __name__ == "__main__":
    sys.exit(main())
