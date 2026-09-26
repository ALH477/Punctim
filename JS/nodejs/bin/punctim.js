#!/usr/bin/env node
// SPDX-License-Identifier: LGPL-3.0-only
'use strict';

// punctim — the DCF medium tool (Node.js; stdlib only: fs, dgram, child_process).
//
// Reads DeModFrames from any medium and emits them on any other, byte-deterministically;
// encodes/decodes single frames; certifies the medium codecs against the golden vectors.
// The same CLI exists in Python, C, Rust and Go (Documentation/DCF_MEDIUM_SPEC.md):
//
//   punctim version [--json]
//   punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
//                   [--no-validate] [--stats] [--queue N]
//   punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
//   punctim decode  (HEX | --stdin) [--json]
//   punctim certify [--vectors DIR] [--family NAME ...]
//
// Media in this build: file: stdio: hex: udp:dialect=proto|bare hydra: (frame_tx/frame_rx
// subprocess PHY). loop: l2eth: afsk:/audio: sdr: janus: parse but exit 3 (unsupported).
// Exit codes: 0 ok · 1 I/O error · 2 usage · 3 medium unsupported · 4 certification failed
// · 5 invalid frame · 6 --expect not met.
//
// Determinism rule (normative): for finite inputs (stdio, hex/file without follow),
// `punctim io` produces byte-identical output to every other language's punctim for
// identical input and URI; udp:dialect=proto needs ts=0 (the default).

const fs = require('fs');
const path = require('path');
const dgram = require('dgram');
const cp = require('child_process');

const wire = require('../src/frame.js');
const sp = require('../src/superpack.js');
const M = require('../src/medium.js');

const EXIT = Object.freeze({ OK: 0, IO: 1, USAGE: 2, UNSUPPORTED: 3, CERT: 4, INVALID: 5, EXPECT: 6 });
const IMPL = 'node';
const MEDIA = ['file', 'stdio', 'hex', 'udp', 'hydra'];
const FRAME_TYPES = { 0: 'FData', 1: 'FAck', 2: 'FBeacon', 3: 'FCtrl' };

class UsageError extends Error {}
class Unsupported extends Error {}
class IoError extends Error {}

// ── small output helpers (synchronous, EPIPE-aware) ─────────────────────────────────────
const SLEEPER = new Int32Array(new SharedArrayBuffer(4));
function sleepSync(ms) {
  Atomics.wait(SLEEPER, 0, 0, ms);
}

function writeAllSync(fd, data) {
  const buf = Buffer.isBuffer(data) ? data : Buffer.from(String(data), 'utf8');
  let off = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(fd, buf, off, buf.length - off);
    } catch (e) {
      if (e.code === 'EAGAIN') {
        sleepSync(1);
        continue;
      }
      throw e;
    }
  }
}

const out = (s) => writeAllSync(1, s);
const err = (s) => {
  try {
    writeAllSync(2, s);
  } catch (e) { /* stderr gone: nothing left to tell */ }
};

/** JSON exactly as Python's json.dumps(obj, separators=(",", ":")) (ensure_ascii). */
function pyJson(obj) {
  const s = JSON.stringify(obj, (k, v) => {
    if (typeof v === 'number' && k === 'seconds' && Number.isInteger(v)) return `@@FLOAT${v}@@`;
    return v;
  });
  return s.replace(/"@@FLOAT(-?\d+)@@"/g, '$1.0')
    .replace(/[\u0080-\uffff]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
}

const tick = () => new Promise((res) => setImmediate(res));

// ── the URI grammar (mirrors python/dcf/medium.py:parse_uri) ────────────────────────────
const SCHEMES = {
  file: ['path', 'mode', 'append', 'follow', 'in', 'out'],
  stdio: [],
  hex: ['path', 'mode', 'append', 'follow'],
  udp: ['dialect', 'bind', 'peer', 'pair', 'flush_ms', 'ts', 'seq_start'],
  l2eth: ['if', 'ethertype', 'dst', 'mtu', 'impl', 'id', 'flush_ms'],
  loop: ['id'],
  hydra: ['in', 'out', 'profile', 'fec', 'interleave', 'base_freq', 'tone_spacing',
    'baud', 'n_tones', 'impl', 'tx', 'rx'],
  afsk: ['in', 'out', 'profile', 'fec'],
  audio: ['in', 'out', 'profile', 'fec'],
  sdr: ['in', 'out', 'mod'],
  janus: ['in', 'out', 'pset', 'fs', 'pset_file', 'tx', 'rx'],
  mc: ['rcon', 'pass_file', 'pass_env', 'fifo', 'log', 'bot', 'egress', 'ns', 'poll_hz'],
};
const COMMON_KEYS = ['name'];

/** `SCHEME[:k=v,...]` -> {scheme, kw}. Keys are validated per scheme (+ name); values are
 *  kept verbatim; a later duplicate wins; `|` inside a value separates multiple values. */
function parseUri(spec) {
  if (typeof spec !== 'string' || !spec.trim()) throw new UsageError('empty medium URI');
  const i = spec.indexOf(':');
  const scheme = (i < 0 ? spec : spec.slice(0, i)).trim().toLowerCase();
  const rest = i < 0 ? '' : spec.slice(i + 1);
  if (!Object.prototype.hasOwnProperty.call(SCHEMES, scheme)) {
    throw new UsageError(`unknown medium '${scheme}' (one of: ${Object.keys(SCHEMES).join(', ')})`);
  }
  const allowed = new Set([...SCHEMES[scheme], ...COMMON_KEYS]);
  const kw = {};
  for (const item of rest.split(',')) {
    if (!item) continue;
    const j = item.indexOf('=');
    if (j < 0) throw new UsageError(`${scheme}: bad item '${item}' (want key=value)`);
    const k = item.slice(0, j).trim();
    if (!allowed.has(k)) {
      throw new UsageError(`${scheme}: unknown key '${k}' (keys: ${[...allowed].sort().join(', ')})`);
    }
    kw[k] = item.slice(j + 1);
  }
  return { scheme, kw };
}

const multi = (v) => String(v || '').split('|').filter(Boolean);

function uBool(v, key) {
  const s = String(v).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(s)) return true;
  if (['0', 'false', 'no', 'off', ''].includes(s)) return false;
  throw new UsageError(`${key}='${v}': want 0|1`);
}

/** Python int(s, 10) / int(s[2:], 16) for a 0x prefix, as a BigInt (null if malformed). */
function parseIntStr(v) {
  const s = String(v).trim();
  if (/^0[xX][0-9a-fA-F]+$/.test(s)) return BigInt(s);
  if (/^[+-]?\d+$/.test(s)) return BigInt(s);
  return null;
}

