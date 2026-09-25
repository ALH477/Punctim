# DCF-Medium — Deterministic Across Mediums

**Version 1** · DeMoD LLC · This document is normative. Companion to
[`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) (the 17-byte `DeModFrame`),
[`SUPERPACK_SPEC.md`](SUPERPACK_SPEC.md) (the 32-byte pair container) and
[`DCF_MODEM_SPEC.md`](DCF_MODEM_SPEC.md) (the acoustic modems).

Status: the **Python reference is canonical** (`python/MCP/mediumlab_core.py`), the
golden vectors are `Documentation/medium_vectors.json` (162 cases, with an identical
`python/MCP/` copy and the dependency-free `codec/medium_vectors.gen.h`), and the
uniform **`punctim`** CLI (`python/punctim.py`) is the Python implementation of the
cross-language contract in [§ punctim](#the-punctim-cli-contract). The C, Rust, Go and
Node ports are certified against the same vectors (see
[Reference implementations](#reference-implementations)).

## Motivation

One wire quantum is certified byte-for-byte across 13 languages. What rides *under* it —
a `.dcf` file, a pipe, a UDP datagram, a raw-Ethernet payload, an acoustic tone stream —
was, until this spec, a per-language, per-tool affair with incompatible encodings. DCF
is meant to be **deterministic across mediums**:

> a computer can process a DCF frame stream into any I/O combination, and every
> implementation produces the same bytes.

DCF-Medium makes that a contract. Every medium is a **deterministic codec** between a
sequence of frames and the medium's representation, pinned by golden vectors exactly
like the adapters. Media are transports *beneath* the quantum: they carry the 17 bytes
opaquely and never re-interpret them, so the **246-vector wire certificate is untouched**.

## The medium model

A **medium codec** is a pair of total functions

```
encode : [frame]          -> representation
decode : representation   -> ([frame], diagnostics)
diagnostics = { skipped_bytes, bad_lines, invalid_frames }
```

where a *frame* is a 17-byte string and the *representation* is the medium's native
unit: a byte stream, text lines, a datagram sequence, a symbol string or a bit string.

### The frame gate

A 17-byte window `w` is a **valid frame** iff

```
w[0] == 0xD3  ∧  (w[1] >> 4) == 1  ∧  CRC-16/CCITT-FALSE(w[0..14]) == be16(w[15..16])
```

This is exactly the existing wire-quantum validity rule (anchors `CRC("123456789") =
0x29B1`, `CRC(0^15) = 0x4EC3`). It is the only thing a medium may inspect.

### The five laws

Every medium codec satisfies, and every language's cert checks:

1. **Lossless** — `decode(encode(F)).frames == F` for every sequence `F` of valid frames.
2. **Order-preserving** — frames come out in the order they went in; no medium reorders.
3. **Resync** — for the self-delimiting media (`stream`, `hex`),
   `decode(garbage ‖ encode(F) ‖ garbage)` yields exactly `F` (modulo a false positive
   of probability 2⁻²⁰ per offset: sync byte ∧ version nibble ∧ 16-bit CRC).
4. **Frame gate** — the only validity test is the gate above; a medium never invents,
   repairs or drops a frame that passes it.
5. **Media never parse the frame** — beyond the gate, the 17 bytes are opaque (type,
   seq, src, dst, payload, timestamp are never read or rewritten).

## Medium classes

| Class | Medium (URI scheme) | `encode` | `decode` |
|---|---|---|---|
| stream | `file:` (`.dcf`), `stdio:` | concatenated 17-byte frames | **byte-wise scan** (below) |
| text | `hex:` | 34 lowercase hex digits + `\n` per frame | line parser (below) |
| datagram | `udp:dialect=proto` | one 34-byte ProtoMessage per frame | accept iff `msg_type == 12 ∧ payload_len == 17` |
| datagram | `udp:dialect=bare` | consecutive pairs → one 32-byte SuperPack; a lone frame raw (17 B) | 32 B ∧ `is_superpack` → 2 frames; 17 B → 1 frame; else invalid |
| datagram | `l2eth:` | `[n u16 BE][SuperPack × ⌈n/2⌉]`, odd tail paired with the zero filler | `unbatch`; the filler is dropped via `n`; truncated → reject |
| datagram | `loop:` | in-process broadcast | identity |
| analog | `hydra:` | HydraModem **symbol string** (tone indices) | symbols → bits → deinterleave → hard FEC → CRC |
| analog | `afsk:` | AFSK **bit string** | 0x7E search → crc8 check or RS decode |

### stream — `file:` (`.dcf`) and `stdio:`

`encode` concatenates frames. `decode` scans byte-wise:

```
i = 0, skipped = 0
while len - i >= 17:
    if gate(buf[i : i+17]):  emit buf[i : i+17];  i += 17
    else:                    i += 1;  skipped += 1
tail_bytes = len - i            # < 17 trailing bytes; NOT counted in skipped_bytes
```

No COBS/SLIP: the frame is self-delimiting. This is a strict superset of the historical
aligned 17-byte stepping (an aligned clean file decodes identically). A streaming
decoder (`StreamScanner`) keeps a carry of at most 16 bytes between reads, so **any
chunking of the input yields the same frames and `skipped_bytes`**; at EOF the carry is
the `tail_bytes`.

### text — `hex:`

`encode` writes each frame as 34 lowercase hex digits followed by `\n`. `decode` splits
the input on `\n`; from each line strips leading/trailing ASCII whitespace (space, tab,
CR, VT, FF — so CRLF input works); skips blank lines and lines starting with `#`;
accepts uppercase; and counts any other line that is not exactly 34 hex digits as a
**bad line**. Decoded frames are returned raw — `hex_decode` does **not** gate (the
pipeline gate does). Input bytes are read as Latin-1, so any non-hex byte just makes a
bad line.

### datagram — `udp:`

UDP has **two dialects**; a URI picks one (`dialect=proto` is the default). Receivers
are strict: a proto receiver does not parse bare datagrams and vice versa.

**`proto`** — the binary ProtoMessage envelope of the Go/C/Rust/Python mesh nodes
(`go/node/proto.go`, `C_SDK/node/dcf_proto.h`, `rust/src/lib.rs`, `python/dcf/proto.py`):

```
 [0]      msg_type     u8
 [1..4]   sequence     u32 BE
 [5..12]  timestamp    u64 BE
 [13..16] payload_len  u32 BE
 [17..]   payload      payload_len bytes      (header = struct ">BIQI", 17 bytes)
```

A frame travels as `ProtoMessage(msg_type = FRAME = 12, sequence, timestamp,
payload_len = 17, frame)` — **34 bytes**. The sender's `sequence` starts at `seq_start`
(default 1) and increments by one per datagram (mod 2³²); `timestamp` is **0** by
default (`ts=0`, deterministic) or microseconds since the Unix epoch (`ts=now`). The
receiver rejects a short header (< 17 bytes) or a `payload_len` that overruns the
datagram (bytes beyond `payload_len` are ignored), accepts `msg_type == 12` with
`payload_len == 17`, and **passes over** every other type — types 1–11 are adapter
envelopes, not frames on this medium. Registry (normative, all languages):

| id | name | id | name | id | name |
|---|---|---|---|---|---|
| 1 | POSITION | 5 | RELIABLE | 9 | GAME_DCF |
| 2 | AUDIO | 6 | ACK | 10 | TEXT_DCF |
| 3 | GAME_EVENT | 7 | PING | 11 | MESH |
| 4 | STATE_SYNC | 8 | PONG | **12** | **FRAME** |

Cross-language golden vector (pinned in the vectors as `go_golden`): type 1, seq 42,
ts `0x0102030405060708`, payload `010203` →
`010000002a010203040506070800000003010203`.

**`bare`** — the Node / WASM / matrix-bridge dialect (`JS/nodejs/src/node.js`): with
`pair=1` (default) two consecutive frames travel as one 32-byte SuperPack
(`SUPERPACK_SPEC.md`), and a lone frame travels raw as 17 bytes — at end of input, or,
for an infinite input, once it has waited `flush_ms` (default 20 ms) for a partner. With
`pair=0` every frame travels raw. The receiver unpacks a 32-byte datagram that
`is_superpack` and passes the joint CRC into its two frames, takes a 17-byte datagram as
one frame, and counts anything else as invalid.

### datagram — `l2eth:`

The DCF-Snake raw-L2 payload (byte-identical to `hydramodem/dcf-tools/snake_l2.h`):

```
payload = [n_frames u16 BE] [SuperPack(frame 2k, frame 2k+1)] × ⌈n/2⌉
```

An odd trailing frame is paired with the canonical **zero filler** — the valid DATA
frame `encode(0,0,0,0,00000000,0)` = `d310000000000000000000000000005b80` — which the
receiver discards using `n_frames`. A payload shorter than `2 + 32·⌈n/2⌉` is rejected;
trailing bytes after the last SuperPack (Ethernet minimum-size padding) are ignored.
Capacity per payload = `((mtu − 2) / 32) · 2` frames (92 at MTU 1500). A writer fills a
payload up to capacity and flushes a partial batch at end of input (or after `flush_ms`
for an infinite input). Default EtherType `0x88B5` (record plane); `impl=raw` needs
`CAP_NET_RAW`, `impl=loop` is a privilege-free in-process double.

### datagram — `loop:`

An in-process broadcast bus named by `id`: every attached port delivers to every other.
The identity codec; for tests and single-process bridges.

### analog — `hydra:` (HydraModem M-FSK)

Certified **to the symbol stream** of `hydra_frame_build`
(`hydramodem/src/hydra_{profile,frame,conv,fec,interleave,crc}.c` is the ground truth;
the vectors were cross-checked against it). One frame is `total_syms` tone indices,
written as a string with one lowercase hex digit per symbol (`n_tones ≤ 16`):

```
[ preamble_syms: tone 0, tone n_tones-1, tone 0, ... (starts with 0) ]
[ sync_word 0x2DD4, 16 bits MSB-first -> symbols                        ]
[ interleave?( fec( frame17 ‖ CRC-16/CCITT-FALSE(frame17) BE ) ) -> symbols ]
```

Bits map to symbols MSB-first, `bits_per_symbol = log2(n_tones)` bits each, the last
symbol zero-padded. Profiles (user fields of `hydra_profile_default` /
`hydra_profile_aux_cable`):

| profile | sample_rate | baud | n_tones | base_freq | tone_spacing | preamble_syms | sync | fec | interleave |
|---|---|---|---|---|---|---|---|---|---|
| `default` | 48000 | 1000 | 2 | 2000 | 1000 | 24 | 0x2DD4 | conv | 1 |
| `aux` | 48000 | 1200 | 2 | 1200 | 1200 | 16 | 0x2DD4 | conv | 1 |

(The default profile ships **conv FEC + interleave on**; an old header comment claiming
"FEC off" was stale.) Derived fields, exactly as `hydra_profile_init`:
`data_bits = 152` (17 + 2 bytes); `coded_bits` = 152 (none) / 456 (rep3) / 316 (conv =
2·(152+6)); `interleave_stride` = the first `s ≥ ⌈√n⌉` coprime with `n` (13 / 23 / 19),
or 1 when interleave is off; `sync_syms = ⌈16/bps⌉`; `data_syms = ⌈coded_bits/bps⌉`;
`total_syms = preamble_syms + sync_syms + data_syms` — **default 192 / 496 / 356, aux
184 / 488 / 348** (none / rep3 / conv, binary FSK).

FEC: **none**; **rep3** (each bit ×3, majority decode); **conv** — K=7 rate-½,
generators G0 = 0171 (0x79), G1 = 0133 (0x5B) octal, register `reg = (bit << 6) | state`,
outputs `parity(reg & G0), parity(reg & G1)`, `state = reg >> 1`, plus 6 zero tail bits
so the trellis ends in state 0. Interleaver: TX gather `out[i] = in[(i·stride) mod n]`,
RX scatter `out[(i·stride) mod n] = in[i]`.

`decode`: require exactly `total_syms` symbols, each `< n_tones`; skip the preamble by
count; require the 16 sync bits (padding bits are ignored); deinterleave; FEC-decode with
hard decisions (none: identity; rep3: majority; conv: Viterbi with the correlation
metric of the C soft decoder on ±1 values, states then bits in ascending order, strict
`>` for survivors, start and traceback from state 0 — ML on a binary symmetric channel,
so it corrects any ≤ 4 coded-bit errors); then check the CRC-16. Anything else → no
frame. The **waveform** (continuous-phase tones, timing recovery, soft metrics) is not
certified: it is loopback-tested (`hydramodem/`, `hydramodem/dcf-tools/dcf_loopback`).

### analog — `afsk:` (python/modem AFSK)

Certified **to the bit stream** of `python/modem/acoustic_frame.py` (`encode_bits`), one
`0`/`1` character per bit:

```
[ preamble_bits alternating 0,1,0,1,... ][ 0x7E = 01111110 ]
[ payload: frame17 ‖ crc8(frame17)   (fec=0)   |   RS(17+16) codeword (fec=1) ]
[ 16 alternating postamble bits ]
```

`crc8` is poly 0x31, init 0x00, MSB-first, non-reflected (`crc8("123456789") = 0xA2`);
the RS code is the certified DCF-FEC Reed-Solomon (`DCF_FEC_SPEC.md`, 16 parity bytes,
corrects 8 byte errors). `decode` finds the first `01111110` at bit level, then checks
the crc8 or RS-decodes. Profiles (tones/baud are the analog layer; only
`preamble_bits` affects the bit string):

| profile | mark Hz | space Hz | baud | preamble_bits | bits (crc8 / RS) |
|---|---|---|---|---|---|
| `standard` | 1200 | 2200 | 300 | 80 | 248 / 368 |
| `handheld` (default) | 1200 | 1800 | 300 | 240 | 408 / 528 |
| `aux-cable` | 1000 | 1500 | 1200 | 16 | 184 / 304 |

**`hydra` and `afsk` are two different acoustic media.** They differ in tones, baud,
sync word (0x2DD4 vs 0x7E), integrity check (CRC-16 vs CRC-8/RS) and FEC (conv/rep3 vs
RS), so **they do not interoperate** — not even `hydra:profile=aux` with
`afsk:profile=aux-cable` over the same cable. Each is certified separately.

### Directory PHYs (WAV / IQ spools)

The analog media (`hydra:`, `afsk:`, `sdr:`, `janus:`) exchange one file per frame
through directories: a writer publishes `<name>-<n>.<ext>` (`n` = 8-digit decimal counter
from 1 per writer process, `name` defaults to the scheme) atomically (write
`.<name>-<n>.<ext>.tmp`, then rename); a reader processes files ending in `.<ext>` that
do not start with `.`, in lexicographic order, each once. Two peers form a link by
sharing a directory (A's `out=` is B's `in=`). `hydra:` WAVs are produced by the
`frame_tx`/`frame_rx` tools (`impl=tool`, the default) or in-process by
`libhydramodem` (`impl=cffi`); they are loopback-tested, not byte-certified.

## Certification tiers

| Tier | Media | What is pinned |
|---|---|---|
| **Byte-certified** (`medium_vectors.json`) | `stream`, `hex`, `udp_proto`, `udp_bare`, `l2eth`, `hydra_symbols`, `afsk_bits` | every byte / symbol / bit of the representation, both directions |
| **Loopback-tested** | HydraModem WAV, AFSK WAV, SDR `.cf32`, JANUS WAV, C DCFM modem | frame in = frame out over the waveform, per implementation |
| **Deferred to v0.2** | `sdr_bytes` family (+ SDR outside Python), live tcp / serial / ws media, live audio devices (the `python/modem/main.py` 15-byte frame is **non-conforming** until ported), gRPC / GNS, `libhydramodem` linked into the C `punctim` | — |

Certification stops at the symbol/bit stream for the analog media — the same line the
repo already draws for Opus, the PM synth and quanta: WAV output is not required to be
byte-identical across DSP backends.

## Medium URI grammar

```
URI   = SCHEME [ ":" [ item *( "," item ) ] ]
item  = key "=" value            ; value = any chars except ","  ("=" allowed)
```

The scheme is case-insensitive; keys are validated per scheme (an unknown key or an item
without `=` is a usage error); empty items are ignored; a repeated key: the last wins;
`|` separates multiple values inside one value (`peer=a:1|b:2`); every scheme accepts
`name=`. Integers are decimal or `0x`-hex; booleans are `0|1` (also `true|false`).
Single-sourced in `python/dcf/medium.py:parse_uri` and mirrored by every `punctim`.

| Scheme | Keys (default) | Class | `io` input is |
|---|---|---|---|
| `file:` | `path=` (required), `append=0\|1` (0), `follow=0\|1` (0), `mode=r\|w\|rw` (from the side); legacy `in=`/`out=` | stream | finite unless `follow=1` |
| `stdio:` | — | stream | finite |
| `hex:` | `path=` (stdin / stdout when absent), `append=0\|1` (0), `follow=0\|1` (0), `mode=` | text | finite unless `follow=1` |
| `udp:` | `dialect=proto\|bare` (proto), `bind=host:port` (0.0.0.0:0; required as input), `peer=host:port\|…` (required as output; legacy `id@host:port` accepted), `pair=0\|1` (1), `flush_ms=` (20), `ts=0\|now` (0), `seq_start=` (1) | datagram | infinite |
| `l2eth:` | `if=` (—), `ethertype=` (0x88B5), `dst=` MAC (ff:ff:ff:ff:ff:ff), `mtu=` (1500), `impl=raw\|loop` (raw if `if=` given, else loop), `id=` (l2eth; loop bus), `flush_ms=` (20) | datagram | infinite |
| `loop:` | `id=` (default) | datagram | infinite |
| `hydra:` | `in=`/`out=` dirs, `profile=default\|aux` (default), `fec=none\|rep3\|conv` (conv), `interleave=0\|1` (1), `base_freq=`, `tone_spacing=`, `baud=`, `n_tones=` (from the profile), `impl=tool\|cffi` (tool), `tx=`/`rx=` (`$HYDRA_TX`/`$HYDRA_RX`/PATH `frame_tx`/`frame_rx`) | analog | infinite |
| `afsk:` | `in=`/`out=` dirs, `profile=standard\|handheld\|aux-cable` (handheld), `fec=0\|1` (0) | analog | infinite |
| `audio:` | alias of `afsk:` (historical `dcf-bridge` name) | analog | infinite |
| `sdr:` | `in=`/`out=` dirs, `mod=` (gfsk) | analog (loopback-tested) | infinite |
| `janus:` | `in=`/`out=` dirs, `pset=` (1), `fs=` (48000), `pset_file=`, `tx=`/`rx=` | analog (loopback-tested) | infinite |
| `mc:` | `rcon=host:port`, `pass_file=`, `fifo=PATH`, `log=PATH`, `bot=ARGV`, `egress=console\|chat`, `ns=dcf`, `poll_hz=4` — a Minecraft world's DeModFrame register (`DCF_MINECRAFT_SPEC.md`); **Python-only** (other `punctim`s exit 3) | register | infinite |

Every historical `dcf-bridge -t` string keeps working (`udp:bind=,peer=`, `audio:`,
`sdr:`, `janus:`, `hydra:`, `file:in=,out=`); a *bridge* `file:` port keeps its
historical defaults (`follow=1`, `append=1`). As a `punctim io` side, a medium needs its
direction's key (`bind=`/`in=` as input, `peer=`/`out=` as output), else exit 2. A
`hydra:` option the installed tools cannot honour (e.g. `interleave=0` on a `frame_tx`
without `--interleave`) is "unsupported in this build" (exit 3); `profile=aux` on such a
tool falls back to the explicit tone flags `--base-freq 1200 --tone-spacing 1200 --baud
1200`.

## The `punctim` CLI contract

Identical in every Tier-A language (Python, C, Rust, Go, Node). `punctim` is the
*medium* tool; the existing node verbs (`dcfnode start|send-*`, `dcf mesh`,
`dcf-bridge`, `node.js recv|send`) remain the *adapter/mesh* tools.

```
punctim version [--json]
punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
                [--no-validate] [--stats] [--queue N]
punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
punctim decode  (HEX | --stdin) [--json]
punctim certify [--vectors DIR] [--family NAME ...]
punctim sim     ...                                      (Python only; see WP-Sim)
```

### `io`

A **single-threaded, ordered pipeline** `reader → gate → writer`; it never reorders.

- **Finite inputs** (`stdio:`, `hex:` and `file:` without `follow`) are read to EOF,
  every frame is written, the writer is flushed (including a pending bare-UDP lone frame
  and a partial l2eth batch), nothing is ever dropped; exit 0.
- **Infinite inputs** (`udp:`, `l2eth:`, `loop:`, the directory PHYs, `follow=1`) run
  until `--count` frames were written, `--seconds` elapsed, or SIGINT; with `--expect N`
  and no `--count`, they also stop once N frames were written. Received frames cross a
  bounded FIFO of `--queue` items (default **256**); on overflow the oldest is shed and
  counted in `dropped`. Frames already queued when the run ends are still written.
- **Gate:** a frame failing the gate is counted in `invalid_frames` and not written.
  `--no-validate` passes it through (a debugging aid; determinism is normative only for
  valid frames; media that cannot carry an invalid frame — SuperPack pairing, l2eth —
  send it raw or count it invalid). Medium units that carry no frame (an undecodable
  datagram / payload) are also counted in `invalid_frames`.
- `--count N` stops after N frames written (any input). `--expect N` makes the exit
  code **6** unless exactly N frames were written.
- `--stats` prints **one JSON line on stderr** with exactly these keys, in this order:
  `in`, `out` (the URIs as given), `frames_in` (frames read, before the gate),
  `frames_out` (frames written), `invalid_frames`, `skipped_bytes` (stream resync,
  excluding `tail_bytes`), `bad_lines` (hex), `dropped` (queue sheds), `seconds`
  (wall time, 3 decimals).

**Determinism rule (normative):** *for finite inputs, `punctim io` in any language
produces byte-identical output for identical input and URI; `udp:proto` needs `ts=0`
(the default).* "Output" is the written file / stdout bytes, or the datagram sequence
for `udp:` (proto: `ProtoMessage(12, seq_start + i, 0, 17, frame_i)`; bare:
`bare_encode(frames)`), or the payload sequence for `l2eth:`.

### `encode` / `decode`

`encode` mirrors `wirelab_core.encode(frame_type, seq, src, dst, payload, ts_us)` and
prints 34 lowercase hex digits + `\n`. Integers are decimal or `0x`-hex; `--type` 0–15,
`--seq/--src/--dst` 0–65535, `--ts` is taken modulo 2²⁴; `--payload` is exactly 8 hex
digits; `--text` is at most 4 UTF-8 bytes, right-padded with `00`.

`decode` takes one hex frame (or, with `--stdin`, one per line; blank and `#` lines are
skipped) and prints one record per frame. With `--json` each record is one compact JSON
object (no insignificant whitespace) with the keys, in order: `hex`, `valid`,
`syndrome`, `frame_type`, `frame_type_name`, `seq`, `src`, `dst`, `payload` (8 hex
digits), `ts_us`, `crc`, plus `error` (a reason string) when `valid` is false — i.e. the
`wirelab_core.decode()` keys plus `valid`/`syndrome`/`hex`. For a 17-byte word the fields
are read raw even when the gate fails; for input that is not 17 bytes of hex the record
is `{"hex", "valid": false, "error"}`. Any invalid frame → exit **5**.

### `certify`, `version`

`certify` runs the implementation's medium codecs against `medium_vectors.json`
(`--vectors DIR`, else `$PUNCTIM_VECTORS`, else `Documentation/` found by walking up
from the executable, else a packaged copy), printing `PASS <family> (<n> cases)` /
`FAIL <family>: <reason>` lines; `--family` restricts the run. Exit 0 iff all pass,
else **4**. `version` prints `punctim <version> (<impl>)`; `--json` prints
`{"name":"punctim","version":…,"impl":…,"media":[schemes],"families":[families]}`.

### Exit codes (all languages)

| code | meaning |
|---|---|
| 0 | ok |
| 1 | I/O error (missing file, socket error, broken pipe) |
| 2 | usage (bad flag, bad URI, unknown scheme/key, missing direction key) |
| 3 | medium unsupported in this build (no numpy for `afsk:`, no HydraModem tools, no janus-c, an unsupported tool option) |
| 4 | certification failed |
| 5 | invalid frame given to `decode` |
| 6 | `--expect N` not met |

Test harnesses discover the binaries through `PUNCTIM_PY`, `PUNCTIM_C`, `PUNCTIM_RS`,
`PUNCTIM_GO`, `PUNCTIM_JS`; `PUNCTIM_VECTORS` overrides the vector directory.

```sh
printf 'd31312340001ffffdeadbeefab12cd24c0\n' | punctim io --in hex: --out file:path=a.dcf
punctim io --in file:path=a.dcf --out hex:                       # -> the same line
punctim io --in udp:bind=0.0.0.0:9100 --out hex: --expect 1 --seconds 5 &
punctim io --in file:path=a.dcf --out udp:peer=127.0.0.1:9100    # proto dialect, ts=0
punctim io --in hex:path=f.hex --out hydra:out=/tmp/tones,profile=aux
```

## `medium_vectors.json`

Generated by `python3 python/MCP/gen_medium_vectors.py <out.json>` (which first asserts
the laws, then writes `<out.json>` and `<dir>/medium_vectors.gen.h`); committed as
identical `Documentation/medium_vectors.json` and `python/MCP/medium_vectors.json`, and
`codec/medium_vectors.gen.h`. Hex strings are lowercase.

```
version: 1
anchors: { crc_123456789: 0x29B1, crc_zero15: 0x4EC3, frame_len: 17,
           proto_header_len: 17, msg_frame: 12, super_len: 32, l2_hdr: 2,
           l2_filler: "d310…5b80", hydra_sync_word: 0x2DD4, afsk_sync: 0x7E,
           afsk_crc8_123456789: 0xA2 }
basis:   [ { id, type, seq, src, dst, payload(hex), ts, hex } × 6 ]   # = gen_superpack_vectors._FRAMES
families:
  stream:        { cases: [ { name, input(hex), frames[hex], skipped_bytes, tail_bytes } ] }        # 11
  hex:           { cases: [ { name, frames[hex], text, decode_input, decoded[hex], bad_lines } ] }  # 8
                   (text == hex_encode(frames); hex_decode(decode_input) == (decoded, bad_lines); frames == decoded)
  udp_proto:     { header_len: 17, msg_frame: 12, types: {NAME: id},
                   cases: [ { name, type, seq, ts, ts_hex, payload(hex), datagram(hex), accept_as_frame } ] }  # 21
                   (ts_hex = 16 hex digits of ts, for languages whose JSON numbers are doubles)
  udp_bare:      { cases: [ { name, frames[hex], datagrams[hex] } ] }                               # 7 (0,1,2,3,6 frames + 2 reserved-type)
  l2eth:         { hdr: 2, filler(hex), cases: [ { name, frames[hex], payload(hex) } ] }             # 5 (1..4 frames + 1 reserved-type)
  hydra_symbols: { profiles: { default|aux: { sample_rate, baud, n_tones, base_freq, tone_spacing,
                                               preamble_syms, sync_word, fec_mode, interleave, tx_gain } },
                   cases: [ { name, profile, fec: "none"|"rep3"|"conv", interleave: 0|1, n_tones, frame(hex),
                              coded_bits, interleave_stride, total_syms, symbols } ] }             # 74
  afsk_bits:     { profiles: { standard|handheld|aux-cable: { mark, space, baud, preamble_bits } },
                   cases: [ { name, profile, fec: bool, frame(hex), n_bits, bits } ] }              # 36
```

Case plan: stream = aligned (1, 3), garbage prefix / between / suffix, truncated tail,
fake sync with a bad CRC, `D3 1x` inside a payload, only garbage, all six; hex = one,
all six, uppercase + CRLF, comments + blank lines, bad lines, no trailing newline, an
ungated (CRC-failing) line; udp_proto = types 1–11 (`accept_as_frame: false`), type 12 for
each basis frame (seq 1–6, ts 0), type 12 with a non-zero ts, type 12 with a 16-byte
payload (rejected), and the Go golden vector; hydra = 6 frames × {default, aux} ×
{none, rep3, conv} × {interleave 0, 1} = 72, plus 2 with `n_tones = 4` (default, conv,
interleave 1; 190 symbols); afsk = 6 frames × 3 profiles × {crc8, RS} = 36.

**Cert obligations** (each language): stream — `decode(input)` equals
`(frames, skipped_bytes, tail_bytes)`, also when fed in chunks; hex — both directions;
udp_proto — encode, decode and `accept_as_frame`; udp_bare — encode and decode;
l2eth — batch and unbatch; hydra — the derived fields, `encode == symbols`,
`decode(symbols) == frame`; afsk — `encode == bits`, `decode(bits) == frame`.

`codec/medium_vectors.gen.h` mirrors the JSON for dependency-free C tests:
`MED_FRAME_LEN 17`, `MED_PROTO_HDR 17`, `MED_MSG_FRAME 12`, `MED_SUPER_LEN 32`,
`MED_L2_HDR 2`, `MED_HYDRA_SYNC 0x2DD4u`, `MED_AFSK_SYNC 0x7Eu`,
`MED_AFSK_CRC8_123456789`, `MED_CRC_123456789`, `MED_CRC_ZERO15`; `MED_L2_FILLER[17]`,
`MED_BASIS[6][17]`; the case tables `MED_STREAM_CASES` (`med_stream_case_t`),
`MED_HEX_CASES` (`med_hex_case_t`), `MED_PROTO_CASES` (`med_proto_case_t`),
`MED_BARE_CASES` (`med_bare_case_t`), `MED_L2_CASES` (`med_l2_case_t`),
`MED_HYDRA_CASES` (`med_hydra_case_t`; `fec` 0 none / 1 rep3 / 2 conv — the C enum
order), `MED_AFSK_CASES` (`med_afsk_case_t`; `fec` 0/1) with counts `MED_N_STREAM`,
`MED_N_HEX`, `MED_N_PROTO`, `MED_N_BARE`, `MED_N_L2`, `MED_N_HYDRA`, `MED_N_AFSK`; and
the profile tables `MED_HYDRA_PROFILES[2]` (`med_hydra_profile_t`) and
`MED_AFSK_PROFILES[3]` (`med_afsk_profile_t`).

```sh
python3 python/MCP/mediumlab_core.py                                  # self-test
python3 python/MCP/gen_medium_vectors.py /tmp/mv.json                 # regen + verify laws
diff /tmp/mv.json Documentation/medium_vectors.json
diff /tmp/mv.json python/MCP/medium_vectors.json
diff /tmp/medium_vectors.gen.h codec/medium_vectors.gen.h
python3 python/punctim.py certify                                     # Python cert
cd python && python3 -m unittest tests.test_medium -v                 # laws + io round trips
```

## Reference implementations

| Lang | Codec | Cert | `punctim` |
|------|-------|------|-----------|
| Python (canonical) | `python/MCP/mediumlab_core.py` | `python/tests/test_medium.py`, `punctim certify` | `python/punctim.py` (runtime `python/dcf/medium.py`) |
| C | `codec/demod_medium.h` | `C_SDK/tests/test_medium_certify.c` | `C_SDK/node/punctim.c` |
| Rust | `codec/src/medium.rs` | `codec/tests/certify_medium.rs` | `codec/src/bin/punctim.rs` |
| Go | `go/medium/medium.go` | `go/medium/medium_certify_test.go` | `go/cmd/punctim` |
| Node.js | `JS/nodejs/src/medium.js` | `JS/nodejs/test/certify_medium.js` | `JS/nodejs/bin/punctim.js` |
| HydraModem | `hydramodem/src/hydra_frame.c` (ground truth) | `hydramodem/dcf-tools/hydra_symbols_certify.c` | — |

The C, Rust, Go and Node entries are the planned ports of this reference; a port is
conforming when its cert passes every family it implements (C++/Java/Perl implement the
digital families only; Kotlin/Swift/Haskell/Lua/Lisp are deferred).

## Theorem

Each medium codec is a fixed, input-independent transformation composed of byte
concatenation, fixed-layout headers, the certified SuperPack container, and (for the
analog media) fixed bit permutations and linear codes over GF(2)/GF(2⁸) followed by the
certified CRC. Matching `medium_vectors.json` therefore pins an implementation to the
reference on the entire input space of valid frame sequences, and because no medium
reads the frame past the gate, the 246-vector wire certificate and every adapter
certificate are untouched — adding a medium never re-opens them.

## Security & export posture

Media add no cryptography and remove none: every representation here is **plaintext**
(`DCF_SECURITY_EXPOSURE.md`). Deploy behind WireGuard or operator-supplied,
export-compliant crypto **beneath** the medium (under the UDP socket, on the Ethernet
segment); never inside a codec.

## `punctim sim` — sizing a system (Python only)

`punctim sim` answers "what medium, and what hardware, does this system need?" from the same
certified codecs this spec defines. It is stdlib-only (`python/dcf/sim/`: `media.py`,
`traffic.py`, `plan.py`), reads a system description (`--spec system.json` or flags: nodes,
primary medium + candidates, latency target, traffic per adapter, MAC, guard, node power) and
prints a report whose every row is tagged:

- **EXACT** — arithmetic on the certified codecs: airtime/frame = `total_syms / baud` (hydra:
  0.356 s default conv, 0.192 s none, 0.290 s aux conv) or `n_bits / baud` (afsk: 1.36 s
  handheld crc8, 0.153 s aux-cable); datagram bytes (ProtoMessage 34 B + 28 B IPv4/UDP;
  SuperPack 32 B per pair / 17 B lone; l2eth `2 + 32·⌈n/2⌉` + 14 B Ethernet, padded to 46 B);
  adapter fragmentation `1 + ⌈len/4⌉` via each adapter's certified packetizer; frames/s and duty
  cycle per candidate; the sense TDMA slot (airtime + 2 × guard) and cycle, and FDMA channels
  (`dcf/sense/mac.py`); Pipe chunks, `⌈N/W⌉` rounds and data-lane bytes; `OutboundQueue(256)`
  time-to-overflow.
- **MODEL** — a stated basis the certificate cannot give: link rates mirrored from
  `lua/dcf_profile.lua` `M.media` (the tests parse the Lua and assert equality), the measured
  HydraModem airtime (= exact symbol time + 40 ms lead/tail, asserted for every FEC mode),
  node energy and the LoRa / RS-485 comparison (`dcf/sense/model.py`), hardware class
  (`mcu` < `sbc-1core` < `desktop`) and the `DCF_FIELD_USE.md` tier.

A candidate passes iff duty < 80 %, one message of each interactive adapter fits the latency
target, the 13 600 bit/s live-voice floor (`2 × 17 B × 50 × 8`) holds when audio is requested,
and on a shared channel the sense TDMA cycle fits its interval. The recommendation is the
passing candidate with the cheapest hardware class, then the least link load. `--json` emits
`{"exact", "model", "recommendation"}`. Exit 0 ok (also when nothing passes) · 1 I/O · 2 bad
input.

```sh
python3 python/punctim.py sim --spec tests/sim_example.json [--json]
python3 python/punctim.py sim --nodes 200 --sense-interval 60 --mac fdma \
    --candidates 'hydra:profile=default,fec=conv;afsk:profile=handheld'
cd python && python3 -m unittest tests.test_sim -v
```
