// SPDX-License-Identifier: LGPL-3.0-only
'use strict';

// DCF-Medium — the medium codecs (Node.js port), byte-identical to the canonical
// python/MCP/mediumlab_core.py and certified against Documentation/medium_vectors.json.
//
// A *medium codec* is a deterministic pair
//     encode : [frame]        -> representation
//     decode : representation -> ([frame], diagnostics)
// that carries the 17-byte DeModFrame quantum over one medium WITHOUT parsing it beyond
// the frame gate (sync 0xD3 + version nibble 1 + CRC-16/CCITT-FALSE). Media sit *beneath*
// the quantum, so the 246-vector wire certificate is untouched. Spec:
// Documentation/DCF_MEDIUM_SPEC.md.
//
// Families (all byte-certified; the analog ones to their symbol / bit stream):
//   stream         .dcf file / stdio — concatenated frames, byte-wise resync on decode
//   hex            34 lowercase hex chars + "\n" per frame
//   udp_proto      ProtoMessage envelope [type u8][seq u32][ts u64][len u32], FRAME = 12
//   udp_bare       bare 17-B frame or a 32-B SuperPack pair per datagram
//   l2eth          [n u16 BE][SuperPack * ceil(n/2)] Ethernet payload (DCF-Snake raw L2)
//   hydra_symbols  HydraModem M-FSK tone-index stream (port of hydramodem/src/hydra_*.c)
//   afsk_bits      python/modem AFSK on-air bit stream (acoustic_frame.encode_bits)
//
// `hydra` and `afsk` are two DIFFERENT acoustic media (different tones, sync word and
// FEC); they do not interoperate with each other.

const wire = require('./frame.js');
const sp = require('./superpack.js');
const fec = require('./fec.js');

const FRAME_LEN = wire.FRAME_SIZE; // 17

// ── the frame gate (the existing validity rule; media never parse further) ─────────────
/** True iff `f` is a valid DeModFrame: 17 bytes, sync 0xD3, version nibble 1, CRC ok. */
function gate(f) {
  if (!f || f.length !== FRAME_LEN) return false;
  if ((f[0] & 0xFF) !== wire.SYNC || ((f[1] & 0xFF) >>> 4) !== wire.VERSION) return false;
  return wire.crc16(f, wire.CRC_COVER) === (((f[15] & 0xFF) << 8) | (f[16] & 0xFF));
}

function needFrame(f) {
  if (!f || f.length !== FRAME_LEN) throw new Error(`need ${FRAME_LEN}-byte frames, got ${f ? f.length : f}`);
}

// ══ stream (.dcf file, stdio) ═══════════════════════════════════════════════════════════
/** Concatenate 17-byte frames (the .dcf / stdio representation). */
function streamEncode(frames) {
  for (const f of frames) needFrame(f);
  return Buffer.concat(frames.map((f) => Buffer.from(f)));
}

// Byte-wise resync scan of buf from offset i. Appends gated frames; returns [i, skipped]
// with n - i < 17 on return. At offset i: if the 17-byte window passes the gate, emit it
// and i += 17; else i += 1, skipped += 1.
function scan(buf, i, skipped, frames) {
  const last = buf.length - FRAME_LEN; // highest offset with a full window
  while (i <= last) {
    if (buf[i] !== wire.SYNC) {
      // fast-forward to the next 0xD3 that still has a full window (pure speed-up)
      const j = buf.indexOf(wire.SYNC, i + 1);
      if (j < 0 || j > last) {
        skipped += last + 1 - i;
        i = last + 1;
        break;
      }
      skipped += j - i;
      i = j;
      continue;
    }
    const w = buf.subarray(i, i + FRAME_LEN);
    if (gate(w)) {
      frames.push(Buffer.from(w));
      i += FRAME_LEN;
    } else {
      i += 1;
      skipped += 1;
    }
  }
  return [i, skipped];
}

/** Byte-wise resync decode -> {frames, skippedBytes, tailBytes}. */
function streamDecode(buf) {
  buf = Buffer.from(buf);
  const frames = [];
  const [i, skipped] = scan(buf, 0, 0, frames);
  return { frames, skippedBytes: skipped, tailBytes: buf.length - i };
}

/** Incremental stream decoder: any chunking yields exactly streamDecode()'s frames and
 *  skippedBytes; the carry is at most 16 bytes; flush() returns (and clears) the tail. */
class StreamScanner {
  constructor() {
    this.carry = Buffer.alloc(0);
    this.skippedBytes = 0;
    this.framesOut = 0;
  }

  feed(chunk) {
    const buf = this.carry.length ? Buffer.concat([this.carry, Buffer.from(chunk)]) : Buffer.from(chunk);
    const frames = [];
    const [i, skipped] = scan(buf, 0, this.skippedBytes, frames);
    this.skippedBytes = skipped;
    this.carry = Buffer.from(buf.subarray(i));
    this.framesOut += frames.length;
    return frames;
  }

  /** Bytes currently carried (a possible frame prefix), <= 16. */
  get pending() {
    return this.carry.length;
  }

  flush() {
    const tail = this.carry;
    this.carry = Buffer.alloc(0);
    return tail;
  }
}

// ══ hex (text lines) ════════════════════════════════════════════════════════════════════
const HEX34 = /^[0-9a-fA-F]{34}$/;

/** Strip Python's " \t\r\v\f" (NOT "\n": lines are already split on it). */
function stripWs(s) {
  let a = 0;
  let b = s.length;
  const ws = (c) => c === 0x20 || c === 0x09 || c === 0x0D || c === 0x0B || c === 0x0C;
  while (a < b && ws(s.charCodeAt(a))) a++;
  while (b > a && ws(s.charCodeAt(b - 1))) b--;
  return s.slice(a, b);
}

/** One frame per line: 34 lowercase hex characters + "\n". */
function hexEncode(frames) {
  let out = '';
  for (const f of frames) {
    needFrame(f);
    out += Buffer.from(f).toString('hex') + '\n';
  }
  return out;
}