function uInt(v, key) {
  const n = parseIntStr(v);
  if (n === null) throw new UsageError(`${key}='${v}': want an integer (decimal or 0x hex)`);
  return Number(n);
}

function uNum(v, key) {
  const s = String(v).trim();
  const n = Number(s);
  if (!s || Number.isNaN(n)) throw new UsageError(`${key}='${v}': want a number`);
  return n;
}

function uChoice(v, key, choices) {
  if (!choices.includes(v)) throw new UsageError(`${key}='${v}': want ${choices.join('|')}`);
  return v;
}

function hostPort(s, key) {
  const k = s.lastIndexOf(':');
  const host = k < 0 ? '' : s.slice(0, k);
  if (k < 0 || !host) throw new UsageError(`${key}='${s}': want host:port`);
  const port = uInt(s.slice(k + 1), key);
  if (port < 0 || port > 65535) throw new UsageError(`${key}='${s}': port out of range`);
  return [host, port];
}

// accepts host:port and the legacy id@host:port
const peersOf = (v) => multi(v).map((p) => {
  const hp = p.split('@').pop();
  const r = hostPort(hp, 'peer');
  if (r[1] === 0) throw new UsageError(`peer='${p}': port must be > 0`);
  return r;
});

/** True iff `punctim io --in spec` is a finite input (read to EOF, never drops). */
function finiteInput(spec) {
  const { scheme, kw } = parseUri(spec);
  if (scheme === 'stdio') return true;
  if (scheme === 'hex' || scheme === 'file') return !uBool(kw.follow === undefined ? '0' : kw.follow, 'follow');
  return false;
}

function unsupported(scheme) {
  const why = {
    loop: 'loop: is an in-process medium; the Node punctim has no second endpoint in this process',
    l2eth: 'l2eth: raw Ethernet needs AF_PACKET sockets, which Node stdlib does not provide',
    afsk: 'afsk: the AFSK WAV modem is Python-only (numpy); the bit stream is certified here',
    audio: 'audio: (= afsk:) the AFSK WAV modem is Python-only (numpy)',
    sdr: 'sdr: the IQ modem is Python-only (numpy)',
    janus: 'janus: the JANUS transport is Python-only (janus-c subprocess)',
    mc: "mc: a Minecraft world's register is Python-only (punctim mc)",
  }[scheme];
  return new Unsupported(why || `${scheme}: not available in the Node build`);
}

// ── HydraModem tools (frame_tx / frame_rx subprocess PHY; mirrors dcf.transport) ────────
function which(name) {
  for (const dir of String(process.env.PATH || '').split(path.delimiter)) {
    if (!dir) continue;
    const p = path.join(dir, name);
    try {
      fs.accessSync(p, fs.constants.X_OK);
      if (fs.statSync(p).isFile()) return p;
    } catch (e) { /* keep looking */ }
  }
  return null;
}

const HYDRA_CAPS = new Map();
/** The optional flags a frame_tx/frame_rx build understands (from its usage text). */
function hydraToolCaps(tool) {
  if (HYDRA_CAPS.has(tool)) return HYDRA_CAPS.get(tool);
  let usage = '';
  try {
    const r = cp.spawnSync(tool, [], { encoding: 'utf8', timeout: 10000 });
    usage = (r.stdout || '') + (r.stderr || '');
  } catch (e) { /* not runnable: no caps */ }
  const caps = new Set(['--profile', '--interleave', '--preamble'].filter((f) => usage.includes(f)));
  HYDRA_CAPS.set(tool, caps);
  return caps;
}

// hydra_profile_aux_cable as explicit tool flags when the tool has no --profile switch
const HYDRA_AUX_FLAGS = ['--base-freq', '1200', '--tone-spacing', '1200', '--baud', '1200'];

/** Resolve tools + the per-call flag list for a hydra: URI (Python HydraTransport). */
function hydraSetup(kw) {
  const fec = uChoice(kw.fec === undefined ? 'conv' : kw.fec, 'fec', ['none', 'rep3', 'conv']);
  const profile = uChoice(kw.profile === undefined ? 'default' : kw.profile, 'profile',
    ['default', 'aux', 'melody', 'chime', 'nocturne', 'bass', 'duet']);
  // The musical tone-table profiles (hydramodem/docs/MUSIC.md) pass to the tools as --profile NAME;
  // their pitches come from the tone table, so the linear tone-plan overrides are usage errors. The
  // duet (two frames per WAV) is Python-only.
  const music = ['melody', 'chime', 'nocturne', 'bass'].includes(profile);
  if (music && ['base_freq', 'tone_spacing', 'n_tones'].some((k) => kw[k] !== undefined)) {
    throw new UsageError('hydra: base_freq/tone_spacing/n_tones do not apply to a musical profile '
      + '(its pitches come from the tone table)');
  }
  const interleave = kw.interleave === undefined ? null : (uBool(kw.interleave, 'interleave') ? 1 : 0);
  const over = [];
  for (const [k, flag, conv] of [['base_freq', '--base-freq', uNum], ['tone_spacing', '--tone-spacing', uNum],
    ['baud', '--baud', uNum], ['n_tones', '--n-tones', uInt]]) {
    if (kw[k] !== undefined) over.push(flag, String(conv(kw[k], k)));
  }
  const impl = uChoice(kw.impl === undefined ? 'tool' : kw.impl, 'impl', ['tool', 'cffi']);
  if (impl === 'cffi') throw new Unsupported('hydra:impl=cffi (in-process libhydramodem) is Python-only; use impl=tool');
  if (profile === 'duet') throw new Unsupported('hydra:profile=duet (two frames per WAV) is Python-only');
  const tx = kw.tx || process.env.HYDRA_TX || which('frame_tx');
  const rx = kw.rx || process.env.HYDRA_RX || which('frame_rx');
  if (!tx || !rx) {
    throw new Unsupported('HydraModem tools not found: build hydramodem/dcf-tools (build.sh) and put '
      + 'frame_tx/frame_rx on PATH or in $HYDRA_TX/$HYDRA_RX (or pass tx=/rx=)');
  }
  const txc = hydraToolCaps(tx);
  const rxc = hydraToolCaps(rx);
  const caps = new Set([...txc].filter((c) => rxc.has(c)));
  const prof = [];
  if (music) {
    if (!caps.has('--profile')) {
      throw new Unsupported(`hydra: profile=${profile} needs frame_tx/frame_rx with --profile (rebuild hydramodem/dcf-tools)`);
    }
    prof.push('--profile', profile);
  }
  if (profile === 'aux') {
    if (caps.has('--profile')) prof.push('--profile', 'aux');
    else {
      prof.push(...HYDRA_AUX_FLAGS);
      if (caps.has('--preamble')) prof.push('--preamble', '16');
    }
  }
  if (interleave !== null && !interleave) {
    if (!caps.has('--interleave')) {
      throw new Unsupported('hydra: interleave=0 needs frame_tx/frame_rx with --interleave (rebuild hydramodem/dcf-tools)');
    }
    prof.push('--interleave', '0');
  }
  prof.push(...over);
  const name = kw.name === undefined ? 'hydra' : kw.name;
  for (const d of [kw.out, kw.in]) {
    if (d) fs.mkdirSync(d, { recursive: true });
  }
  return { tx, rx, fecFlag: '--' + fec, prof, name };
}

