#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""io_matrix — the cross-language `punctim io` interop matrix (language x medium-in x
medium-out), per Documentation/DCF_MEDIUM_SPEC.md's determinism rule: for finite inputs,
`punctim io` in any language produces byte-identical output for identical input and URI.

Localhost + files only (stdlib, no Docker/nix). Legs, for every ordered (writer, reader)
pair of the available CLIs (same-language pairs included):

  file      hex -> file:W.dcf (writer), file:W.dcf -> hex: (reader) == corpus.hex;
            every writer's W.dcf == the reference byte stream
  stdio     writer (hex -> stdio:) | reader (stdio: -> hex:) == corpus.hex
  udp-proto reader (udp:dialect=proto,bind -> hex --expect 109) <- writer (hex -> udp:peer)
  udp-bare  the same with dialect=bare (SuperPack pairs + lone raw frame)
  hydra     writer (12 frames -> hydra:out=DIR WAVs, == direct frame_tx WAVs), then each
            reader (hydra:in=DIR -> hex --expect 12) == the 12 frames
  hydra-melody  the same on profile=melody (the musical profile; WAVs == frame_tx --profile melody)
  garbage   every CLI: file:garbage.dcf -> hex: == corpus.hex, same skipped_bytes as the
            Python CLI (and the in-script reference scan), frames_in == 109

Discovery: $PUNCTIM_PY/_C/_RS/_GO/_JS (shlex-split), $HYDRA_TOOLS_DIR. A CLI that cannot
run `version --json` is skipped everywhere; a medium a CLI does not support (exit 3) is a
skip, never a failure. Writes a Markdown report to stdout and tests/io_matrix_results.md;
exit 1 iff any cell failed.

    python3 tests/io_matrix.py [--only file,stdio,udp,hydra,hydra-melody,garbage] [--langs py,c,rs,go,js]
                               [--verbose] [--keep DIR] [--report PATH]
"""
import argparse
import datetime
import json
import os
import random
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGS = (("py", "Python", "PUNCTIM_PY", "python3 python/punctim.py"),
         ("c", "C", "PUNCTIM_C", "C_SDK/build/punctim"),
         ("rs", "Rust", "PUNCTIM_RS", "codec/target/debug/punctim"),
         ("go", "Go", "PUNCTIM_GO", "go/bin/punctim"),
         ("js", "Node", "PUNCTIM_JS", "node JS/nodejs/bin/punctim.js"))
LEGS = ("file", "stdio", "udp-proto", "udp-bare", "hydra", "hydra-melody", "garbage")
ALIASES = {"hex": "file", "udp": ("udp-proto", "udp-bare"), "udp_proto": "udp-proto",
           "udp_bare": "udp-bare", "proto": "udp-proto", "bare": "udp-bare"}
TITLES = {"file": "hex → file → hex", "stdio": "stdio pipe", "udp-proto": "udp proto",
          "udp-bare": "udp bare", "hydra": "hydra WAV dir",
          "hydra-melody": "hydra melody WAV dir", "garbage": "garbage twin"}
PASS, FAIL, SKIP = "✅", "❌", "⏭"
HYDRA_N = 12
T_SHORT, T_UDP, T_HYDRA = 30, 25, 90          # per-subprocess timeouts (s)
VERBOSE = False


@dataclass
class Lang:
    key: str
    name: str
    argv: list
    why: str = ""                 # non-empty => skipped everywhere
    info: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not self.why


@dataclass
class Res:
    rc: object                    # int, or None on timeout
    out: bytes
    err: str

    def why(self, what):
        if self.rc is None:
            return f"{what}: {self.err}"
        tail = [ln for ln in self.err.strip().splitlines() if ln.strip()]
        return f"{what} exit {self.rc}" + (f": {tail[-1][:160]}" if tail else "")


# ── helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(argv, timeout):
    if VERBOSE:
        log("$ " + shlex.join(argv))
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Res(None, b"", f"timeout after {timeout}s (killed)")
    except OSError as e:
        return Res(127, b"", str(e))
    return Res(p.returncode, p.stdout, p.stderr.decode("utf-8", "replace"))


def fresh(path):
    """Remove a previous run's output (a reused --keep dir must never yield a stale pass)."""
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)
    return path