/** Classify one hex line: returns a Buffer (a frame), null (skipped), or false (bad line). */
function hexLine(line) {
  const s = stripWs(line);
  if (!s || s[0] === '#') return null;
  if (!HEX34.test(s)) return false;
  return Buffer.from(s, 'hex');
}

/** Parse hex lines -> {frames, badLines}. Frames are returned raw (NOT gated). */
function hexDecode(text) {
  const frames = [];
  let badLines = 0;
  for (const line of String(text).split('\n')) {
    const r = hexLine(line);
    if (r === null) continue;
    if (r === false) badLines++;
    else frames.push(r);
  }
  return { frames, badLines };
}

// ══ udp dialect "proto" (ProtoMessage envelope) ═════════════════════════════════════════
// Byte-identical to python/dcf/proto.py, go/node/proto.go, rust/src/lib.rs, C dcf_proto.h:
//   [0] msg_type u8 | [1:5] sequence u32 | [5:13] timestamp u64 | [13:17] payload_len u32
const PROTO_HEADER_LEN = 17;
const MSG_FRAME = 12;
const MSG_TYPES = Object.freeze({
  POSITION: 1, AUDIO: 2, GAME_EVENT: 3, STATE_SYNC: 4, RELIABLE: 5,
  ACK: 6, PING: 7, PONG: 8, GAME_DCF: 9, TEXT_DCF: 10, MESH: 11, FRAME: 12,
});
const U64_MAX = (1n << 64n) - 1n;

/** Serialize one ProtoMessage. `seq` wraps modulo 2^32; `ts` is a BigInt (or safe number). */
function protoEncode(type, seq, ts, payload) {
  const pl = Buffer.from(payload || []);
  if (!Number.isInteger(type) || type < 0 || type > 0xFF) throw new Error('msg_type must be u8');
  const t = BigInt(ts);
  if (t < 0n || t > U64_MAX) throw new Error('timestamp must be u64');
  const h = Buffer.alloc(PROTO_HEADER_LEN);
  h[0] = type;
  h.writeUInt32BE(Number(BigInt.asUintN(32, BigInt(seq))), 1);
  h.writeBigUInt64BE(t, 5);
  h.writeUInt32BE(pl.length, 13);
  return Buffer.concat([h, pl]);
}

/** Parse one ProtoMessage -> {type, seq, ts (BigInt), payload}. Throws on a short header or
 *  a payload_len that overruns the datagram; bytes after payload_len are ignored. */
function protoDecode(buf) {
  buf = Buffer.from(buf);
  if (buf.length < PROTO_HEADER_LEN) throw new Error('message shorter than 17-byte header');
  const plen = buf.readUInt32BE(13);
  if (buf.length < PROTO_HEADER_LEN + plen) throw new Error('payload length exceeds message size');
  return {
    type: buf[0],
    seq: buf.readUInt32BE(1),
    ts: buf.readBigUInt64BE(5),
    payload: Buffer.from(buf.subarray(PROTO_HEADER_LEN, PROTO_HEADER_LEN + plen)),
  };
}

/** One frame as a 34-byte ProtoMessage(MSG_FRAME, seq, ts, len 17, frame). */
function protoFrameEncode(f, seq, ts = 0n) {
  needFrame(f);
  return protoEncode(MSG_FRAME, seq, ts, f);
}

/** The carried frame, or null unless msg_type == 12 and payload_len == 17 (not gated). */
function protoFrameDecode(dgram) {
  let m;
  try {
    m = protoDecode(dgram);
  } catch (e) {
    return null;
  }
  if (m.type !== MSG_FRAME || m.payload.length !== FRAME_LEN) return null;
  return m.payload;
}

// ══ udp dialect "bare" (17-B frame / 32-B SuperPack per datagram) ═══════════════════════
/** Datagrams for a frame sequence: with `pair`, consecutive pairs become one 32-byte
 *  SuperPack and a lone trailing frame goes raw (17 B); without, every frame goes raw. */
function bareEncode(frames, pair = true) {
  const fs = frames.map((f) => Buffer.from(f));
  if (!pair) return fs;
  const out = [];
  let i = 0;
  for (; i + 1 < fs.length; i += 2) out.push(sp.pack(fs[i], fs[i + 1]));
  if (i < fs.length) out.push(fs[i]);
  return out;
}

/** Frames carried by one bare datagram: a valid 32-B SuperPack -> its 2 frames; a 17-B
 *  datagram -> [it] (not gated); anything else -> []. */
function bareDecode(dgram) {
  const d = Buffer.from(dgram);
  if (d.length === sp.SUPER_LEN && sp.isSuperpack(d)) {
    try {
      return sp.unpack(d);
    } catch (e) {
      return [];
    }
  }
  if (d.length === FRAME_LEN) return [d];
  return [];
}

// ══ l2eth (raw-L2 Ethernet payload; byte-identical to hydramodem/dcf-tools/snake_l2.h) ══
const L2_HDR = 2;
// The canonical zero filler: a valid DATA frame with all application fields 0.
const L2_FILLER = wire.encode({ version: 1, type: 0, seq: 0, src: 0, dst: 0, payload: Buffer.alloc(4), tsUs: 0 });

/** Number of DeModFrames that fit one Ethernet payload of the given MTU. */
function l2Capacity(mtu) {
  if (mtu < L2_HDR) return 0;
  return Math.floor((mtu - L2_HDR) / sp.SUPER_LEN) * 2;
}

/** [n_frames u16 BE][SuperPack * ceil(n/2)], odd tail paired with L2_FILLER. */
function l2Batch(frames) {
  const n = frames.length;
  if (n > 0xFFFF) throw new Error('too many frames for one batch');
  const parts = [Buffer.from([(n >>> 8) & 0xFF, n & 0xFF])];
  for (let i = 0; i < n; i += 2) {
    const b = i + 1 < n ? frames[i + 1] : L2_FILLER;
    parts.push(sp.pack(Buffer.from(frames[i]), Buffer.from(b)));
  }
  return Buffer.concat(parts);
}