// ── readers ──────────────────────────────────────────────────────────────────────────────
// Finite readers are async iterables of frames; infinite readers have start(sink)/stop().
class StreamReader {
  constructor(fd, owned) {
    this.finite = true;
    this.fd = fd; // null = stdin
    this.owned = owned;
    this.sc = new M.StreamScanner();
    this.badLines = 0;
    this.invalid = 0;
    this.tailBytes = 0;
  }

  get skippedBytes() {
    return this.sc.skippedBytes;
  }

  async* [Symbol.asyncIterator]() {
    if (this.fd !== null) {
      const buf = Buffer.allocUnsafe(65536);
      for (;;) {
        const n = fs.readSync(this.fd, buf, 0, buf.length, null);
        if (!n) break;
        yield* this.sc.feed(buf.subarray(0, n));
        await tick();
      }
    } else {
      for await (const chunk of process.stdin) yield* this.sc.feed(chunk);
    }
    this.tailBytes = this.sc.flush().length;
  }

  interrupt() {
    if (this.fd === null) process.stdin.destroy();
  }

  close() {
    if (this.owned && this.fd !== null) fs.closeSync(this.fd);
  }
}

class HexReader {
  constructor(fd, owned) {
    this.finite = true;
    this.fd = fd; // null = stdin
    this.owned = owned;
    this.skippedBytes = 0;
    this.badLines = 0;
    this.invalid = 0;
  }

  * lines(text) {
    for (const line of text) {
      const r = M.hexLine(line);
      if (r === false) this.badLines += 1;
      else if (r !== null) yield r;
    }
  }

  async* chunks() {
    if (this.fd !== null) {
      const buf = Buffer.allocUnsafe(65536);
      for (;;) {
        const n = fs.readSync(this.fd, buf, 0, buf.length, null);
        if (!n) break;
        yield Buffer.from(buf.subarray(0, n));
        await tick();
      }
    } else {
      for await (const chunk of process.stdin) yield chunk;
    }
  }

  async* [Symbol.asyncIterator]() {
    let carry = '';
    for await (const chunk of this.chunks()) {
      const text = carry + chunk.toString('latin1');
      const parts = text.split('\n');
      carry = parts.pop();
      yield* this.lines(parts);
    }
    if (carry) yield* this.lines([carry]);
  }

  interrupt() {
    if (this.fd === null) process.stdin.destroy();
  }

  close() {
    if (this.owned && this.fd !== null) fs.closeSync(this.fd);
  }
}

// Infinite polling readers (file/hex follow, hydra dir). They can pause, so unlike UDP
// they apply backpressure: each step moves a bounded chunk and waits while the pipeline
// queue has no room (`room()`), instead of shedding frames that are safe on disk.
const IDLE = 0;
const MORE = 1;
const BLOCKED = 2;

class PollReader {
  constructor(periodMs) {
    this.finite = false;
    this.periodMs = periodMs;
    this.badLines = 0;
    this.invalid = 0;
    this.timer = null;
    this.imm = null;
    this.running = false;
    this.error = null;
  }

  get skippedBytes() {
    return 0;
  }

  async start(sink, room) {
    this.sink = sink;
    this.room = room || (() => true);
    this.running = true;
    this.run();
  }

  run() {
    this.timer = null;
    this.imm = null;
    if (!this.running) return;
    let r;
    try {
      r = this.room() ? this.step() : BLOCKED;
    } catch (e) {
      this.error = e;
      return;
    }
    if (!this.running) return;
    if (r === MORE) this.imm = setImmediate(() => this.run());
    else this.timer = setTimeout(() => this.run(), r === BLOCKED ? 2 : this.periodMs);
  }

  stop() {
    this.running = false;
    if (this.timer) clearTimeout(this.timer);
    if (this.imm) clearImmediate(this.imm);
    this.timer = null;
    this.imm = null;
  }

  close() {
    this.stop();
  }
}

class FileFollowReader extends PollReader {
  // .dcf spool tailed for growth (Python FileTransport follow=True, 200 ms poll).
  constructor(p, chunk = 17 * 128) {
    super(200);
    this.path = p;
    this.pos = 0;
    this.chunk = chunk;
    this.buf = Buffer.allocUnsafe(chunk);
    this.sc = new M.StreamScanner();
  }

  get skippedBytes() {
    return this.sc.skippedBytes;
  }

  /** Read up to one chunk from the current position (empty if none / file missing). */
  readChunk() {
    let fd;
    try {
      fd = fs.openSync(this.path, 'r');
    } catch (e) {
      if (e.code === 'ENOENT') return this.buf.subarray(0, 0);
      throw e;
    }
    try {
      const n = fs.readSync(fd, this.buf, 0, this.chunk, this.pos);
      this.pos += n;
      return this.buf.subarray(0, n);
    } finally {
      fs.closeSync(fd);
    }
  }

  step() {
    const data = this.readChunk();
    if (!data.length) return IDLE;
    for (const f of this.sc.feed(data)) this.sink(f);
    return data.length === this.chunk ? MORE : IDLE;
  }
}

class HexFollowReader extends FileFollowReader {
  // hex lines tailed for growth (Python HexTransport follow=True); a partial last line is
  // carried until its newline arrives. Without a path: stdin lines until EOF.
  constructor(p) {
    super(p, 35 * 128);
    this.carry = '';
    this.backlog = [];
  }

  get skippedBytes() {
    return 0;
  }

  lines(list) {
    for (const line of list) {
      const r = M.hexLine(line);
      if (r === false) this.badLines += 1;
      else if (r !== null) this.sink(r);
    }
  }

  step() {
    const data = this.readChunk();
    if (!data.length) return IDLE;
    const text = this.carry + data.toString('latin1');
    const cut = text.lastIndexOf('\n') + 1;
    if (cut) this.lines(text.slice(0, cut - 1).split('\n'));
    this.carry = text.slice(cut);
    return data.length === this.chunk ? MORE : IDLE;
  }

