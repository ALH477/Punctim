// SPDX-License-Identifier: LGPL-3.0-only
'use strict';

// Certifies src/medium.js (DCF-Medium: stream / hex / udp_proto / udp_bare / l2eth /
// hydra_symbols / afsk_bits) byte-for-byte against the cross-language golden vectors in
// Documentation/medium_vectors.json, then checks the medium laws the generator enforces
// (FEC correction on the symbol/bit streams, resync on random garbage). Dependency-free.
//   node JS/nodejs/test/certify_medium.js   (from the repo root)

const fs = require('fs');
const path = require('path');
const M = require('../src/medium.js');

function loadVectors() {
  const candidates = [
    process.env.PUNCTIM_VECTORS && path.join(process.env.PUNCTIM_VECTORS, 'medium_vectors.json'),
    path.join(__dirname, '..', '..', '..', 'Documentation', 'medium_vectors.json'),
    path.join(__dirname, '..', '..', '..', 'python', 'MCP', 'medium_vectors.json'),
  ].filter(Boolean);
  for (const p of candidates) {
    if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, 'utf8'));
  }
  throw new Error('medium_vectors.json not found (run python3 python/MCP/gen_medium_vectors.py)');
}

let fails = 0;
function check(cond, label) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + label);
  if (!cond) fails += 1;
}

const mv = loadVectors();

// 1. every family, byte-for-byte (the same runner `punctim certify` uses)
for (const r of M.certifyVectors(mv)) {
  for (const c of r.checks) check(c.ok, c.label);
}

// 2. FEC laws on the hydra symbol stream (as gen_medium_vectors.py asserts)
function flipCoded(p, symbols, positions) {
  const bps = p.bits_per_symbol;
  const head = p.preamble_syms + p.sync_syms;
  const syms = [...symbols].map((c) => parseInt(c, 16));
  const bits = M.hydraSymbolsToBits(syms.slice(head), bps);
  for (const q of positions) bits[q] ^= 1;
  return symbols.slice(0, head) + M.hydraBitsToSymbols(bits, bps).map((s) => s.toString(16)).join('');
}
{
  let conv = 0;
  let rep3 = 0;
  let none = 0;
  let bad = 0;
  for (const c of mv.families.hydra_symbols.cases) {
    const p = M.hydraProfile(c.profile, { fec: c.fec, interleave: c.interleave, n_tones: c.n_tones });
    const f = c.frame;
    const bi = Number(c.name.slice(1, c.name.indexOf('_')));
    const cb = p.coded_bits;
    if (c.fec === 'conv') {
      for (const k of [1, 2, 3]) {
        const pos = [...new Set(Array.from({ length: k }, (_, j) => (bi * 37 + j * 101 + k * 13) % cb))].sort((a, b) => a - b);
        const g = M.hydraSymbolsDecode(p, flipCoded(p, c.symbols, pos));
        if (g && g.toString('hex') === f) conv++;
        else bad++;
      }
    } else if (c.fec === 'rep3') {
      const st = p.interleave_stride;
      const pos = [3, 50, 140].map((trip) => {
        const src = 3 * trip + (bi % 3);
        if (!c.interleave) return src;
        for (let i = 0; i < cb; i++) if ((i * st) % cb === src) return i;
        return -1;
      });
      const g = M.hydraSymbolsDecode(p, flipCoded(p, c.symbols, pos));
      if (g && g.toString('hex') === f) rep3++;
      else bad++;
    } else {
      if (M.hydraSymbolsDecode(p, flipCoded(p, c.symbols, [(bi * 11) % cb])) === null) none++;
      else bad++;
    }
  }
  check(bad === 0, `hydra FEC laws: conv corrects 1..3 coded-bit flips (${conv}), rep3 majority (${rep3}), none detects (${none})`);
}

// 3. hard Viterbi inverts the encoder on arbitrary messages (deterministic LCG)
{
  let seed = 0x2DD4;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) >>> 0) >>> 16) & 1;
  let ok = true;
  for (let t = 0; t < 32; t++) {
    const bits = Array.from({ length: 16 + t * 5 }, rnd);
    const back = M.hydraConvDecodeHard(M.hydraConvEncode(bits));
    if (back.length !== bits.length || back.some((b, i) => b !== bits[i])) ok = false;
  }
  check(ok, 'hydra conv: hard Viterbi decode(encode(m)) = m on 32 random messages');
}

// 4. resync law on random garbage: garbage || stream(F) || garbage decodes to F
{
  const frames = mv.basis.map((b) => Buffer.from(b.hex, 'hex'));
  let seed = 7;
  const byte = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) >>> 24);
  let ok = true;
  for (let t = 0; t < 64; t++) {
    const g1 = Buffer.from(Array.from({ length: t % 23 }, byte).map((b) => (b === 0xD3 ? 0 : b)));
    const g2 = Buffer.from(Array.from({ length: (t * 7) % 19 }, byte).map((b) => (b === 0xD3 ? 0 : b)));
    const blob = Buffer.concat([g1, M.streamEncode(frames), g2]);
    const r = M.streamDecode(blob);
    if (r.frames.length !== frames.length || r.frames.some((f, i) => !f.equals(frames[i]))) ok = false;
    if (r.skippedBytes + r.tailBytes !== g1.length + g2.length) ok = false;
  }
  check(ok, 'stream: garbage || encode(F) || garbage decodes to F (64 random wrappings)');
}

// 5. proto ts beyond 2^53 round-trips as BigInt (the Go golden vector)
{
  const g = mv.families.udp_proto.cases.find((c) => c.name === 'go_golden');
  const m = M.protoDecode(Buffer.from(g.datagram, 'hex'));
  check(m.ts === 72623859790382856n && m.ts.toString() === String(BigInt('0x' + g.ts_hex)) && g.ts > Number.MAX_SAFE_INTEGER,
    `udp_proto: u64 ts ${m.ts} (> 2^53) decoded exactly as BigInt`);
}

if (fails) {
  console.error(`\n${fails} CHECK(S) FAILED`);
  process.exit(1);
}
console.log('\nALL MEDIUM VECTORS HOLD — Node.js DCF Medium is cemented.');