def slurp(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def io_argv(lang, inp, out, *extra):
    return lang.argv + ["io", "--in", inp, "--out", out, *extra]


def stats_of(err):
    for ln in reversed(err.strip().splitlines()):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and "frames_in" in d:
            return d
    return None


def mismatch(expected, got, text=True):
    """'' when equal, else a short description of the first difference."""
    if expected == got:
        return ""
    if text:
        e, g = expected.decode("latin-1").splitlines(), got.decode("latin-1").splitlines()
        for i, (a, b) in enumerate(zip(e, g)):
            if a != b:
                return f"line {i + 1}: expected {a!r}, got {b[:40]!r} ({len(g)} lines)"
        return f"{len(g)} lines, expected {len(e)}"
    n = next((i for i, (a, b) in enumerate(zip(expected, got)) if a != b),
             min(len(expected), len(got)))
    return f"first difference at byte {n} ({len(got)} bytes, expected {len(expected)})"


def verdict(res, what, expected, got, text=True):
    """(status, reason) for one CLI run: exit 3 = skip, other non-zero = fail, then bytes."""
    if res.rc == 3:
        return SKIP, res.why(what) + " (medium unsupported in this build)"
    if res.rc != 0:
        return FAIL, res.why(what)
    diff = mismatch(expected, got, text)
    return (FAIL, f"{what}: output differs: {diff}") if diff else (PASS, "")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def udp_bound(port):
    """True once some socket is bound to `port` (Linux /proc; None if unknown)."""
    seen = False
    for tab in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(tab) as fh:
                seen = True
                for ln in fh.readlines()[1:]:
                    local = ln.split()[1]
                    if int(local.rsplit(":", 1)[1], 16) == port:
                        return True
        except OSError:
            continue
    return False if seen else None


# ── corpus ────────────────────────────────────────────────────────────────────
def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def gate(w):
    return len(w) == 17 and w[0] == 0xD3 and w[1] >> 4 == 1 and \
        crc16(w[:15]) == int.from_bytes(w[15:17], "big")


def scan(buf):
    """The DCF-Medium stream decode: (frames, skipped_bytes, tail_bytes)."""
    i, skipped, frames = 0, 0, []
    while len(buf) - i >= 17:
        if gate(buf[i:i + 17]):
            frames.append(bytes(buf[i:i + 17]))
            i += 17
        else:
            i, skipped = i + 1, skipped + 1
    return frames, skipped, len(buf) - i


def make_garbage(frames):
    """The deterministic garbage-injected .dcf twin of the corpus."""
    rng = random.Random(0xD3C0FFEE)
    bad = bytearray(frames[10])
    bad[16] ^= 0x5A                                    # a 0xD3 0x10 counterfeit, CRC fails
    special = {0: bytes(rng.randrange(256) for _ in range(5)),   # garbage prefix
               3: b"\xd3",                                        # a stray sync byte
               10: bytes(bad),                                    # full fake frame
               20: b"\xd3\x10" + bytes(rng.randrange(256) for _ in range(6)),  # fake prefix
               21: b"\xd3\x13\x00"}                              # D3 1x right before a frame
    out = bytearray()
    for i, f in enumerate(frames):
        junk = special.get(i)
        if junk is None and i % 9 == 4:
            junk = bytes(rng.randrange(256) for _ in range(rng.randint(1, 20)))
        out += (junk or b"") + f
    out += bytes(rng.randrange(256) for _ in range(21))   # suffix: 5 skipped + 16 tail
    got, skipped, tail = scan(out)
    assert got == frames and tail == 16, "garbage seed produced a false frame; change it"
    return bytes(out), skipped


# ── discovery ─────────────────────────────────────────────────────────────────
def resolve(argv):
    """Make path-like tokens absolute (the invoking cwd first, then the repo root)."""
    res = []
    for tok in argv:
        if os.sep in tok and not os.path.isabs(tok):
            for base in (os.getcwd(), ROOT):
                if os.path.exists(os.path.join(base, tok)):
                    tok = os.path.join(base, tok)
                    break
        res.append(tok)
    return res


def discover(keys):
    langs = []
    for key, name, env, default in LANGS:
        if key not in keys:
            continue
        spec = os.environ.get(env) or default
        lg = Lang(key, name, resolve(shlex.split(spec)))
        r = run(lg.argv + ["version", "--json"], T_SHORT)
        if r.rc != 0:
            lg.why = f"{key}: `{spec}` not runnable ({r.why('version --json')})"
        else:
            try:
                lg.info = json.loads(r.out.decode().strip().splitlines()[-1])
            except (ValueError, IndexError):
                lg.why = f"{key}: `version --json` printed no JSON"
        if lg.why:
            log(f"skip {lg.why}")
        langs.append(lg)
    return langs


# ── legs ──────────────────────────────────────────────────────────────────────
class Ctx:
    def __init__(self, langs, scratch, corpus_hex, frames, hydra):
        self.langs, self.dir, self.hex, self.frames = langs, scratch, corpus_hex, frames
        self.corpus = os.path.join(scratch, "corpus.hex")
        self.hydra = hydra                      # (frame_tx, frame_rx) or a skip reason
        self.garbage_note = ""
        self.retries = []                       # cells that passed/failed only on a retry

    def retry(self, leg, w, r, fn):
        """Run a timing-sensitive cell; one retry on failure, recorded in the report."""
        st, why = fn()
        if st == FAIL:
            log(f"retry {leg} {w.key}->{r.key}: {why}")
            st2, why2 = fn()
            self.retries.append(f"{leg} {w.key}→{r.key}: first attempt {FAIL} ({why}); "
                                f"retry {st2}")
            st, why = st2, why2
        return st, why

    def p(self, name):
        return os.path.join(self.dir, name)

    def pairs(self, fn):
        cells = {}
        for w in self.langs:
            for r in self.langs:
                dead = [lg.why for lg in (w, r) if not lg.ok]
                cells[(w.key, r.key)] = (SKIP, dead[0]) if dead else fn(w, r)
        return cells


def leg_file(cx):
    wcol, dcf_ref = {}, b"".join(cx.frames)
    for w in cx.langs:
        if not w.ok:
            wcol[w.key] = (SKIP, w.why)
            continue
        out = fresh(cx.p(f"W_{w.key}.dcf"))
        r = run(io_argv(w, "hex:path=" + cx.corpus, "file:path=" + out), T_SHORT)
        got = slurp(out)
        wcol[w.key] = verdict(r, f"{w.key} write", dcf_ref, got, text=False)

    def cell(w, r):
        if wcol[w.key][0] != PASS:
            return FAIL, f"writer {w.key} failed (see W.dcf column)"
        res = run(io_argv(r, f"file:path={cx.p(f'W_{w.key}.dcf')}", "hex:"), T_SHORT)
        return verdict(res, f"{r.key} read", cx.hex, res.out)
    return cx.pairs(cell), ("W.dcf ≡ ref", wcol)


def leg_stdio(cx):
    def cell(w, r):
        wa = io_argv(w, "hex:path=" + cx.corpus, "stdio:")
        ra = io_argv(r, "stdio:", "hex:")
        if VERBOSE:
            log(f"$ {shlex.join(wa)} | {shlex.join(ra)}")
        with open(cx.p("stdio_w.err"), "w+b") as we, open(cx.p("stdio_r.err"), "w+b") as re_:
            pw = subprocess.Popen(wa, stdout=subprocess.PIPE, stderr=we)
            pr = subprocess.Popen(ra, stdin=pw.stdout, stdout=subprocess.PIPE, stderr=re_)
            pw.stdout.close()
            try:
                out, _ = pr.communicate(timeout=T_SHORT)
                wrc = pw.wait(timeout=T_SHORT)
            except subprocess.TimeoutExpired:
                for p in (pw, pr):
                    p.kill()
                    p.wait()
                return FAIL, f"pipeline timeout after {T_SHORT}s (killed)"
            we.seek(0)
            re_.seek(0)
            wres = Res(wrc, b"", we.read().decode("utf-8", "replace"))
            rres = Res(pr.returncode, out, re_.read().decode("utf-8", "replace"))
        if wres.rc != 0:
            return (SKIP if wres.rc == 3 else FAIL), wres.why(f"{w.key} write")
        return verdict(rres, f"{r.key} read", cx.hex, out)
    return cx.pairs(cell), None


def udp_once(cx, w, r, dialect):
    port = free_port()
    rhex = fresh(cx.p(f"R_udp_{dialect}_{w.key}_{r.key}.hex"))
    ra = io_argv(r, f"udp:dialect={dialect},bind=127.0.0.1:{port}", "hex:path=" + rhex,
                 "--expect", str(len(cx.frames)), "--seconds", "10", "--stats")
    if VERBOSE:
        log(f"$ {shlex.join(ra)} &")
    rerr = open(cx.p("udp_r.err"), "w+b")
    pr = subprocess.Popen(ra, stdout=subprocess.DEVNULL, stderr=rerr)
    try:
        t_end = time.monotonic() + 10
        while pr.poll() is None and time.monotonic() < t_end:
            b = udp_bound(port)
            if b is None:                                   # no /proc: just wait a bit
                time.sleep(0.5)
                break
            if b:
                break
            time.sleep(0.02)
        if pr.poll() is not None:
            rerr.seek(0)
            res = Res(pr.returncode, b"", rerr.read().decode("utf-8", "replace"))
            return (SKIP if res.rc == 3 else FAIL), res.why(f"{r.key} read (before send)")
        wres = run(io_argv(w, "hex:path=" + cx.corpus,
                           f"udp:dialect={dialect},peer=127.0.0.1:{port}"), T_SHORT)
        try:
            pr.wait(timeout=T_UDP)
        except subprocess.TimeoutExpired:
            pr.kill()
            pr.wait()
            return FAIL, f"{r.key} read: timeout after {T_UDP}s (killed)"
        rerr.seek(0)
        rres = Res(pr.returncode, b"", rerr.read().decode("utf-8", "replace"))
    finally:
        if pr.poll() is None:
            pr.kill()
            pr.wait()
        rerr.close()
    if wres.rc != 0:
        return (SKIP if wres.rc == 3 else FAIL), wres.why(f"{w.key} write")
    return verdict(rres, f"{r.key} read", cx.hex, slurp(rhex))


def leg_udp(dialect):
    def leg(cx):
        def cell(w, r):                         # one retry (port race / scheduler hiccup)
            return cx.retry(f"udp-{dialect}", w, r, lambda: udp_once(cx, w, r, dialect))
        return cx.pairs(cell), None
    return leg


def leg_hydra(cx, profile=None):
    """profile=None: the default hydra leg; profile=NAME: the same leg on a musical
    profile (URI `profile=NAME`, reference `frame_tx … --profile NAME`)."""
    tag = "hydra" if profile is None else f"hydra_{profile}"
    popt = "" if profile is None else f",profile={profile}"
    pflags = [] if profile is None else ["--profile", profile]
    first = cx.frames[:HYDRA_N]
    c12 = cx.p("corpus12.hex")
    exp_hex = "".join(f.hex() + "\n" for f in first).encode()
    with open(c12, "wb") as fh:
        fh.write(exp_hex)
    no_hydra = {lg.key: f"{lg.key}: hydra: not in this build's media"
                for lg in cx.langs if lg.ok and "hydra" not in lg.info.get("media", [])}
    if isinstance(cx.hydra, str):
        why = cx.hydra
        return cx.pairs(lambda w, r: (SKIP, why)), \
            ("WAVs ≡ frame_tx", {lg.key: (SKIP, lg.why or why) for lg in cx.langs})
    tools_dir = fresh(cx.p(f"{tag}_tools"))       # a private snapshot: immune to a concurrent
    os.makedirs(tools_dir)                        # rebuild of hydramodem/dcf-tools/build
    tx, rx = (shutil.copy2(t, tools_dir) for t in cx.hydra)
    ref = fresh(cx.p(f"{tag}_ref"))
    os.makedirs(ref)
    for i, f in enumerate(first, 1):              # the reference: frame_tx called directly
        subprocess.run([tx, f.hex(), os.path.join(ref, f"hydra-{i:08d}.wav"), "--conv", *pflags],
                       check=True, capture_output=True, timeout=T_SHORT)
    ref_wavs = {n: slurp(os.path.join(ref, n)) for n in sorted(os.listdir(ref))}
    tools = f"tx={tx},rx={rx}"
    wcol = {}
    for w in cx.langs:
        if not w.ok or w.key in no_hydra:
            wcol[w.key] = (SKIP, w.why or no_hydra[w.key])
            continue
        d = fresh(cx.p(f"{tag}_{w.key}"))
        res = run(io_argv(w, "hex:path=" + c12, f"hydra:out={d},{tools}{popt}"), T_HYDRA)
        names = sorted(n for n in os.listdir(d) if not n.startswith(".")) \
            if os.path.isdir(d) else []
        diff = ""
        if names != list(ref_wavs):
            diff = f"files {names[:3]}{'…' if len(names) > 3 else ''} != {len(ref_wavs)} " \
                   f"reference names hydra-00000001.wav…"
        else:
            bad = [n for n in names if slurp(os.path.join(d, n)) != ref_wavs[n]]
            diff = f"{len(bad)} WAV(s) differ from frame_tx, first {bad[0]}" if bad else ""
        wcol[w.key] = verdict(res, f"{w.key} write", b"", b"")
        if wcol[w.key][0] == PASS and diff:
            wcol[w.key] = (FAIL, f"{w.key} write: {diff}")

    def cell(w, r):
        if w.key in no_hydra or r.key in no_hydra:
            return SKIP, no_hydra.get(w.key) or no_hydra[r.key]
        if wcol[w.key][0] == SKIP:
            return wcol[w.key]
        if wcol[w.key][0] != PASS:
            return FAIL, f"writer {w.key} failed (see WAVs column)"
        return cx.retry(tag, w, r, lambda: hydra_read(w, r))

    def hydra_read(w, r):
        rhex = fresh(cx.p(f"R_{tag}_{w.key}_{r.key}.hex"))
        res = run(io_argv(r, f"hydra:in={cx.p(f'{tag}_{w.key}')},{tools}{popt}", "hex:path=" + rhex,
                          "--expect", str(HYDRA_N), "--seconds", "60"), T_HYDRA)
        return verdict(res, f"{r.key} read", exp_hex, slurp(rhex))
    return cx.pairs(cell), ("WAVs ≡ frame_tx", wcol)


def leg_garbage(cx):
    garbage, ref_skip = make_garbage(cx.frames)
    gpath = cx.p("garbage.dcf")
    with open(gpath, "wb") as fh:
        fh.write(garbage)
    runs = {}
    for lg in cx.langs:
        if lg.ok:
            runs[lg.key] = run(io_argv(lg, "file:path=" + gpath, "hex:", "--stats"), T_SHORT)
    py = stats_of(runs["py"].err) if "py" in runs and runs["py"].rc == 0 else None
    base = py["skipped_bytes"] if py and "skipped_bytes" in py else ref_skip
    cx.garbage_note = (f"`garbage.dcf` = {len(garbage)} B (109 frames + {len(garbage) - 109 * 17}"
                       f" B junk); reference scan: skipped_bytes = {ref_skip}, tail_bytes = 16; "
                       f"Python CLI reported {py['skipped_bytes'] if py else 'n/a'}")
    row = {}
    for lg in cx.langs:
        if not lg.ok:
            row[lg.key] = (SKIP, lg.why)
            continue
        res = runs[lg.key]
        st, why = verdict(res, f"{lg.key} read", cx.hex, res.out)
        s = stats_of(res.err) if st == PASS else None
        if st == PASS:
            if s is None:
                st, why = FAIL, f"{lg.key}: no --stats JSON line on stderr"
            elif (s.get("skipped_bytes"), s.get("frames_in"), s.get("frames_out")) != \
                    (base, 109, 109) or (lg.key == "py" and base != ref_skip):
                st, why = FAIL, (f"{lg.key}: skipped_bytes={s.get('skipped_bytes')} "
                                 f"frames_in={s.get('frames_in')} frames_out="
                                 f"{s.get('frames_out')}, expected {base}/109/109"
                                 f" (reference scan {ref_skip})")
        row[lg.key] = (st, why)
    return {("garbage.dcf", k): v for k, v in row.items()}, None


LEG_FN = {"file": leg_file, "stdio": leg_stdio, "udp-proto": leg_udp("proto"),
          "udp-bare": leg_udp("bare"), "hydra": leg_hydra,
          "hydra-melody": lambda cx: leg_hydra(cx, "melody"), "garbage": leg_garbage}
LEG_DESC = {
    "file": "writer `io --in hex:path=corpus.hex --out file:path=W.dcf`; reader "
            "`io --in file:path=W.dcf --out hex:` == corpus.hex; W.dcf ≡ the 109 frames "
            "concatenated (so every writer's W.dcf is identical)",
    "stdio": "`writer io --in hex:path=corpus.hex --out stdio: | reader io --in stdio: "
             "--out hex:` == corpus.hex",
    "udp-proto": "reader `io --in udp:dialect=proto,bind=127.0.0.1:P --out hex:path=R.hex "
                 "--expect 109 --seconds 10 --stats` (started first), writer `io --in "
                 "hex:path=corpus.hex --out udp:dialect=proto,peer=127.0.0.1:P`; R.hex == "
                 "corpus.hex",
    "udp-bare": "as udp proto with `dialect=bare` (SuperPack pairs + a lone raw frame)",
    "hydra": f"writer `io --in hex:path=corpus12.hex --out hydra:out=DIR,tx=…,rx=…` (first "
             f"{HYDRA_N} frames; WAVs ≡ `frame_tx HEX hydra-%08d.wav --conv`), reader `io "
             f"--in hydra:in=DIR,tx=…,rx=… --out hex:path=R.hex --expect {HYDRA_N} "
             f"--seconds 60` == corpus12.hex",
    "hydra-melody": "as hydra with `profile=melody` (the musical tone-table profile, "
                    "hydramodem/docs/MUSIC.md): WAVs ≡ `frame_tx HEX hydra-%08d.wav --conv "
                    "--profile melody`, every reader recovers the 12 frames",
    "garbage": "every CLI `io --in file:path=garbage.dcf --out hex: --stats` == corpus.hex, "
               "skipped_bytes == Python's, frames_in == frames_out == 109",
}


# ── report ────────────────────────────────────────────────────────────────────
def table(leg, cells, extra, langs, note):
    lines, notes, tally = [], {}, {PASS: 0, FAIL: 0, SKIP: 0}

    def mark(key, st, why):
        tally[st] += 1
        if st == PASS:
            return PASS
        n = notes.setdefault(why, [len(notes) + 1, []])
        n[1].append(key)
        return f"{st} [{n[0]}]"
    rows = list(dict.fromkeys(w for w, _ in cells))         # writers, in --langs order
    cols = [lg.key for lg in langs]
    head = ["writer \\ reader"] + cols + ([extra[0]] if extra else [])
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for w in rows:
        row = [f"**{w}**"] + [mark(f"{w}→{r}", *cells[(w, r)]) for r in cols]
        if extra:
            row.append(mark(f"{w} writer", *extra[1][w]))
        lines.append("| " + " | ".join(row) + " |")
    out = [f"### {TITLES[leg]} (`{leg}`)", "", LEG_DESC[leg] + ".", ""]
    if note:
        out += [note + ".", ""]
    out += lines
    if notes:
        out.append("")
        for why, (n, keys) in notes.items():
            out.append(f"- [{n}] {', '.join(keys)}: {why}")
    return out + [""], tally


def main(argv=None):
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="comma list of legs: " + ",".join(LEGS) + " (udp = both)")
    ap.add_argument("--langs", default="py,c,rs,go,js", help="comma list of py,c,rs,go,js")
    ap.add_argument("--verbose", action="store_true", help="print each command (stderr)")
    ap.add_argument("--keep", metavar="DIR", help="use and keep this scratch dir")
    ap.add_argument("--report", default=os.path.join(ROOT, "tests", "io_matrix_results.md"),
                    help="Markdown report path ('' = stdout only)")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    legs = list(LEGS)
    if a.only:
        want = set()
        for x in a.only.split(","):
            x = ALIASES.get(x.strip(), x.strip())
            want.update(x if isinstance(x, tuple) else (x,))
        if want - set(LEGS):
            ap.error(f"unknown leg(s) {sorted(want - set(LEGS))}; one of {', '.join(LEGS)}")
        legs = [lg for lg in LEGS if lg in want]
    keys = [k.strip() for k in a.langs.split(",") if k.strip()]
    if set(keys) - {k for k, *_ in LANGS}:
        ap.error(f"unknown language(s) in --langs {a.langs!r}")

    with open(os.path.join(ROOT, "Documentation", "golden_vectors.json")) as fh:
        frames = [bytes.fromhex(v["frame"]) for v in json.load(fh)["encode_basis"]]
    assert len(frames) == 109 and all(gate(f) for f in frames)
    corpus_hex = "".join(f.hex() + "\n" for f in frames).encode()

    scratch = a.keep or tempfile.mkdtemp(prefix="io_matrix.")
    os.makedirs(scratch, exist_ok=True)
    with open(os.path.join(scratch, "corpus.hex"), "wb") as fh:
        fh.write(corpus_hex)
    hdir = os.path.abspath(os.path.join(ROOT, os.environ.get("HYDRA_TOOLS_DIR")
                                        or "hydramodem/dcf-tools/build"))
    tx, rx = os.path.join(hdir, "frame_tx"), os.path.join(hdir, "frame_rx")
    hydra = (tx, rx) if all(os.access(t, os.X_OK) for t in (tx, rx)) else \
        f"HydraModem frame_tx/frame_rx not found in {hdir} (run hydramodem/dcf-tools/build.sh)"

    try:
        langs = discover(keys)
        cx = Ctx(langs, scratch, corpus_hex, frames, hydra)
        git = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip() or "unknown"
        md = ["# punctim io matrix", "",
              f"- date: {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M UTC}",
              f"- git: `{git}`",
              f"- corpus: the {len(frames)} `encode_basis` frames of "
              "`Documentation/golden_vectors.json` (`corpus.hex`)",
              f"- legs: {', '.join(legs)}; generated by `python3 tests/io_matrix.py`",
              "- hydra tools: " + (f"`{tx.replace(ROOT + '/', '')}`, `{rx.replace(ROOT + '/', '')}`"
                                   " (snapshotted into the scratch dir for the run)"
                                   if isinstance(hydra, tuple) else f"{SKIP} {hydra}"), "",
              "| lang | command | version --json |", "|---|---|---|"]
        for lg in langs:
            v = json.dumps(lg.info, separators=(",", ":")) if lg.ok else f"{SKIP} {lg.why}"
            md.append(f"| {lg.key} | `{shlex.join(lg.argv).replace(ROOT + '/', '')}` | {v} |")
        md.append("")
        total = {PASS: 0, FAIL: 0, SKIP: 0}
        for leg in legs:
            log(f"leg {leg} …")
            t0 = time.monotonic()
            cells, extra = LEG_FN[leg](cx)
            lines, tally = table(leg, cells, extra, langs,
                                 cx.garbage_note if leg == "garbage" else "")
            log(f"leg {leg}: {tally[PASS]} pass, {tally[FAIL]} fail, {tally[SKIP]} skipped "
                f"({time.monotonic() - t0:.1f}s)")
            md += lines
            for k in total:
                total[k] += tally[k]
        if cx.retries:
            md += ["### retried cells", ""] + [f"- {x}" for x in cx.retries] + [""]
        summary = f"io-matrix: {total[PASS]} pass, {total[FAIL]} fail, {total[SKIP]} skipped"
        md += [summary, ""]
        text = "\n".join(md)
        print(text, end="")
        if a.report:
            with open(a.report, "w", encoding="utf-8") as fh:
                fh.write(text)
        return 1 if total[FAIL] else 0
    finally:
        if not a.keep:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