  async start(sink, room) {
    if (this.path) return super.start(sink, room);
    this.sink = sink;
    this.room = room || (() => true);
    this.running = true;
    const drain = () => {
      this.timer = null;
      if (!this.running) return;
      while (this.backlog.length && this.room()) this.lines([this.backlog.shift()]);
      if (this.backlog.length) {
        process.stdin.pause();
        this.timer = setTimeout(drain, 2);
      } else {
        process.stdin.resume();
      }
    };
    process.stdin.on('data', (chunk) => {
      const parts = (this.carry + chunk.toString('latin1')).split('\n');
      this.carry = parts.pop();
      this.backlog.push(...parts);
      drain();
    });
    process.stdin.on('end', () => {
      if (this.carry) this.backlog.push(this.carry);
      this.carry = '';
      drain();
    });
    return undefined;
  }

  stop() {
    super.stop();
    if (!this.path) process.stdin.destroy();
  }
}

class HydraReader extends PollReader {
  // HydraModem WAV spool (Python _DirMedium._tail): every 100 ms list unseen *.wav not
  // starting with '.', sorted; decode each once with frame_rx (one file per step).
  constructor(kw) {
    super(100);
    this.h = hydraSetup(kw);
    this.dir = kw.in;
    this.seen = new Set();
    this.todo = [];
  }

  decodeFile(p) {
    let r;
    try {
      r = cp.spawnSync(this.h.rx, [p, this.h.fecFlag, ...this.h.prof], { encoding: 'utf8', timeout: 60000 });
    } catch (e) {
      return null;
    }
    if (r.error || r.status !== 0) return null;
    const h = String(r.stdout || '').trim();
    if (!/^[0-9a-fA-F]*$/.test(h) || h.length % 2) return null;
    const b = Buffer.from(h, 'hex');
    return b.length === M.FRAME_LEN ? b : null;
  }

  step() {
    if (!this.todo.length) {
      let files;
      try {
        files = fs.readdirSync(this.dir).filter((f) => f.endsWith('.wav') && !f.startsWith('.')).sort();
      } catch (e) {
        files = [];
      }
      this.todo = files.filter((f) => !this.seen.has(f));
      if (!this.todo.length) return IDLE;
    }
    const f = this.todo.shift();
    this.seen.add(f);
    const fr = this.decodeFile(path.join(this.dir, f));
    if (fr) this.sink(fr);
    return this.todo.length ? MORE : IDLE;
  }
}

class UdpReader {
  constructor(kw) {
    this.finite = false;
    this.bind = hostPort(kw.bind, 'bind');
    this.dialect = uChoice(kw.dialect === undefined ? 'proto' : kw.dialect, 'dialect', ['proto', 'bare']);
    // validate the writer-side keys too (as the Python factory does)
    peersOf(kw.peer || '');
    uBool(kw.pair === undefined ? '1' : kw.pair, 'pair');
    uInt(kw.flush_ms === undefined ? '20' : kw.flush_ms, 'flush_ms');
    uChoice(kw.ts === undefined ? '0' : kw.ts, 'ts', ['0', 'now']);
    uInt(kw.seq_start === undefined ? '1' : kw.seq_start, 'seq_start');
    this.skippedBytes = 0;
    this.badLines = 0;
    this.invalid = 0;
    this.sock = null;
  }

  start(sink) {
    return new Promise((resolve, reject) => {
      const sock = dgram.createSocket({ type: 'udp4', reuseAddr: true });
      this.sock = sock;
      sock.once('error', (e) => reject(new IoError(`udp bind ${this.bind.join(':')}: ${e.message}`)));
      sock.on('message', (msg) => {
        if (this.dialect === 'proto') {
          let m;
          try {
            m = M.protoDecode(msg);
          } catch (e) {
            this.invalid += 1;
            return;
          }
          if (m.type !== M.MSG_FRAME) return; // an adapter envelope, not a frame here
          if (m.payload.length !== M.FRAME_LEN) {
            this.invalid += 1;
            return;
          }
          sink(m.payload);
        } else {
          const frames = M.bareDecode(msg);
          if (!frames.length) {
            this.invalid += 1;
            return;
          }
          for (const f of frames) sink(f);
        }
      });
      sock.bind({ address: this.bind[0], port: this.bind[1] }, () => {
        sock.removeAllListeners('error');
        sock.on('error', () => {});
        try {
          sock.setRecvBufferSize(1 << 20); // absorb bursts (the kernel may cap it)
        } catch (e) { /* keep the default */ }
        resolve();
      });
    });
  }

  stop() {
    if (this.sock) {
      try {
        this.sock.close();
      } catch (e) { /* already closed */ }
      this.sock = null;
    }
  }

  close() {
    this.stop();
  }
}

function openReader(spec) {
  const { scheme, kw } = parseUri(spec);
  if (scheme === 'stdio') return new StreamReader(null, false);
  if ((scheme === 'file' || scheme === 'hex') && finiteInput(spec)) {
    const p = kw.path || (scheme === 'file' ? kw.in : undefined);
    if (scheme === 'file' && !p) throw new UsageError('file: needs path=');
    const fd = p ? fs.openSync(p, 'r') : null;
    return scheme === 'file' ? new StreamReader(fd, !!p) : new HexReader(fd, !!p);
  }
  if (scheme === 'file' || scheme === 'hex') {
    if (kw.mode !== undefined) uChoice(kw.mode, 'mode', ['r', 'w', 'rw']);
    uBool(kw.append === undefined ? '0' : kw.append, 'append');
    const p = kw.path || (scheme === 'file' ? kw.in : undefined);
    if (scheme === 'file') {
      if (!p) throw new UsageError('file: needs path= (or in=/out=)');
      return new FileFollowReader(p);
    }
    return new HexFollowReader(p || null);
  }
  if (scheme === 'udp') {
    if (kw.bind === undefined) throw new UsageError('udp as input needs bind=host:port');
    return new UdpReader(kw);
  }
  if (['hydra', 'afsk', 'audio', 'sdr', 'janus'].includes(scheme) && !kw.in) {
    throw new UsageError(`${scheme} as input needs in=<dir>`);
  }
  if (scheme === 'hydra') return new HydraReader(kw);
  throw unsupported(scheme);
}

// ── writers ──────────────────────────────────────────────────────────────────────────────
class BufferedWriter {
  // file / stdio (raw 17-byte frames) and hex (34 hex + LF) — buffered, synchronous.
  constructor(fd, owned, hex) {
    this.fd = fd;
    this.owned = owned;
    this.hex = hex;
    this.parts = [];
    this.size = 0;
  }