/** Split an Ethernet payload back into frames (bit-exact). Throws on a short/truncated
 *  batch or a corrupt SuperPack; trailing padding bytes are ignored. */
function l2Unbatch(buf) {
  buf = Buffer.from(buf);
  if (buf.length < L2_HDR) throw new Error('short batch');
  const n = buf.readUInt16BE(0);
  const npairs = (n + 1) >>> 1;
  if (buf.length < L2_HDR + npairs * sp.SUPER_LEN) throw new Error('truncated batch');
  const frames = [];
  let off = L2_HDR;
  for (let k = 0; k < npairs; k++) {
    const [a, b] = sp.unpack(buf.subarray(off, off + sp.SUPER_LEN));
    off += sp.SUPER_LEN;
    frames.push(a);
    if (frames.length < n) frames.push(b);
  }
  return frames;
}

// ══ hydra_symbols (HydraModem M-FSK; port of hydramodem/src/hydra_{profile,frame,conv,
//    fec,interleave,crc}.c — the C is the ground truth) ═════════════════════════════════
const HYDRA_DCF_BYTES = 17;
const HYDRA_CRC_BYTES = 2;
const HYDRA_SYNC_BITS = 16;
const HYDRA_DATA_BITS = (HYDRA_DCF_BYTES + HYDRA_CRC_BYTES) * 8; // 152
const HYDRA_CONV_MEM = 6;
const HYDRA_CONV_STATES = 64;
const G0 = 0x79; // 0171 octal; bit6 = newest tap
const G1 = 0x5B; // 0133 octal

const FEC_NONE = 0;
const FEC_REP3 = 1;
const FEC_CONV = 2; // the C hydra_fec_mode enum order
const FEC_NAMES = Object.freeze({ none: FEC_NONE, rep3: FEC_REP3, conv: FEC_CONV });
const FEC_BY_ID = ['none', 'rep3', 'conv'];

// User fields of hydra_profile_default() / hydra_profile_aux_cable(). The default profile
// ships fec_mode = CONV and interleave = 1 (hydra_profile.c).
const HYDRA_PROFILES = Object.freeze({
  default: Object.freeze({
    sample_rate: 48000.0, baud: 1000.0, n_tones: 2, base_freq: 2000.0,
    tone_spacing: 1000.0, preamble_syms: 24, sync_word: 0x2DD4,
    fec_mode: FEC_CONV, interleave: 1, tx_gain: 0.9,
  }),
  aux: Object.freeze({
    sample_rate: 48000.0, baud: 1200.0, n_tones: 2, base_freq: 1200.0,
    tone_spacing: 1200.0, preamble_syms: 16, sync_word: 0x2DD4,
    fec_mode: FEC_CONV, interleave: 1, tx_gain: 0.9,
  }),
});
const HYDRA_USER_FIELDS = Object.freeze(['sample_rate', 'baud', 'n_tones', 'base_freq', 'tone_spacing',
  'preamble_syms', 'sync_word', 'fec_mode', 'interleave', 'tx_gain']);

function fecId(v) {
  if (typeof v === 'string' && !/^\d+$/.test(v)) {
    if (!(v in FEC_NAMES)) throw new Error(`unknown fec ${JSON.stringify(v)} (none|rep3|conv)`);
    return FEC_NAMES[v];
  }
  const n = Number(v);
  if (!Number.isInteger(n) || n < 0 || n > 2) throw new Error(`unknown fec mode ${v}`);
  return n;
}

function gcd(a, b) {
  while (b) [a, b] = [b, a % b];
  return a;
}

function hydraConvCodedLen(nMsg) {
  return 2 * (nMsg + HYDRA_CONV_MEM);
}

/** Deterministic coprime stride ~ sqrt(n) (hydra_interleave.c). */
function hydraInterleaveStride(n) {
  if (n < 3) return 1;
  let s = 1;
  while (s * s < n) s++;
  if (s >= n) s = n - 1;
  while (s < n && gcd(s, n) !== 1) s++;
  if (s >= n) {
    s = 2;
    while (s < n && gcd(s, n) !== 1) s++;
    if (s >= n) s = 1;
  }
  return s;
}

function toInt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) throw new Error(`not a number: ${v}`);
  return Math.trunc(n);
}

/** A hydra profile object: the named profile's user fields (+ overrides), with every
 *  derived field computed exactly as hydra_profile_init(). `fec`/`fec_mode` accept
 *  "none"|"rep3"|"conv" or 0|1|2. Throws on a config hydra_profile_init rejects. */