  write(frame) {
    const b = this.hex ? Buffer.from(Buffer.from(frame).toString('hex') + '\n', 'ascii') : Buffer.from(frame);
    this.parts.push(b);
    this.size += b.length;
    if (this.size >= 65536) this.flush();
  }

  poll() {
    this.flush();
  }

  flush() {
    if (!this.size) return;
    const b = Buffer.concat(this.parts, this.size);
    this.parts = [];
    this.size = 0;
    writeAllSync(this.fd, b);
  }

  async close() {
    try {
      this.flush();
    } finally {
      if (this.owned) fs.closeSync(this.fd);
    }
  }
}

class UdpWriter {
  constructor(kw) {
    this.bind = hostPort(kw.bind === undefined ? '0.0.0.0:0' : kw.bind, 'bind');
    this.peers = peersOf(kw.peer || '');
    this.dialect = uChoice(kw.dialect === undefined ? 'proto' : kw.dialect, 'dialect', ['proto', 'bare']);
    this.pair = uBool(kw.pair === undefined ? '1' : kw.pair, 'pair');
    this.flushMs = Math.max(0, uInt(kw.flush_ms === undefined ? '20' : kw.flush_ms, 'flush_ms'));
    this.tsNow = uChoice(kw.ts === undefined ? '0' : kw.ts, 'ts', ['0', 'now']) === 'now';
    const s0 = parseIntStr(kw.seq_start === undefined ? '1' : kw.seq_start);
    if (s0 === null) throw new UsageError(`seq_start='${kw.seq_start}': want an integer (decimal or 0x hex)`);
    this.seq = Number(BigInt.asUintN(32, s0));
    this.pending = null; // bare: [frame, t_ms]
    this.inflight = 0;
    this.error = null;
    this.waiters = [];
    this.sock = null;
  }

  open() {
    return new Promise((resolve, reject) => {
      const sock = dgram.createSocket({ type: 'udp4', reuseAddr: true });
      this.sock = sock;
      sock.once('error', (e) => reject(new IoError(`udp bind ${this.bind.join(':')}: ${e.message}`)));
      sock.bind({ address: this.bind[0], port: this.bind[1] }, () => {
        sock.removeAllListeners('error');
        sock.on('error', (e) => { this.error = this.error || e; });
        resolve(this);
      });
    });
  }

  sendDatagram(dg) {
    for (const [host, port] of this.peers) {
      this.inflight += 1;
      this.sock.send(dg, port, host, (e) => {
        if (e && !this.error) this.error = e;
        this.inflight -= 1;
        if (this.inflight < 256) {
          const w = this.waiters;
          this.waiters = [];
          for (const r of w) r();
        }
      });
    }
  }

  async write(frame) {
    if (this.dialect === 'proto') {
      const seq = this.seq;
      this.seq = (this.seq + 1) >>> 0;
      const ts = this.tsNow ? BigInt(Date.now()) * 1000n : 0n;
      this.sendDatagram(M.protoFrameEncode(frame, seq, ts));
    } else if (!this.pair || !M.gate(frame)) {
      this.flushPending(); // keep order: a held frame goes first
      this.sendDatagram(Buffer.from(frame));
    } else if (this.pending === null) {
      this.pending = [Buffer.from(frame), Date.now()];
    } else {
      const held = this.pending[0];
      this.pending = null;
      this.sendDatagram(sp.pack(held, frame));
    }
    if (this.inflight >= 1024) await new Promise((r) => this.waiters.push(r));
    if (this.error) throw new IoError(`udp send: ${this.error.message}`);
  }

  flushPending() {
    if (this.pending !== null) {
      const held = this.pending[0];
      this.pending = null;
      this.sendDatagram(held);
    }
  }

  poll() {
    if (this.pending !== null && Date.now() - this.pending[1] >= this.flushMs) this.flushPending();
  }

  async close() {
    this.flushPending();
    while (this.inflight > 0) {
      await new Promise((r) => {
        this.waiters.push(r);
        setTimeout(r, 5);
      });
    }
    const e = this.error;
    try {
      this.sock.close();
    } catch (x) { /* already closed */ }
    if (e) throw new IoError(`udp send: ${e.message}`);
  }
}

class HydraWriter {
  constructor(kw) {
    this.h = hydraSetup(kw);
    this.dir = kw.out;
    this.n = 0;
  }

  write(frame) {
    this.n += 1;
    const tmp = path.join(this.dir, `.${this.h.name}-${this.n}.wav.tmp`);
    const fin = path.join(this.dir, `${this.h.name}-${String(this.n).padStart(8, '0')}.wav`);
    const r = cp.spawnSync(this.h.tx, [Buffer.from(frame).toString('hex'), tmp, this.h.fecFlag, ...this.h.prof],
      { stdio: 'ignore', timeout: 60000 });
    if (r.error) throw new IoError(`frame_tx: ${r.error.message}`);
    if (r.status !== 0) throw new IoError(`frame_tx exited ${r.status === null ? r.signal : r.status}`);
    fs.renameSync(tmp, fin); // atomic publish so a reader never sees a partial WAV
  }

  poll() {}

  async close() {}
}

async function openWriter(spec) {
  const { scheme, kw } = parseUri(spec);
  if (scheme === 'stdio') return new BufferedWriter(1, false, false);
  if (scheme === 'file' || scheme === 'hex') {
    const p = kw.path || (scheme === 'file' ? kw.out : undefined);
    if (scheme === 'file' && !p) throw new UsageError('file: needs path=');
    const append = uBool(kw.append === undefined ? '0' : kw.append, 'append');
    const fd = p ? fs.openSync(p, append ? 'a' : 'w') : 1;
    return new BufferedWriter(fd, !!p, scheme === 'hex');
  }
  if (scheme === 'udp' && !kw.peer) throw new UsageError('udp as output needs peer=host:port');
  if (['hydra', 'afsk', 'audio', 'sdr', 'janus'].includes(scheme) && !kw.out) {
    throw new UsageError(`${scheme} as output needs out=<dir>`);
  }
  if (scheme === 'udp') return new UdpWriter(kw).open();
  if (scheme === 'hydra') return new HydraWriter(kw);
  throw unsupported(scheme);
}

// ── the pipeline: reader -> frame gate -> writer (single-threaded, ordered) ─────────────
class BoundedQueue {
  constructor(maxlen) {
    this.maxlen = maxlen;
    this.items = [];
    this.head = 0;
    this.dropped = 0;
    this.wake = null; // resolves the pump's idle wait on the next put()
  }

  /** Resolve after the next put() or `ms`, whichever comes first. */
  idle(ms) {
    return new Promise((res) => {
      const t = setTimeout(() => {
        this.wake = null;
        res();
      }, ms);
      this.wake = () => {
        clearTimeout(t);
        res();
      };
    });
  }

  get length() {
    return this.items.length - this.head;
  }

  put(x) {
    this.items.push(x);
    if (this.wake) {
      const w = this.wake;
      this.wake = null;
      w();
    }
    while (this.length > this.maxlen) {
      this.head += 1; // shed the oldest
      this.dropped += 1;
    }
    if (this.head > 4096 && this.head * 2 > this.items.length) {
      this.items = this.items.slice(this.head);
      this.head = 0;
    }
  }

  get() {
    if (this.head >= this.items.length) return undefined;
    const x = this.items[this.head];
    this.items[this.head] = undefined;
    this.head += 1;
    return x;
  }
}

const STOP = { flag: false, reader: null };

async function runIo(inSpec, outSpec, { count = null, seconds = null, expect = null, validate = true, queue = 256 } = {}) {
  const t0 = process.hrtime.bigint();
  const elapsed = () => Number(process.hrtime.bigint() - t0) / 1e9;
  const reader = openReader(inSpec);
  let writer;
  try {
    writer = await openWriter(outSpec);
  } catch (e) {
    reader.close();
    throw e;
  }
  STOP.reader = reader;
  const st = { frames_in: 0, frames_out: 0, invalid_frames: 0, dropped: 0 };
  let limit = count;
  if (limit === null && expect !== null && !reader.finite) limit = expect;
  const late = () => seconds !== null && elapsed() >= seconds;
  const done = () => limit !== null && st.frames_out >= limit;

  const handle = async (frame) => {
    st.frames_in += 1;
    if (validate && !M.gate(frame)) {
      st.invalid_frames += 1;
      return;
    }
    await writer.write(frame);
    st.frames_out += 1;
  };

  try {
    if (reader.finite) {
      if (!done()) {
        try {
          for await (const fr of reader) {
            await handle(fr);
            if (done() || late() || STOP.flag) break;
          }
        } catch (e) {
          if (!STOP.flag) throw e; // an interrupted stdin read ends the run cleanly
        }
      }
    } else {
      const q = new BoundedQueue(Math.max(1, queue));
      const qmax = Math.max(1, queue);
      await reader.start((fr) => q.put(fr), () => q.length < qmax);
      try {
        while (!done()) {
          if (reader.error) throw reader.error;
          const item = q.get();
          if (item === undefined) {
            writer.poll();
            if (late() || STOP.flag) break;
            await q.idle(2);
            continue;
          }
          await handle(item);
        }
      } finally {
        reader.stop();
      }
      while (!done()) { // frames already received are not dropped
        const item = q.get();
        if (item === undefined) break;
        await handle(item);
      }
      st.dropped = q.dropped;
    }
  } finally {
    STOP.reader = null;
    try {
      await writer.close();
    } finally {
      reader.close();
    }
  }
  return {
    in: inSpec,
    out: outSpec,
    frames_in: st.frames_in,
    frames_out: st.frames_out,
    invalid_frames: st.invalid_frames + reader.invalid,
    skipped_bytes: reader.skippedBytes,
    bad_lines: reader.badLines,
    dropped: st.dropped,
    seconds: Math.round(elapsed() * 1000) / 1000,
  };
}

// ── argv parsing (argparse-compatible enough: --k v, --k=v, any order) ──────────────────
function parseOpts(argv, spec, cmd) {
  // spec: { '--name': {key, flag?, nargs?: '+'} , _pos: [key...] }
  const a = {};
  const pos = [];
  for (let i = 0; i < argv.length; i++) {
    let tok = argv[i];
    if (tok === '-h' || tok === '--help') {
      a.help = true;
      continue;
    }
    if (tok.startsWith('--') && tok.length > 2) {
      let val;
      const eq = tok.indexOf('=');
      if (eq >= 0) {
        val = tok.slice(eq + 1);
        tok = tok.slice(0, eq);
      }
      const o = spec[tok];
      if (!o) throw new UsageError(`${cmd}: unrecognized argument ${argv[i]}`);
      if (o.flag) {
        if (val !== undefined) throw new UsageError(`${cmd}: ${tok} takes no value`);
        a[o.key] = true;
        continue;
      }
      if (o.nargs === '+') {
        const vals = val !== undefined ? [val] : [];
        while (i + 1 < argv.length && !(argv[i + 1].startsWith('-') && argv[i + 1].length > 1 && !/^-\d/.test(argv[i + 1]))) vals.push(argv[++i]);
        if (!vals.length) throw new UsageError(`${cmd}: ${tok} expects at least one argument`);
        a[o.key] = (a[o.key] || []).concat(vals);
        continue;
      }
      if (val === undefined) {
        if (i + 1 >= argv.length) throw new UsageError(`${cmd}: ${tok} expects one argument`);
        val = argv[++i];
      }
      a[o.key] = val;
      continue;
    }
    pos.push(tok);
  }
  const want = spec._pos || [];
  if (pos.length > want.length) throw new UsageError(`${cmd}: unrecognized arguments: ${pos.slice(want.length).join(' ')}`);
  want.forEach((k, i) => { if (i < pos.length) a[k] = pos[i]; });
  return a;
}

/** argparse `int(s, 0)`: decimal (no leading zeros), 0x / 0o / 0b prefixes. */
function int0(s, what, min) {
  const t = String(s).trim();
  let v = null;
  if (/^[+-]?(0|[1-9]\d*)$/.test(t)) v = Number(t);
  else if (/^[+-]?0[xX][0-9a-fA-F]+$/.test(t) || /^[+-]?0[oO][0-7]+$/.test(t) || /^[+-]?0[bB][01]+$/.test(t)) {
    const neg = t[0] === '-';
    v = Number(t.replace(/^[+-]/, ''));
    if (neg) v = -v;
  }
  if (v === null || !Number.isFinite(v)) throw new UsageError(`${what}: invalid int value: '${s}'`);
  if (v < min) throw new UsageError(`${what}: must be >= ${min}`);
  return v;
}

const USAGE = `usage: punctim {version,io,encode,decode,certify} ...

DCF medium tool (Node.js): move DeModFrames between any two media, deterministically
(Documentation/DCF_MEDIUM_SPEC.md).

  punctim version [--json]
  punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
                  [--no-validate] [--stats] [--queue N]
  punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
  punctim decode  (HEX | --stdin) [--json]
  punctim certify [--vectors DIR] [--family NAME ...]

media: file: stdio: hex: udp:dialect=proto|bare hydra: (loop: l2eth: afsk: audio: sdr:
       janus: exit 3 in this build)
exit: 0 ok, 1 I/O, 2 usage, 3 medium unsupported, 4 cert failed, 5 invalid frame,
      6 --expect not met
`;