function hydraProfile(name = 'default', overrides = {}) {
  if (!Object.prototype.hasOwnProperty.call(HYDRA_PROFILES, name)) {
    throw new Error(`unknown hydra profile ${JSON.stringify(name)} (default|aux)`);
  }
  const p = Object.assign({}, HYDRA_PROFILES[name]);
  const ov = Object.assign({}, overrides);
  if ('fec' in ov) {
    ov.fec_mode = ov.fec;
    delete ov.fec;
  }
  for (const [k, v] of Object.entries(ov)) {
    if (!HYDRA_USER_FIELDS.includes(k)) throw new Error(`unknown hydra profile field ${JSON.stringify(k)}`);
    if (v === null || v === undefined) continue;
    p[k] = v;
  }
  p.fec_mode = fecId(p.fec_mode);
  for (const k of ['sample_rate', 'baud', 'base_freq', 'tone_spacing', 'tx_gain']) {
    p[k] = Number(p[k]);
    if (!Number.isFinite(p[k])) throw new Error(`${k} must be a number`);
  }
  for (const k of ['n_tones', 'preamble_syms', 'sync_word', 'interleave']) p[k] = toInt(p[k]);
  p.interleave = p.interleave ? 1 : 0;
  p.sync_word &= 0xFFFF;
  p.name = name;

  if (p.sample_rate <= 0 || p.baud <= 0) throw new Error('sample_rate/baud must be > 0');
  if (p.tone_spacing <= 0 || p.base_freq <= 0) throw new Error('base_freq/tone_spacing must be > 0');
  if (p.preamble_syms < 0) throw new Error('preamble_syms must be >= 0');
  const nt = p.n_tones;
  if (nt < 2 || (nt & (nt - 1)) !== 0) throw new Error('n_tones must be a power of two >= 2');
  const bps = Math.round(Math.log2(nt));
  p.bits_per_symbol = bps;
  p.samples_per_symbol = Math.floor(p.sample_rate / p.baud + 0.5);
  if (p.samples_per_symbol < 2) throw new Error('samples_per_symbol < 2');
  p.lp_cut = p.baud * 0.5;
  p.data_bits = HYDRA_DATA_BITS;
  p.coded_bits = p.fec_mode === FEC_NONE ? HYDRA_DATA_BITS
    : p.fec_mode === FEC_REP3 ? 3 * HYDRA_DATA_BITS : hydraConvCodedLen(HYDRA_DATA_BITS);
  p.interleave_stride = p.interleave ? hydraInterleaveStride(p.coded_bits) : 1;
  p.sync_syms = Math.floor((HYDRA_SYNC_BITS + bps - 1) / bps);
  p.data_syms = Math.floor((p.coded_bits + bps - 1) / bps);
  p.total_syms = p.preamble_syms + p.sync_syms + p.data_syms;
  if (p.base_freq + (nt - 1) * p.tone_spacing >= 0.5 * p.sample_rate) {
    throw new Error('highest tone at/above Nyquist');
  }
  for (const k of ['base_freq', 'tone_spacing']) {
    const c = p[k] / p.baud;
    if (Math.abs(c - Math.trunc(c + 0.5)) > 1e-6) {
      throw new Error(`${k} must be an integer multiple of baud (orthogonality)`);
    }
  }
  return p;
}

// ---- bit / byte / symbol helpers (MSB-first) ----
function hydraBytesToBits(data) {
  const out = [];
  for (const b of data) for (let i = 0; i < 8; i++) out.push((b >>> (7 - i)) & 1);
  return out;
}

function hydraBitsToBytes(bits) {
  const n = Math.floor(bits.length / 8);
  const out = Buffer.alloc(n);
  for (let i = 0; i < n; i++) {
    let v = 0;
    for (let j = 0; j < 8; j++) v = (v << 1) | (bits[8 * i + j] & 1);
    out[i] = v;
  }
  return out;
}

/** Pack bits into symbols MSB-first; the final symbol is zero-padded. */
function hydraBitsToSymbols(bits, bps) {
  const n = bits.length;
  const nsym = Math.floor((n + bps - 1) / bps);
  const out = [];
  for (let s = 0; s < nsym; s++) {
    let v = 0;
    for (let b = 0; b < bps; b++) {
      const idx = s * bps + b;
      v = (v << 1) | (idx < n ? bits[idx] & 1 : 0);
    }
    out.push(v);
  }
  return out;
}

function hydraSymbolsToBits(symbols, bps) {
  const out = [];
  for (const s of symbols) for (let b = 0; b < bps; b++) out.push((s >>> (bps - 1 - b)) & 1);
  return out;
}

// ---- FEC: K=7 r=1/2 convolutional (G0=0171, G1=0133, 6 zero tail bits) ----
function parity7(v) {
  v &= 0x7F;
  v ^= v >>> 4;
  v ^= v >>> 2;
  v ^= v >>> 1;
  return v & 1;
}

// TRELLIS[state*2 + bit] = [o0, o1, next]
const TRELLIS = (() => {
  const t = [];
  for (let s = 0; s < HYDRA_CONV_STATES; s++) {
    for (let b = 0; b < 2; b++) {
      const reg = ((b << 6) | s) & 0x7F;
      t.push([parity7(reg & G0), parity7(reg & G1), reg >>> 1]);
    }
  }
  return t;
})();

/** n message bits -> 2*(n+6) coded bits (tail-flushed to state 0), as hydra_conv.c. */
function hydraConvEncode(bits) {
  let state = 0;
  const out = [];
  const n = bits.length;
  for (let i = 0; i < n + HYDRA_CONV_MEM; i++) {
    const b = i < n ? bits[i] & 1 : 0;
    const [o0, o1, ns] = TRELLIS[state * 2 + b];
    out.push(o0, o1);
    state = ns;
  }
  return out;
}

/** Hard-decision Viterbi: coded bits (2*(n+6)) -> n message bits. The C soft decoder's
 *  correlation metric on +/-1 hard values, same state/bit order and strict '>' tie rule,
 *  start state 0, traceback from state 0. */
function hydraConvDecodeHard(coded) {
  const nc = coded.length;
  if (nc % 2 || nc / 2 <= HYDRA_CONV_MEM) throw new Error('bad coded length');
  const L = nc / 2;
  const nMsg = L - HYDRA_CONV_MEM;
  const NEG = -Infinity;
  let pm = new Array(HYDRA_CONV_STATES).fill(NEG);
  pm[0] = 0;
  const tbPrev = [];
  const tbBit = [];
  for (let t = 0; t < L; t++) {
    const s0 = coded[2 * t] & 1 ? 1 : -1;
    const s1 = coded[2 * t + 1] & 1 ? 1 : -1;
    const npm = new Array(HYDRA_CONV_STATES).fill(NEG);
    const prev = new Uint8Array(HYDRA_CONV_STATES);
    const bitv = new Uint8Array(HYDRA_CONV_STATES);
    for (let s = 0; s < HYDRA_CONV_STATES; s++) {
      const base = pm[s];
      if (base === NEG) continue;
      for (let b = 0; b < 2; b++) {
        const [o0, o1, ns] = TRELLIS[s * 2 + b];
        const cand = base + (o0 ? s0 : -s0) + (o1 ? s1 : -s1);
        const cur = npm[ns];
        if (cur === NEG || cand > cur) {
          npm[ns] = cand;
          prev[ns] = s;
          bitv[ns] = b;
        }
      }
    }
    tbPrev.push(prev);
    tbBit.push(bitv);
    pm = npm;
  }
  let s = 0;
  const bits = new Array(L).fill(0);
  for (let t = L - 1; t >= 0; t--) {
    bits[t] = tbBit[t][s];
    s = tbPrev[t][s];
  }
  return bits.slice(0, nMsg);
}