function version() {
  try {
    return require('../package.json').version;
  } catch (e) {
    return '0';
  }
}

// ── commands ─────────────────────────────────────────────────────────────────────────────
function cmdVersion(argv) {
  const a = parseOpts(argv, { '--json': { key: 'json', flag: true } }, 'version');
  if (a.help) return out('usage: punctim version [--json]\n'), EXIT.OK;
  const v = version();
  if (a.json) out(pyJson({ name: 'punctim', version: v, impl: IMPL, media: MEDIA, families: [...M.FAMILIES] }) + '\n');
  else out(`punctim ${v} (${IMPL})\n`);
  return EXIT.OK;
}

async function cmdIo(argv) {
  const a = parseOpts(argv, {
    '--in': { key: 'inp' }, '--out': { key: 'out' }, '--count': { key: 'count' },
    '--seconds': { key: 'seconds' }, '--expect': { key: 'expect' },
    '--no-validate': { key: 'noValidate', flag: true }, '--stats': { key: 'stats', flag: true },
    '--queue': { key: 'queue' },
  }, 'io');
  if (a.help) {
    out('usage: punctim io --in URI --out URI [--count N] [--seconds S] [--expect N] [--no-validate] [--stats] [--queue N]\n');
    return EXIT.OK;
  }
  if (a.inp === undefined || a.out === undefined) throw new UsageError('io: the following arguments are required: --in, --out');
  const count = a.count === undefined ? null : int0(a.count, '--count', 0);
  const expect = a.expect === undefined ? null : int0(a.expect, '--expect', 0);
  const queue = a.queue === undefined ? 256 : int0(a.queue, '--queue', 1);
  let seconds = null;
  if (a.seconds !== undefined) {
    seconds = Number(String(a.seconds).trim());
    if (!String(a.seconds).trim() || Number.isNaN(seconds)) throw new UsageError(`--seconds: invalid float value: '${a.seconds}'`);
  }
  let st;
  try {
    st = await runIo(a.inp, a.out, { count, seconds, expect, validate: !a.noValidate, queue });
  } catch (e) {
    if (e instanceof UsageError) {
      err(`punctim io: ${e.message}\n`);
      return EXIT.USAGE;
    }
    if (e instanceof Unsupported) {
      err(`punctim io: medium unsupported: ${e.message}\n`);
      return EXIT.UNSUPPORTED;
    }
    if (e && e.code === 'EPIPE') return EXIT.IO;
    if (e instanceof IoError || (e && typeof e.code === 'string')) {
      err(`punctim io: I/O error: ${e.message}\n`);
      return EXIT.IO;
    }
    throw e;
  }
  if (a.stats) err(pyJson(st) + '\n');
  if (expect !== null && st.frames_out !== expect) {
    err(`punctim io: expected ${expect} frames, wrote ${st.frames_out}\n`);
    return EXIT.EXPECT;
  }
  return EXIT.OK;
}

function numArg(s, what, lo, hi) {
  const v = parseIntStr(s);
  if (v === null) throw new UsageError(`${what}: '${s}' is not an integer (decimal or 0x hex)`);
  if (v < BigInt(lo) || v > BigInt(hi)) throw new UsageError(`${what}: ${v} out of range ${lo}..${hi}`);
  return v;
}

function cmdEncode(argv) {
  const a = parseOpts(argv, {
    '--type': { key: 'type' }, '--seq': { key: 'seq' }, '--src': { key: 'src' }, '--dst': { key: 'dst' },
    '--payload': { key: 'payload' }, '--text': { key: 'text' }, '--ts': { key: 'ts' },
  }, 'encode');
  if (a.help) {
    out('usage: punctim encode --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]\n');
    return EXIT.OK;
  }
  const missing = ['type', 'seq', 'src', 'dst'].filter((k) => a[k] === undefined).map((k) => '--' + k);
  if (missing.length) throw new UsageError(`encode: the following arguments are required: ${missing.join(', ')}`);
  if ((a.payload === undefined) === (a.text === undefined)) {
    throw new UsageError('encode: one of the arguments --payload --text is required (and they are mutually exclusive)');
  }
  const type = Number(numArg(a.type, '--type', 0, 15));
  const seq = Number(numArg(a.seq, '--seq', 0, 0xFFFF));
  const src = Number(numArg(a.src, '--src', 0, 0xFFFF));
  const dst = Number(numArg(a.dst, '--dst', 0, 0xFFFF));
  const ts = Number(numArg(a.ts === undefined ? '0' : a.ts, '--ts', 0, (1n << 64n) - 1n) & 0xFFFFFFn); // 24-bit on the wire
  let payload;
  if (a.payload !== undefined) {
    const p = String(a.payload).trim();
    if (!/^[0-9a-fA-F]{8}$/.test(p)) throw new UsageError('--payload wants exactly 8 hex digits (4 bytes)');
    payload = Buffer.from(p, 'hex');
  } else {
    const b = Buffer.from(String(a.text), 'utf8');
    if (b.length > 4) throw new UsageError('--text is at most 4 UTF-8 bytes (zero-padded)');
    payload = Buffer.concat([b, Buffer.alloc(4 - b.length)]);
  }
  out(wire.encode({ version: 1, type, seq, src, dst, payload, tsUs: ts }).toString('hex') + '\n');
  return EXIT.OK;
}

/** wirelab_core.decode() fields + hex/valid/syndrome (+ error when invalid), in the Python
 *  key order; fields are read raw from a 17-byte word even when the gate fails. */
function decodeRecord(text) {
  const s = String(text).replace(/^[ \t\r\v\f]+|[ \t\r\v\f]+$/g, '');
  if (s.length % 2 || !/^[0-9a-fA-F]*$/.test(s)) return { hex: s, valid: false, error: 'not hex' };
  const b = Buffer.from(s, 'hex');
  if (b.length !== wire.FRAME_SIZE) return { hex: b.toString('hex'), valid: false, error: `length ${b.length} != 17` };
  const t = b[1] & 0x0F;
  const rec = {
    hex: b.toString('hex'), valid: true, syndrome: wire.syndrome(b),
    frame_type: t, frame_type_name: FRAME_TYPES[t] || `0x${t.toString(16).toUpperCase()}`,
    seq: b.readUInt16BE(2), src: b.readUInt16BE(4), dst: b.readUInt16BE(6),
    payload: b.subarray(8, 12).toString('hex'),
    ts_us: (b[12] << 16) | (b[13] << 8) | b[14], crc: b.readUInt16BE(15),
  };
  let why = null;
  if (b[0] !== wire.SYNC) why = 'bad sync byte';
  else if (b[1] >>> 4 !== wire.VERSION) why = 'bad version nibble';
  else if (rec.syndrome !== 0) why = 'CRC mismatch';
  if (why) {
    rec.valid = false;
    rec.error = why;
  }
  return rec;
}