// ---- FEC: repetition-3 (hydra_fec.c) ----
function hydraRep3Encode(bits) {
  const out = [];
  for (const x of bits) {
    const b = x ? 1 : 0;
    out.push(b, b, b);
  }
  return out;
}

function hydraRep3Decode(coded) {
  const n = Math.floor(coded.length / 3);
  const out = new Array(n);
  for (let i = 0; i < n; i++) {
    out[i] = (coded[3 * i] & 1) + (coded[3 * i + 1] & 1) + (coded[3 * i + 2] & 1) >= 2 ? 1 : 0;
  }
  return out;
}

// ---- coprime-stride block interleaver (hydra_interleave.c) ----
/** TX gather: out[i] = in[(i*stride) % n]. */
function hydraInterleave(bits, stride) {
  const n = bits.length;
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[i] = bits[(i * stride) % n];
  return out;
}

/** RX scatter: out[(i*stride) % n] = in[i] — the exact inverse of hydraInterleave. */
function hydraDeinterleave(bits, stride) {
  const n = bits.length;
  const out = new Array(n).fill(0);
  for (let i = 0; i < n; i++) out[(i * stride) % n] = bits[i];
  return out;
}

function hydraSyncBits(p) {
  return hydraBytesToBits([(p.sync_word >>> 8) & 0xFF, p.sync_word & 0xFF]);
}

const SYMCHARS = '0123456789abcdef';

/** The full TX symbol stream of hydra_frame_build() as a string, one lowercase hex digit
 *  per symbol (tone index; n_tones <= 16):
 *    [preamble: preamble_syms alternating tone 0 / tone n_tones-1, starting with 0]
 *    [sync_word bits -> symbols]
 *    [interleave?(fec(frame17 || CRC16be(frame17))) -> symbols] */
function hydraSymbolsEncode(p, frame17) {
  const f = Buffer.from(frame17);
  if (f.length !== HYDRA_DCF_BYTES) throw new Error('need a 17-byte frame');
  if (p.n_tones > 16) throw new Error('symbol strings need n_tones <= 16');
  const crc = wire.crc16(f);
  const field = Buffer.concat([f, Buffer.from([(crc >>> 8) & 0xFF, crc & 0xFF])]);
  const dataBits = hydraBytesToBits(field);
  let coded;
  if (p.fec_mode === FEC_NONE) coded = dataBits.slice();
  else if (p.fec_mode === FEC_REP3) coded = hydraRep3Encode(dataBits);
  else coded = hydraConvEncode(dataBits);
  if (coded.length !== p.coded_bits) throw new Error('coded length mismatch');
  if (p.interleave) coded = hydraInterleave(coded, p.interleave_stride);
  const bps = p.bits_per_symbol;
  const syms = [];
  for (let k = 0; k < p.preamble_syms; k++) syms.push(k & 1 ? p.n_tones - 1 : 0);
  for (const s of hydraBitsToSymbols(hydraSyncBits(p), bps)) syms.push(s);
  for (const s of hydraBitsToSymbols(coded, bps)) syms.push(s);
  if (syms.length !== p.total_syms) throw new Error('symbol count mismatch');
  let out = '';
  for (const s of syms) out += SYMCHARS[s];
  return out;
}

/** Inverse with hard decisions: strip the preamble by count, check the 16 sync bits,
 *  deinterleave, FEC-decode (none / rep3 majority / hard Viterbi), then the CRC-16 check.
 *  Returns the 17-byte frame Buffer, or null. */
function hydraSymbolsDecode(p, symbols) {
  if (typeof symbols !== 'string' || symbols.length !== p.total_syms) return null;
  const syms = new Array(symbols.length);
  for (let i = 0; i < symbols.length; i++) {
    const v = parseInt(symbols[i], 16);
    if (Number.isNaN(v) || v >= p.n_tones) return null;
    syms[i] = v;
  }
  const bps = p.bits_per_symbol;
  let off = p.preamble_syms;
  const sync = hydraSymbolsToBits(syms.slice(off, off + p.sync_syms), bps).slice(0, HYDRA_SYNC_BITS);
  const want = hydraSyncBits(p);
  for (let i = 0; i < HYDRA_SYNC_BITS; i++) if (sync[i] !== want[i]) return null;
  off += p.sync_syms;
  let coded = hydraSymbolsToBits(syms.slice(off, off + p.data_syms), bps).slice(0, p.coded_bits);
  if (p.interleave) coded = hydraDeinterleave(coded, p.interleave_stride);
  let data;
  if (p.fec_mode === FEC_NONE) data = coded.slice(0, HYDRA_DATA_BITS);
  else if (p.fec_mode === FEC_REP3) data = hydraRep3Decode(coded);
  else data = hydraConvDecodeHard(coded);
  const field = hydraBitsToBytes(data);
  const f = Buffer.from(field.subarray(0, HYDRA_DCF_BYTES));
  const crc = field.readUInt16BE(HYDRA_DCF_BYTES);
  return wire.crc16(f) === crc ? f : null;
}

// ══ afsk_bits (python/modem acoustic_frame: preamble + 0x7E + frame+crc8 | RS + postamble) ═
const AFSK_SYNC = 0x7E;
const AFSK_POSTAMBLE_BITS = 16;
const AFSK_NPARITY = fec.RS_DEFAULT_NPARITY; // 16
const AFSK_PROFILES = Object.freeze({
  standard: Object.freeze({ mark: 1200.0, space: 2200.0, baud: 300, preamble_bits: 80 }),
  handheld: Object.freeze({ mark: 1200.0, space: 1800.0, baud: 300, preamble_bits: 240 }),
  'aux-cable': Object.freeze({ mark: 1000.0, space: 1500.0, baud: 1200, preamble_bits: 16 }),
});

/** Poly-0x31 CRC-8 (MSB-first, init 0x00, non-reflected; the Faust modem's "MAXIM"). */
function crc8(data) {
  let crc = 0;
  for (const byte of data) {
    crc ^= byte & 0xFF;
    for (let i = 0; i < 8; i++) crc = crc & 0x80 ? ((crc << 1) ^ 0x31) & 0xFF : (crc << 1) & 0xFF;
  }
  return crc;
}

function afskProfile(profile) {
  if (!Object.prototype.hasOwnProperty.call(AFSK_PROFILES, profile)) {
    throw new Error(`unknown afsk profile ${JSON.stringify(profile)}`);
  }
  return AFSK_PROFILES[profile];
}

function altBits(n) {
  let s = '';
  for (let i = 0; i < n; i++) s += i % 2 ? '1' : '0';
  return s;
}

function bytesToBitString(data) {
  let s = '';
  for (const b of data) s += (b & 0xFF).toString(2).padStart(8, '0');
  return s;
}

/** The on-air AFSK bit stream ("0"/"1" string): preamble (alternating 0,1..) + 0x7E +
 *  (frame+crc8 | RS16 codeword) + 16 alternating postamble bits. */
function afskBitsEncode(frame17, profile = 'handheld', useFec = false) {
  const pr = afskProfile(profile);
  const f = Buffer.from(frame17);
  const payload = useFec ? fec.rsEncode(f, AFSK_NPARITY) : Buffer.concat([f, Buffer.from([crc8(f)])]);
  return altBits(pr.preamble_bits) + bytesToBitString([AFSK_SYNC]) + bytesToBitString(payload)
    + altBits(AFSK_POSTAMBLE_BITS);
}

/** Inverse (bit-level 0x7E search, then crc8 check or RS decode). Returns the 17-byte frame
 *  Buffer or null. `profile` is accepted for symmetry; the bit layer is preamble agnostic. */
function afskBitsDecode(bits, profile = 'handheld', useFec = false) {
  afskProfile(profile);
  if (typeof bits !== 'string' || !/^[01]*$/.test(bits)) return null;
  const pos = bits.indexOf('01111110');
  if (pos < 0) return null;
  const rest = bits.slice(pos + 8);
  const nbytes = Math.floor(rest.length / 8);
  const data = Buffer.alloc(nbytes);
  for (let i = 0; i < nbytes; i++) data[i] = parseInt(rest.slice(8 * i, 8 * i + 8), 2);
  if (useFec) {
    const n = FRAME_LEN + AFSK_NPARITY;
    if (data.length < n) return null;
    try {
      return Buffer.from(fec.rsDecode(data.subarray(0, n), AFSK_NPARITY, FRAME_LEN).msg);
    } catch (e) {
      return null;
    }
  }
  if (data.length < FRAME_LEN + 1) return null;
  const f = Buffer.from(data.subarray(0, FRAME_LEN));
  return crc8(f) === data[FRAME_LEN] ? f : null;
}

// ══ certificate runner (shared by test/certify_medium.js and `punctim certify`) ═════════
const FAMILIES = Object.freeze(['stream', 'hex', 'udp_proto', 'udp_bare', 'l2eth', 'hydra_symbols', 'afsk_bits']);
const EXAMPLE_FRAME = 'd31312340001ffffdeadbeefab12cd24c0';

const hx = (list) => list.map((b) => Buffer.from(b).toString('hex'));
const sameList = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

// Each checker gets (family vectors, ck) and calls ck(cond, label) per assertion group.
function certAnchors(mv, ck) {
  const a = mv.anchors || {};
  ck(wire.crc16(Buffer.from('123456789', 'ascii')) === a.crc_123456789
    && wire.crc16(Buffer.alloc(15)) === a.crc_zero15
    && a.frame_len === FRAME_LEN && a.proto_header_len === PROTO_HEADER_LEN
    && a.msg_frame === MSG_FRAME && a.super_len === sp.SUPER_LEN && a.l2_hdr === L2_HDR
    && a.l2_filler === L2_FILLER.toString('hex') && a.hydra_sync_word === HYDRA_PROFILES.default.sync_word
    && a.afsk_sync === AFSK_SYNC && a.afsk_crc8_123456789 === crc8(Buffer.from('123456789', 'ascii')),
  'anchors: CRC 0x29B1/0x4EC3, proto header 17, MSG_FRAME 12, SuperPack 32, L2 filler, sync words, crc8');
  const basis = mv.basis || [];
  ck(basis.length > 0 && basis.every((b) => wire.encode({
    version: 1, type: b.type, seq: b.seq, src: b.src, dst: b.dst,
    payload: Buffer.from(b.payload, 'hex'), tsUs: b.ts,
  }).toString('hex') === b.hex && gate(Buffer.from(b.hex, 'hex'))),
  `basis: ${basis.length} frames re-encode byte-identically and pass the gate`);
}

function certStream(fam, ck) {
  const cs = fam.cases;
  let dec = true;
  let chunk = true;
  let enc = true;
  for (const c of cs) {
    const input = Buffer.from(c.input, 'hex');
    const r = streamDecode(input);
    if (!sameList(hx(r.frames), c.frames) || r.skippedBytes !== c.skipped_bytes || r.tailBytes !== c.tail_bytes) dec = false;
    for (let step = 1; step <= 35; step++) {
      const sc = new StreamScanner();
      const got = [];
      for (let i = 0; i < input.length; i += step) got.push(...sc.feed(input.subarray(i, i + step)));
      if (!sameList(hx(got), c.frames) || sc.skippedBytes !== c.skipped_bytes || sc.flush().length !== c.tail_bytes) chunk = false;
    }
    const e = streamEncode(c.frames.map((h) => Buffer.from(h, 'hex')));
    const rt = streamDecode(e);
    if (e.toString('hex') !== c.frames.join('') || !sameList(hx(rt.frames), c.frames) || rt.skippedBytes || rt.tailBytes) enc = false;
  }
  ck(dec, `stream: ${cs.length} cases decode (frames, skipped_bytes, tail_bytes) exactly`);
  ck(chunk, `stream: StreamScanner chunk-invariant (1..35-byte chunks) on all ${cs.length} cases`);
  ck(enc, 'stream: encode = concatenation, decode(encode(F)) = F losslessly');
}