const hex4 = (n) => '0x' + n.toString(16).padStart(4, '0');
function human(rec) {
  if (rec.syndrome === undefined) return `invalid (${rec.error}) ${rec.hex}`;
  const head = rec.valid ? 'valid' : `invalid (${rec.error})`;
  return `${head} type=${rec.frame_type} (${rec.frame_type_name}) seq=${rec.seq} src=${rec.src} dst=${rec.dst} `
    + `payload=${rec.payload} ts_us=${rec.ts_us} crc=${hex4(rec.crc)} syndrome=${hex4(rec.syndrome)}`;
}

function cmdDecode(argv) {
  const a = parseOpts(argv, { '--stdin': { key: 'stdin', flag: true }, '--json': { key: 'json', flag: true }, _pos: ['hex'] }, 'decode');
  if (a.help) {
    out('usage: punctim decode (HEX | --stdin) [--json]\n');
    return EXIT.OK;
  }
  if (!!a.stdin === (a.hex !== undefined)) throw new UsageError('decode wants exactly one of HEX or --stdin');
  let items;
  if (a.stdin) {
    items = [];
    const text = fs.readFileSync(0).toString('latin1');
    const lines = text.split('\n');
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    for (const line of lines) {
      const s = line.replace(/^[ \t\r\v\f\n]+|[ \t\r\v\f\n]+$/g, '');
      if (s && s[0] !== '#') items.push(s);
    }
  } else {
    items = [a.hex];
  }
  let rc = EXIT.OK;
  for (const it of items) {
    const rec = decodeRecord(it);
    if (!rec.valid) rc = EXIT.INVALID;
    out((a.json ? pyJson(rec) : human(rec)) + '\n');
  }
  return rc;
}

/** Locate medium_vectors.json: an explicit dir/file, then $PUNCTIM_VECTORS, then
 *  Documentation/ found walking up from this file, then the python/MCP copy. */
function findVectors(explicit) {
  const cands = [];
  const asFile = (b) => (b.endsWith('.json') ? b : path.join(b, 'medium_vectors.json'));
  if (explicit) cands.push(asFile(explicit));
  else {
    if (process.env.PUNCTIM_VECTORS) cands.push(asFile(process.env.PUNCTIM_VECTORS));
    let d = __dirname;
    for (let i = 0; i < 6; i++) {
      cands.push(path.join(d, 'Documentation', 'medium_vectors.json'));
      d = path.dirname(d);
    }
    d = __dirname;
    for (let i = 0; i < 6; i++) {
      cands.push(path.join(d, 'python', 'MCP', 'medium_vectors.json'));
      d = path.dirname(d);
    }
  }
  for (const c of cands) {
    try {
      if (fs.statSync(c).isFile()) return path.normalize(c);
    } catch (e) { /* next */ }
  }
  throw new IoError('medium_vectors.json not found (use --vectors DIR or $PUNCTIM_VECTORS)');
}

function cmdCertify(argv) {
  const a = parseOpts(argv, { '--vectors': { key: 'vectors' }, '--family': { key: 'family', nargs: '+' } }, 'certify');
  if (a.help) {
    out('usage: punctim certify [--vectors DIR] [--family NAME ...]\n');
    return EXIT.OK;
  }
  let p;
  let vec;
  try {
    p = findVectors(a.vectors);
    vec = JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch (e) {
    err(`punctim certify: ${e.message}\n`);
    return EXIT.IO;
  }
  let results;
  try {
    results = M.certifyVectors(vec, a.family);
  } catch (e) {
    if (e.code === 'EUSAGE') {
      err(`punctim certify: ${e.message}\n`);
      return EXIT.USAGE;
    }
    throw e;
  }
  out(`vectors: ${p}\n`);
  let total = 0;
  let failed = 0;
  for (const r of results) {
    if (r.family !== 'anchors') total += r.n;
    if (r.ok) out(`PASS ${r.family} (${r.n} ${r.family === 'anchors' ? 'basis frames' : 'cases'})\n`);
    else {
      failed += 1;
      out(`FAIL ${r.family}: ${r.msg}\n`);
    }
  }
  if (failed) {
    out(`CERTIFICATION FAILED (${failed} famil${failed === 1 ? 'y' : 'ies'})\n`);
    return EXIT.CERT;
  }
  out(`ALL MEDIUM VECTORS PASS (${total} cases, impl ${IMPL})\n`);
  return EXIT.OK;
}

// ── main ─────────────────────────────────────────────────────────────────────────────────
async function main(argv) {
  const [cmd, ...rest] = argv;
  if (cmd === '-h' || cmd === '--help') {
    out(USAGE);
    return EXIT.OK;
  }
  const cmds = { version: cmdVersion, io: cmdIo, encode: cmdEncode, decode: cmdDecode, certify: cmdCertify };
  if (!cmd || !cmds[cmd]) {
    err(USAGE);
    if (cmd === 'sim') err('punctim: sim is Python-only (python3 python/punctim.py sim)\n');
    else if (cmd) err(`punctim: invalid choice: '${cmd}'\n`);
    return EXIT.USAGE;
  }
  try {
    return await cmds[cmd](rest);
  } catch (e) {
    if (e instanceof UsageError) {
      err(`punctim ${cmd}: ${e.message}\n`);
      return EXIT.USAGE;
    }
    if (e && e.code === 'EPIPE') return EXIT.IO;
    if (e instanceof IoError || (e && typeof e.code === 'string' && e.syscall)) {
      err(`punctim ${cmd}: I/O error: ${e.message}\n`);
      return EXIT.IO;
    }
    err(`punctim ${cmd}: ${e && e.stack ? e.stack : e}\n`);
    return EXIT.IO;
  }
}

if (require.main === module) {
  let sigs = 0;
  const onSig = () => {
    sigs += 1;
    if (sigs > 1) process.exit(130);
    STOP.flag = true;
    if (STOP.reader && STOP.reader.interrupt) STOP.reader.interrupt();
  };
  process.on('SIGINT', onSig);
  process.on('SIGTERM', onSig);
  main(process.argv.slice(2)).then((rc) => process.exit(rc), (e) => {
    err(`punctim: ${e && e.stack ? e.stack : e}\n`);
    process.exit(EXIT.IO);
  });
}

module.exports = { parseUri, runIo, decodeRecord, findVectors, EXIT };