function certHex(fam, ck) {
  const cs = fam.cases;
  let enc = true;
  let dec = true;
  for (const c of cs) {
    if (hexEncode(c.frames.map((h) => Buffer.from(h, 'hex'))) !== c.text) enc = false;
    const r = hexDecode(c.decode_input);
    if (!sameList(hx(r.frames), c.decoded) || r.badLines !== c.bad_lines) dec = false;
    const fp = hexDecode(c.text);
    if (!sameList(hx(fp.frames), c.decoded) || fp.badLines !== 0) dec = false;
  }
  ck(enc, `hex: ${cs.length} cases encode byte-identically (34 lowercase hex + LF)`);
  ck(dec, `hex: ${cs.length} cases decode (CRLF/uppercase/comments/blank; bad_lines counted; not gated)`);
}

function certProto(fam, ck) {
  const cs = fam.cases;
  let enc = true;
  let dec = true;
  let acc = true;
  for (const c of cs) {
    const ts = BigInt('0x' + c.ts_hex);
    const pl = Buffer.from(c.payload, 'hex');
    const dg = Buffer.from(c.datagram, 'hex');
    if (protoEncode(c.type, c.seq, ts, pl).toString('hex') !== c.datagram) enc = false;
    const m = protoDecode(dg);
    if (m.type !== c.type || m.seq !== c.seq || m.ts !== ts || !m.payload.equals(pl)) dec = false;
    const fr = protoFrameDecode(dg);
    if ((fr !== null) !== c.accept_as_frame || (fr && !fr.equals(pl))) acc = false;
    if (fr && protoFrameEncode(pl, c.seq, ts).toString('hex') !== c.datagram) enc = false;
  }
  let guards = true;
  const golden = Buffer.from(cs[cs.length - 1].datagram, 'hex');
  for (const bad of [Buffer.alloc(16), golden.subarray(0, golden.length - 1)]) {
    try {
      protoDecode(bad);
      guards = false;
    } catch (e) { /* expected */ }
  }
  ck(fam.header_len === PROTO_HEADER_LEN && fam.msg_frame === MSG_FRAME
    && Object.keys(fam.types).length === Object.keys(MSG_TYPES).length
    && Object.entries(fam.types).every(([k, v]) => MSG_TYPES[k] === v), 'udp_proto: header 17, MSG_FRAME 12, type registry 1..12');
  ck(enc, `udp_proto: ${cs.length} datagrams encode byte-identically (incl. u64 ts > 2^53, Go golden)`);
  ck(dec && guards, `udp_proto: ${cs.length} datagrams decode exactly; short-header/overrun guards hold`);
  ck(acc, 'udp_proto: accept_as_frame iff type 12 and len 17 (types 1..11 pass through)');
}

function certBare(fam, ck) {
  const cs = fam.cases;
  let enc = true;
  let dec = true;
  for (const c of cs) {
    const fr = c.frames.map((h) => Buffer.from(h, 'hex'));
    if (!sameList(hx(bareEncode(fr)), c.datagrams)) enc = false;
    const got = [];
    for (const d of c.datagrams) got.push(...bareDecode(Buffer.from(d, 'hex')));
    if (!sameList(hx(got), c.frames)) dec = false;
  }
  const tam = Buffer.from(bareEncode([L2_FILLER, Buffer.from(EXAMPLE_FRAME, 'hex')])[0]);
  tam[7] ^= 1;
  const rej = bareDecode(Buffer.alloc(33)).length === 0 && bareDecode(Buffer.alloc(16)).length === 0
    && bareDecode(tam).length === 0;
  ck(enc, `udp_bare: ${cs.length} cases encode byte-identically (pairs -> SuperPack, lone raw)`);
  ck(dec && rej, `udp_bare: ${cs.length} cases decode losslessly; bad lengths + tampered SuperPack rejected`);
}

function certL2(fam, ck) {
  const cs = fam.cases;
  let enc = true;
  let dec = true;
  for (const c of cs) {
    if (l2Batch(c.frames.map((h) => Buffer.from(h, 'hex'))).toString('hex') !== c.payload) enc = false;
    const buf = Buffer.from(c.payload, 'hex');
    if (!sameList(hx(l2Unbatch(buf)), c.frames)) dec = false;
    if (!sameList(hx(l2Unbatch(Buffer.concat([buf, Buffer.alloc(7)]))), c.frames)) dec = false;
    try {
      l2Unbatch(buf.subarray(0, buf.length - 1));
      dec = false;
    } catch (e) { /* truncation rejected */ }
  }
  ck(fam.hdr === L2_HDR && fam.filler === L2_FILLER.toString('hex') && l2Capacity(1500) === 92 && l2Capacity(9000) === 562,
    'l2eth: 2-byte header, zero-DATA filler, capacity(1500)=92');
  ck(enc, `l2eth: ${cs.length} payloads batch byte-identically`);
  ck(dec, `l2eth: ${cs.length} payloads unbatch losslessly; padding ignored, truncation rejected`);
}

function certHydra(fam, ck) {
  const cs = fam.cases;
  let prof = true;
  for (const [name, w] of Object.entries(fam.profiles)) {
    const m = HYDRA_PROFILES[name];
    if (!m || HYDRA_USER_FIELDS.some((k) => m[k] !== w[k])) prof = false;
  }
  let derived = true;
  let enc = true;
  let dec = true;
  for (const c of cs) {
    const p = hydraProfile(c.profile, { fec: c.fec, interleave: c.interleave, n_tones: c.n_tones });
    if (p.coded_bits !== c.coded_bits || p.interleave_stride !== c.interleave_stride || p.total_syms !== c.total_syms) derived = false;
    if (hydraSymbolsEncode(p, Buffer.from(c.frame, 'hex')) !== c.symbols) enc = false;
    const f = hydraSymbolsDecode(p, c.symbols);
    if (!f || f.toString('hex') !== c.frame) dec = false;
    const so = p.preamble_syms; // a corrupted sync symbol is rejected
    const bad = c.symbols.slice(0, so) + (c.symbols[so] === '0' ? '1' : '0') + c.symbols.slice(so + 1);
    if (hydraSymbolsDecode(p, bad) !== null) dec = false;
  }
  ck(prof, 'hydra_symbols: default + aux profile user fields match hydra_profile.c');
  ck(derived, `hydra_symbols: ${cs.length} cases derive coded_bits/interleave_stride/total_syms exactly`);
  ck(enc, `hydra_symbols: ${cs.length} symbol streams encode byte-identically`);
  ck(dec, `hydra_symbols: decode(symbols) = frame on all ${cs.length} (hard Viterbi/rep3/none); bad sync rejected`);
}

function certAfsk(fam, ck) {
  const cs = fam.cases;
  const names = Object.keys(fam.profiles);
  let prof = names.length === Object.keys(AFSK_PROFILES).length;
  for (const k of names) {
    const m = AFSK_PROFILES[k];
    const v = fam.profiles[k];
    if (!m || m.mark !== v.mark || m.space !== v.space || m.baud !== v.baud || m.preamble_bits !== v.preamble_bits) prof = false;
  }
  let enc = true;
  let dec = true;
  for (const c of cs) {
    const f = Buffer.from(c.frame, 'hex');
    const b = afskBitsEncode(f, c.profile, c.fec);
    if (b !== c.bits || b.length !== c.n_bits) enc = false;
    const d = afskBitsDecode(c.bits, c.profile, c.fec);
    if (!d || !d.equals(f)) dec = false;
    // flip a bit inside frame byte 3: RS corrects, crc8 detects
    const body = AFSK_PROFILES[c.profile].preamble_bits + 8 + 8 * 3;
    const fl = c.bits.slice(0, body) + (c.bits[body] === '0' ? '1' : '0') + c.bits.slice(body + 1);
    const g = afskBitsDecode(fl, c.profile, c.fec);
    if (c.fec ? !(g && g.equals(f)) : g !== null) dec = false;
  }
  ck(prof, 'afsk_bits: standard/handheld/aux-cable profiles match acoustic_frame.PROFILES');
  ck(enc, `afsk_bits: ${cs.length} bit streams encode byte-identically`);
  ck(dec, `afsk_bits: decode(bits) = frame on all ${cs.length}; RS corrects a flip, crc8 detects it`);
}

const CERTS = Object.freeze({
  stream: certStream, hex: certHex, udp_proto: certProto, udp_bare: certBare,
  l2eth: certL2, hydra_symbols: certHydra, afsk_bits: certAfsk,
});

/** Certify this module against a parsed medium_vectors.json. Returns one result per family
 *  — "anchors" (+ basis) first, then `families` (default: all seven) — as
 *  {family, ok, n, msg, checks: [{ok, label}]}; `n` = basis frames for anchors, else cases.
 *  An unknown family name throws an Error with code 'EUSAGE'. */
function certifyVectors(mv, families) {
  const fams = families && families.length ? families : FAMILIES;
  for (const name of fams) {
    if (!Object.prototype.hasOwnProperty.call(CERTS, name)) {
      const e = new Error(`unknown family '${name}' (one of: ${FAMILIES.join(', ')})`);
      e.code = 'EUSAGE';
      throw e;
    }
  }
  const run = (family, n, fn) => {
    const checks = [];
    const ck = (cond, label) => checks.push({ ok: !!cond, label });
    try {
      fn(ck);
    } catch (e) {
      checks.push({ ok: false, label: `${family}: ${e.name}: ${e.message}` });
    }
    const bad = checks.find((c) => !c.ok);
    return { family, ok: !bad, n, msg: bad ? bad.label : '', checks };
  };
  const out = [run('anchors', (mv.basis || []).length, (ck) => certAnchors(mv, ck))];
  const F = mv.families || {};
  for (const name of fams) {
    const fam = F[name];
    if (!fam) {
      out.push({ family: name, ok: false, n: 0, msg: 'family missing from vectors',
        checks: [{ ok: false, label: `${name}: family missing from vectors` }] });
      continue;
    }
    out.push(run(name, (fam.cases || []).length, (ck) => CERTS[name](fam, ck)));
  }
  return out;
}

module.exports = {
  FRAME_LEN, gate,
  // stream
  streamEncode, streamDecode, StreamScanner,
  // hex
  hexEncode, hexDecode, hexLine,
  // udp proto
  PROTO_HEADER_LEN, MSG_FRAME, MSG_TYPES, protoEncode, protoDecode, protoFrameEncode, protoFrameDecode,
  // udp bare
  bareEncode, bareDecode,
  // l2eth
  L2_HDR, L2_FILLER, l2Capacity, l2Batch, l2Unbatch,
  // hydra
  HYDRA_PROFILES, HYDRA_USER_FIELDS, HYDRA_DATA_BITS, FEC_NONE, FEC_REP3, FEC_CONV, FEC_NAMES, FEC_BY_ID,
  hydraProfile, hydraInterleaveStride, hydraConvEncode, hydraConvDecodeHard, hydraRep3Encode, hydraRep3Decode,
  hydraInterleave, hydraDeinterleave, hydraBytesToBits, hydraBitsToBytes, hydraBitsToSymbols, hydraSymbolsToBits,
  hydraSymbolsEncode, hydraSymbolsDecode,
  // afsk
  AFSK_SYNC, AFSK_PROFILES, AFSK_POSTAMBLE_BITS, crc8, afskBitsEncode, afskBitsDecode,
  // certificate
  FAMILIES, certifyVectors,
};
