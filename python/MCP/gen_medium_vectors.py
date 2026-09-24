# SPDX-License-Identifier: LGPL-3.0-only
"""Executable laws + golden-vector generator for DCF-Medium (the medium codecs).

A medium codec carries the 17-byte DeModFrame quantum over one medium — a .dcf stream,
hex lines, a UDP datagram (ProtoMessage "proto" dialect or bare/SuperPack), a raw-L2
Ethernet payload, the HydraModem M-FSK symbol stream, or the AFSK bit stream — without
parsing it beyond the frame gate. This generator first asserts the medium laws (lossless,
order-preserving, byte-wise resync, frame gate, FEC-correcting symbol codecs), then emits
the finite vectors every language certifies against byte-for-byte. The 246-vector wire
certificate is untouched. Spec: Documentation/DCF_MEDIUM_SPEC.md.

Usage:  python3 gen_medium_vectors.py [medium_vectors.json]
  Writes  <path>                        (the JSON certificate)
  and     <dir>/medium_vectors.gen.h    (dependency-free C test header)
Exit 0 iff every law holds. Commit identical JSON copies to Documentation/ and
python/MCP/, and the header to codec/medium_vectors.gen.h.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wirelab_core import encode, crc16_ccitt, FRAME_LEN  # noqa: E402
import superpack  # noqa: E402
import mediumlab_core as M  # noqa: E402

ok = lambda name: print(f"  PASS  {name}")

# The 6 basis frames = gen_superpack_vectors._FRAMES (frame_type, seq, src, dst, payload, ts_us)
_FRAMES = [
    (0, 0x0000, 0x0000, 0x0000, b"\x00\x00\x00\x00", 0x000000),
    (3, 0x1234, 0x0001, 0xFFFF, b"\xde\xad\xbe\xef", 0xAB12CD),
    (0, 0x0102, 0x0304, 0x0506, b"\xca\xfe\xba\xbe", 0x010203),
    (1, 0x7FFF, 0xA1A1, 0x00B2, b"ping", 0x0000FF),
    (2, 0x00A0, 0x1000, 0x2000, b"\xff\x00\xff\x00", 0xFFFFFF),
    (3, 0xFFFF, 0xFFFF, 0xFFFF, b"\xff\xff\xff\xff", 0xFFFFFF),
]
B = [encode(*t) for t in _FRAMES]
for f in B:
    assert M.gate(f)


def hx(frames):
    return [f.hex() for f in frames]


# ══ anchors ═══════════════════════════════════════════════════════════════════
af = M._afsk()
assert crc16_ccitt(b"123456789") == 0x29B1 and crc16_ccitt(bytes(15)) == 0x4EC3
anchors = {
    "crc_123456789": 0x29B1,
    "crc_zero15": 0x4EC3,
    "frame_len": FRAME_LEN,
    "proto_header_len": M.PROTO_HEADER_LEN,
    "msg_frame": M.MSG_FRAME,
    "super_len": superpack.SUPER_LEN,
    "l2_hdr": M.L2_HDR,
    "l2_filler": M.L2_FILLER.hex(),
    "hydra_sync_word": 0x2DD4,
    "afsk_sync": af.SYNC_WORD,
    "afsk_crc8_123456789": af.crc8(b"123456789"),
}
assert anchors["proto_header_len"] == 17 and anchors["msg_frame"] == 12
assert anchors["afsk_sync"] == 0x7E
ok(f"anchors: CRC 0x29B1/0x4EC3, proto header 17, MSG_FRAME 12, SuperPack 32, "
   f"afsk crc8(123456789)=0x{anchors['afsk_crc8_123456789']:02X}")

basis = [{"id": i, "type": t[0], "seq": t[1], "src": t[2], "dst": t[3],
          "payload": t[4].hex(), "ts": t[5], "hex": B[i].hex()} for i, t in enumerate(_FRAMES)]


# ══ stream ════════════════════════════════════════════════════════════════════
# a valid frame whose payload holds "D3 1x" (a false sync window overlaps it)
D3IN = encode(0, 0x0001, 0x0002, 0x0003, b"\xd3\x10\x00\x00", 0x000004)
assert M.gate(D3IN) and not M.gate(D3IN[8:] + bytes(8))
FAKE = bytearray(B[3])
FAKE[16] ^= 0x01                      # D3 11 ... with a wrong CRC: a fake sync window
FAKE = bytes(FAKE)
assert FAKE[0] == 0xD3 and FAKE[1] >> 4 == 1 and not M.gate(FAKE)

stream_inputs = [
    ("aligned_1", B[1]),
    ("aligned_3", B[0] + B[1] + B[2]),
    ("garbage_prefix", b"\x00\x11\x22garbage" + B[1] + B[2]),
    ("garbage_between", B[1] + b"\xd3\x1f\x00\xffnoise\x00" + B[2]),
    ("garbage_suffix", B[1] + B[4] + b"\xde\xad"),
    ("truncated_tail", B[1] + B[2][:10]),
    ("fake_sync_bad_crc", FAKE + B[3]),
    ("d3_inside_payload", b"\xd3" + D3IN + B[2]),
    ("only_garbage", bytes((i * 29 + 7) & 0xFF for i in range(40))),
    ("all_six", b"".join(B)),
]
stream_cases = []
for name, buf in stream_inputs:
    fr, sk, tail = M.stream_decode(buf)
    # chunking-invariance law: every chunk size gives the same frames/skipped/tail
    for step in range(1, 36):
        sc = M.StreamScanner()
        got = []
        for i in range(0, len(buf), step):
            got += sc.feed(buf[i:i + step])
        assert got == fr and sc.skipped_bytes == sk and len(sc.flush()) == tail, (name, step)
    assert len(fr) <= 6
    assert all(M.gate(f) for f in fr)
    stream_cases.append({"name": name, "input": buf.hex(), "frames": hx(fr),
                         "skipped_bytes": sk, "tail_bytes": tail})
exp = {c["name"]: c for c in stream_cases}
# resync laws: garbage || encode(F) || garbage decodes to exactly F
assert exp["aligned_3"]["frames"] == hx(B[:3]) and exp["aligned_3"]["skipped_bytes"] == 0
assert exp["garbage_prefix"]["frames"] == hx([B[1], B[2]])
assert exp["garbage_prefix"]["skipped_bytes"] == 10
assert exp["garbage_between"]["frames"] == hx([B[1], B[2]])
assert exp["garbage_suffix"]["frames"] == hx([B[1], B[4]]) and exp["garbage_suffix"]["tail_bytes"] == 2
assert exp["truncated_tail"]["frames"] == hx([B[1]]) and exp["truncated_tail"]["tail_bytes"] == 10
assert exp["fake_sync_bad_crc"]["frames"] == hx([B[3]])
assert exp["fake_sync_bad_crc"]["skipped_bytes"] == 17
assert exp["d3_inside_payload"]["frames"] == hx([D3IN, B[2]])
assert exp["d3_inside_payload"]["skipped_bytes"] == 1
assert exp["only_garbage"]["frames"] == [] and exp["only_garbage"]["tail_bytes"] == 16
assert exp["all_six"]["frames"] == hx(B)
assert M.stream_encode(B) == b"".join(B)
ok(f"stream: {len(stream_cases)} cases — lossless, byte-wise resync, fake-sync + "
   f"D3-in-payload handled, chunk-invariant (1..35-byte chunks)")


# ══ hex ═══════════════════════════════════════════════════════════════════════
BADF = bytearray(B[2])
BADF[5] ^= 0x40                        # 34 hex digits, but fails the frame gate
BADF = bytes(BADF)
assert not M.gate(BADF)
hex_inputs = [
    ("one", M.hex_encode([B[1]])),
    ("all_six", M.hex_encode(B)),
    ("uppercase_crlf", B[1].hex().upper() + "\r\n" + B[2].hex().upper() + "\r\n"),
    ("comments_blank", "# DCF hex capture\n\n  " + B[3].hex() + "  \n#" + B[0].hex()
     + "\n\t" + B[4].hex() + "\n\n"),
    ("bad_lines", B[1].hex() + "\nzz" + B[1].hex()[2:] + "\n" + B[2].hex()[:33] + "\n"
     + B[2].hex() + "\n" + B[3].hex() + "00\n" + "d3 13" + B[1].hex()[4:] + "\n"),
    ("no_trailing_newline", B[5].hex()),
    ("ungated_line", B[1].hex() + "\n" + BADF.hex() + "\n"),
]
hex_cases = []
for name, text in hex_inputs:
    decoded, bad = M.hex_decode(text)
    canon = M.hex_encode(decoded)
    assert M.hex_decode(canon) == (decoded, 0)            # canonical form is a fixed point
    assert len(decoded) <= 6
    hex_cases.append({"name": name, "frames": hx(decoded), "text": canon,
                      "decode_input": text, "decoded": hx(decoded), "bad_lines": bad})
hexp = {c["name"]: c for c in hex_cases}
assert hexp["one"]["decode_input"] == hexp["one"]["text"] == B[1].hex() + "\n"
assert hexp["all_six"]["decoded"] == hx(B)
assert hexp["uppercase_crlf"]["decoded"] == hx([B[1], B[2]])
assert hexp["comments_blank"]["decoded"] == hx([B[3], B[4]])
assert hexp["bad_lines"]["decoded"] == hx([B[1], B[2]]) and hexp["bad_lines"]["bad_lines"] == 4
assert hexp["no_trailing_newline"]["decoded"] == hx([B[5]])
assert hexp["ungated_line"]["decoded"] == hx([B[1], BADF])  # hex_decode never gates
ok(f"hex: {len(hex_cases)} cases — lossless, CRLF/uppercase/comments/blank accepted, "
   f"bad lines counted, not gated")


# ══ udp_proto ═════════════════════════════════════════════════════════════════
proto_cases = []


def _proto_case(name, t, seq, ts, payload):
    dg = M.proto_encode(t, seq, ts, payload)
    assert M.proto_decode(dg) == (t, seq, ts, payload)
    fr = M.proto_frame_decode(dg)
    acc = fr is not None
    assert acc == (t == M.MSG_FRAME and len(payload) == FRAME_LEN)
    assert len(payload) <= 64 and len(dg) <= 128
    proto_cases.append({"name": name, "type": t, "seq": seq, "ts": ts, "ts_hex": f"{ts:016x}",
                        "payload": payload.hex(), "datagram": dg.hex(),
                        "accept_as_frame": acc})


_type_names = {v: k for k, v in M.MSG_TYPES.items()}
for t in range(1, 12):
    pl = b"" if t in (M.MSG_TYPES["PING"], M.MSG_TYPES["PONG"]) else bytes(range(t, 2 * t))
    _proto_case(f"type_{t}_{_type_names[t].lower()}", t, t, 0, pl)
for i, f in enumerate(B):
    _proto_case(f"frame_b{i}", M.MSG_FRAME, i + 1, 0, f)
    assert M.proto_frame_encode(f, i + 1) == bytes.fromhex(proto_cases[-1]["datagram"])
_proto_case("frame_ts_now", M.MSG_FRAME, 7, 0x00060A1B2C3D4E5F, B[1])
_proto_case("frame_wrong_len", M.MSG_FRAME, 8, 0, B[1][:16])
_proto_case("go_golden", 1, 42, 0x0102030405060708, b"\x01\x02\x03")
assert proto_cases[-1]["datagram"] == "010000002a010203040506070800000003010203"
assert proto_cases[-1]["ts"] == 72623859790382856
for bad in (bytes(16), bytes.fromhex(proto_cases[-1]["datagram"])[:-1]):
    try:
        M.proto_decode(bad)
        raise AssertionError("header guard missed")
    except ValueError:
        pass
ok(f"udp_proto: {len(proto_cases)} cases — types 1..11 pass through, MSG_FRAME=12 carries "
   f"exactly one 17-B frame in 34 B, Go/C golden vector, header guards")


# ══ udp_bare ══════════════════════════════════════════════════════════════════
bare_cases = []
for n in (0, 1, 2, 3, 6):
    fs = B[:n]
    dgs = M.bare_encode(fs)
    assert [len(d) for d in dgs] == [32] * (n // 2) + [17] * (n % 2)
    assert [f for d in dgs for f in M.bare_decode(d)] == fs
    bare_cases.append({"name": f"frames_{n}", "frames": hx(fs), "datagrams": hx(dgs)})
assert M.bare_decode(bytes(33)) == [] and M.bare_decode(bytes(16)) == []
tam = bytearray(M.bare_encode(B[:2])[0])
tam[7] ^= 1
assert M.bare_decode(bytes(tam)) == []
ok(f"udp_bare: {len(bare_cases)} cases — pairs -> 32-B SuperPack, lone frame raw 17 B, "
   f"lossless, tampered SuperPack rejected")


# ══ l2eth ═════════════════════════════════════════════════════════════════════
l2_cases = []
for n in (1, 2, 3, 4):
    fs = B[1:1 + n]
    pl = M.l2_batch(fs)
    assert len(pl) == M.L2_HDR + ((n + 1) // 2) * 32 <= 130
    assert int.from_bytes(pl[:2], "big") == n
    assert M.l2_unbatch(pl) == fs
    assert M.l2_unbatch(pl + b"\x00" * 7) == fs        # Ethernet min-size padding ignored
    try:
        M.l2_unbatch(pl[:-1])
        raise AssertionError("truncated batch accepted")
    except ValueError:
        pass
    if n % 2:
        assert superpack.unpack(pl[-32:])[1] == M.L2_FILLER
    l2_cases.append({"name": f"frames_{n}", "frames": hx(fs), "payload": pl.hex()})
assert M.l2_capacity(1500) == 92 and M.l2_capacity(9000) == 562
ok(f"l2eth: {len(l2_cases)} cases — [n u16][SuperPack*ceil(n/2)], zero-DATA filler on odd n, "
   f"truncation rejected")


# ══ hydra_symbols ═════════════════════════════════════════════════════════════
def _flip_coded(p, symbols, positions):
    """Flip coded bits (in transmitted order) at `positions` in a symbol string."""
    bps = p["bits_per_symbol"]
    head = p["preamble_syms"] + p["sync_syms"]
    syms = [int(c, 16) for c in symbols]
    bits = M.hydra_symbols_to_bits(syms[head:], bps)
    for q in positions:
        bits[q] ^= 1
    return symbols[:head] + "".join("0123456789abcdef"[s]
                                    for s in M.hydra_bits_to_symbols(bits, bps))


hydra_profiles = {name: {k: M.HYDRA_PROFILES[name][k] for k in M.HYDRA_USER_FIELDS}
                  for name in ("default", "aux")}
ANCH = {("default", "none"): 192, ("default", "rep3"): 496, ("default", "conv"): 356,
        ("aux", "none"): 184, ("aux", "rep3"): 488, ("aux", "conv"): 348}
CODED = {"none": 152, "rep3": 456, "conv": 316}
STRIDE = {"none": 13, "rep3": 23, "conv": 19}
hydra_cases = []
flip_checks = 0


def _hydra_case(name, prof, fec, il, nt, bi):
    global flip_checks
    p = M.hydra_profile(prof, fec=fec, interleave=il, n_tones=nt)
    f = B[bi]
    s = M.hydra_symbols_encode(p, f)
    assert len(s) == p["total_syms"]
    assert M.hydra_symbols_decode(p, s) == f, name
    # preamble / sync shape
    assert all(int(c, 16) == ((nt - 1) if k & 1 else 0) for k, c in enumerate(s[:p["preamble_syms"]]))
    # FEC laws on the symbol stream
    cb = p["coded_bits"]
    if fec == "conv":
        for k in (1, 2, 3):
            pos = sorted({(bi * 37 + j * 101 + k * 13) % cb for j in range(k)})
            assert M.hydra_symbols_decode(p, _flip_coded(p, s, pos)) == f, (name, pos)
            flip_checks += 1
    elif fec == "rep3":
        # one flip in each of three different repetition triples (post-deinterleave)
        st = p["interleave_stride"]
        pos = []
        for trip in (3, 50, 140):
            src = 3 * trip + (bi % 3)                       # coded-order index
            pos.append(next(i for i in range(cb) if (i * st) % cb == src) if il else src)
        assert M.hydra_symbols_decode(p, _flip_coded(p, s, pos)) == f, name
        flip_checks += 1
    else:
        assert M.hydra_symbols_decode(p, _flip_coded(p, s, [bi * 11 % cb])) is None  # detect
        flip_checks += 1
    # a corrupted sync word is rejected
    so = p["preamble_syms"]
    bad = s[:so] + ("1" if s[so] == "0" else "0") + s[so + 1:]
    assert M.hydra_symbols_decode(p, bad) is None
    hydra_cases.append({"name": name, "profile": prof, "fec": fec, "interleave": il,
                        "n_tones": nt, "frame": f.hex(), "coded_bits": cb,
                        "interleave_stride": p["interleave_stride"],
                        "total_syms": p["total_syms"], "symbols": s})
    return p


for bi in range(6):
    for prof in ("default", "aux"):
        for fec in ("none", "rep3", "conv"):
            for il in (0, 1):
                p = _hydra_case(f"b{bi}_{prof}_{fec}_il{il}", prof, fec, il, 2, bi)
                assert p["coded_bits"] == CODED[fec]
                assert p["total_syms"] == ANCH[(prof, fec)]
                assert p["interleave_stride"] == (STRIDE[fec] if il else 1)
for bi in (1, 3):
    p = _hydra_case(f"b{bi}_default_conv_il1_nt4", "default", "conv", 1, 4, bi)
    assert (p["bits_per_symbol"], p["sync_syms"], p["data_syms"], p["total_syms"]) == (2, 8, 158, 190)
assert len(hydra_cases) == 74
ok(f"hydra_symbols: {len(hydra_cases)} cases — coded 152/456/316, stride 13/23/19, total "
   f"default 192/496/356 aux 184/488/348; decode(encode)=id; {flip_checks} FEC checks "
   f"(conv corrects 1..3 flips, rep3 majority, none detects); bad sync rejected")


# ══ afsk_bits ═════════════════════════════════════════════════════════════════
AFSK_LEN = {"standard": (248, 368), "handheld": (408, 528), "aux-cable": (184, 304)}
afsk_profiles = {k: dict(v) for k, v in af.PROFILES.items()}
afsk_cases = []
for bi in range(6):
    for prof in ("standard", "handheld", "aux-cable"):
        for fec in (False, True):
            f = B[bi]
            bits = M.afsk_bits_encode(f, prof, fec)
            assert len(bits) == AFSK_LEN[prof][fec]
            assert M.afsk_bits_decode(bits, prof, fec) == f
            pre = afsk_profiles[prof]["preamble_bits"]
            assert bits[:pre] == "01" * (pre // 2) and bits[pre:pre + 8] == "01111110"
            body = pre + 8 + 8 * 3                   # flip a bit inside frame byte 3
            flipped = bits[:body] + ("1" if bits[body] == "0" else "0") + bits[body + 1:]
            got = M.afsk_bits_decode(flipped, prof, fec)
            assert got == (f if fec else None)       # RS corrects; crc8 detects
            afsk_cases.append({"name": f"b{bi}_{prof}_{'rs' if fec else 'crc8'}",
                               "profile": prof, "fec": fec, "frame": f.hex(),
                               "n_bits": len(bits), "bits": bits})
assert len(afsk_cases) == 36
ok(f"afsk_bits: {len(afsk_cases)} cases — standard 248/368, handheld 408/528, aux-cable "
   f"184/304 bits; decode(encode)=id; RS corrects a flip, crc8 detects it")


# ══ write JSON ════════════════════════════════════════════════════════════════
cert = {
    "version": 1,
    "anchors": anchors,
    "basis": basis,
    "families": {
        "stream": {"cases": stream_cases},
        "hex": {"cases": hex_cases},
        "udp_proto": {"header_len": M.PROTO_HEADER_LEN, "msg_frame": M.MSG_FRAME,
                      "types": dict(M.MSG_TYPES), "cases": proto_cases},
        "udp_bare": {"cases": bare_cases},
        "l2eth": {"hdr": M.L2_HDR, "filler": M.L2_FILLER.hex(), "cases": l2_cases},
        "hydra_symbols": {"profiles": hydra_profiles, "cases": hydra_cases},
        "afsk_bits": {"profiles": afsk_profiles, "cases": afsk_cases},
    },
}

out = sys.argv[1] if len(sys.argv) > 1 else "medium_vectors.json"
with open(out, "w") as fh:
    json.dump(cert, fh, indent=1)
    fh.write("\n")
ncases = sum(len(v["cases"]) for v in cert["families"].values())
print(f"  INFO  wrote {out} ({os.path.getsize(out)} bytes, {ncases} cases)")


# ══ dependency-free C header (mirrors codec/*_vectors.gen.h) ══════════════════
def cbytes(b):
    b = bytes(b)
    if not b:
        return "{0}"
    return "{" + ",".join(f"0x{x:02X}" for x in b) + "}"


def cframes(hexlist):
    if not hexlist:
        return "{{0}}"
    return "{" + ",".join(cbytes(bytes.fromhex(h)) for h in hexlist) + "}"


def cstr(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 0x20 <= o < 0x7F and ch != "?":
            out.append(ch)
        else:
            out.append(f"\\{o:03o}")
    out.append('"')
    return "".join(out)


def cdouble(x):
    return repr(float(x))


hdr_path = os.path.join(os.path.dirname(os.path.abspath(out)), "medium_vectors.gen.h")
L = []
w = L.append
w("/* GENERATED by python/MCP/gen_medium_vectors.py — DO NOT EDIT.")
w(" * DCF-Medium golden vectors (Documentation/DCF_MEDIUM_SPEC.md, medium_vectors.json).")
w(" * hydra fec: 0 none, 1 rep3, 2 conv (the C hydra_fec_mode enum order). */")
w("#ifndef DCF_MEDIUM_VECTORS_GEN_H")
w("#define DCF_MEDIUM_VECTORS_GEN_H")
w("#include <stddef.h>")
w("#include <stdint.h>")
w("")
w("#define MED_FRAME_LEN 17")
w("#define MED_PROTO_HDR 17")
w("#define MED_MSG_FRAME 12")
w("#define MED_SUPER_LEN 32")
w("#define MED_L2_HDR 2")
w("#define MED_HYDRA_SYNC 0x2DD4u")
w("#define MED_AFSK_SYNC 0x7Eu")
w(f"#define MED_AFSK_CRC8_123456789 0x{anchors['afsk_crc8_123456789']:02X}u")
w("#define MED_CRC_123456789 0x29B1u")
w("#define MED_CRC_ZERO15 0x4EC3u")
w("")
w(f"static const uint8_t MED_L2_FILLER[17] = {cbytes(M.L2_FILLER)};")
w("static const uint8_t MED_BASIS[6][17] = {")
for f in B:
    w(f"  {cbytes(f)},")
w("};")
w("")

# stream
w("typedef struct { const char *name; const uint8_t *input; int input_len; int n_frames;")
w("                 uint8_t frames[6][17]; int skipped_bytes; int tail_bytes; } med_stream_case_t;")
for i, c in enumerate(stream_cases):
    w(f"static const uint8_t MED_STREAM_IN_{i}[] = {cbytes(bytes.fromhex(c['input']))};")
w("static const med_stream_case_t MED_STREAM_CASES[] = {")
for i, c in enumerate(stream_cases):
    w(f"  {{{cstr(c['name'])}, MED_STREAM_IN_{i}, {len(c['input']) // 2}, {len(c['frames'])}, "
      f"{cframes(c['frames'])}, {c['skipped_bytes']}, {c['tail_bytes']}}},")
w("};")
w("#define MED_N_STREAM ((int)(sizeof(MED_STREAM_CASES) / sizeof(MED_STREAM_CASES[0])))")
w("")

# hex
w("typedef struct { const char *name; const char *text; int n_frames; uint8_t frames[6][17];")
w("                 const char *decode_input; int n_decoded; uint8_t decoded[6][17];")
w("                 int bad_lines; } med_hex_case_t;")
w("static const med_hex_case_t MED_HEX_CASES[] = {")
for c in hex_cases:
    w(f"  {{{cstr(c['name'])}, {cstr(c['text'])}, {len(c['frames'])}, {cframes(c['frames'])},")
    w(f"   {cstr(c['decode_input'])}, {len(c['decoded'])}, {cframes(c['decoded'])}, "
      f"{c['bad_lines']}}},")
w("};")
w("#define MED_N_HEX ((int)(sizeof(MED_HEX_CASES) / sizeof(MED_HEX_CASES[0])))")
w("")

# udp_proto
w("typedef struct { const char *name; uint8_t type; uint32_t seq; uint64_t ts;")
w("                 uint8_t payload[64]; int payload_len; uint8_t datagram[128]; int datagram_len;")
w("                 int accept_as_frame; } med_proto_case_t;")
w("static const med_proto_case_t MED_PROTO_CASES[] = {")
for c in proto_cases:
    pl, dg = bytes.fromhex(c["payload"]), bytes.fromhex(c["datagram"])
    w(f"  {{{cstr(c['name'])}, {c['type']}, {c['seq']}u, 0x{c['ts']:016X}ull, {cbytes(pl)}, "
      f"{len(pl)}, {cbytes(dg)}, {len(dg)}, {1 if c['accept_as_frame'] else 0}}},")
w("};")
w("#define MED_N_PROTO ((int)(sizeof(MED_PROTO_CASES) / sizeof(MED_PROTO_CASES[0])))")
w("")

# udp_bare
w("typedef struct { const char *name; int n_frames; uint8_t frames[6][17]; int n_datagrams;")
w("                 uint8_t datagrams[6][32]; int datagram_len[6]; } med_bare_case_t;")
w("static const med_bare_case_t MED_BARE_CASES[] = {")
for c in bare_cases:
    dgs = [bytes.fromhex(d) for d in c["datagrams"]]
    dga = "{" + ",".join(cbytes(d) for d in dgs) + "}" if dgs else "{{0}}"
    dgl = "{" + ",".join(str(len(d)) for d in dgs) + "}" if dgs else "{0}"
    w(f"  {{{cstr(c['name'])}, {len(c['frames'])}, {cframes(c['frames'])}, {len(dgs)}, {dga}, {dgl}}},")
w("};")
w("#define MED_N_BARE ((int)(sizeof(MED_BARE_CASES) / sizeof(MED_BARE_CASES[0])))")
w("")

# l2eth
w("typedef struct { const char *name; int n_frames; uint8_t frames[6][17]; uint8_t payload[130];")
w("                 int payload_len; } med_l2_case_t;")
w("static const med_l2_case_t MED_L2_CASES[] = {")
for c in l2_cases:
    pl = bytes.fromhex(c["payload"])
    w(f"  {{{cstr(c['name'])}, {len(c['frames'])}, {cframes(c['frames'])}, {cbytes(pl)}, {len(pl)}}},")
w("};")
w("#define MED_N_L2 ((int)(sizeof(MED_L2_CASES) / sizeof(MED_L2_CASES[0])))")
w("")

# hydra profiles + cases
w("typedef struct { const char *name; double sample_rate, baud; int n_tones; double base_freq,")
w("                 tone_spacing; int preamble_syms; unsigned sync_word; } med_hydra_profile_t;")
w("static const med_hydra_profile_t MED_HYDRA_PROFILES[2] = {")
for name in ("default", "aux"):
    p = hydra_profiles[name]
    w(f"  {{{cstr(name)}, {cdouble(p['sample_rate'])}, {cdouble(p['baud'])}, {p['n_tones']}, "
      f"{cdouble(p['base_freq'])}, {cdouble(p['tone_spacing'])}, {p['preamble_syms']}, "
      f"0x{p['sync_word']:04X}u}},")
w("};")
w("typedef struct { const char *name; const char *profile; int fec; int interleave; int n_tones;")
w("                 uint8_t frame[17]; int coded_bits; int interleave_stride; int total_syms;")
w("                 const char *symbols; } med_hydra_case_t;")
w("static const med_hydra_case_t MED_HYDRA_CASES[] = {")
for c in hydra_cases:
    w(f"  {{{cstr(c['name'])}, {cstr(c['profile'])}, {M.FEC_NAMES[c['fec']]}, {c['interleave']}, "
      f"{c['n_tones']}, {cbytes(bytes.fromhex(c['frame']))}, {c['coded_bits']}, "
      f"{c['interleave_stride']}, {c['total_syms']},")
    w(f"   {cstr(c['symbols'])}}},")
w("};")
w("#define MED_N_HYDRA ((int)(sizeof(MED_HYDRA_CASES) / sizeof(MED_HYDRA_CASES[0])))")
w("")

# afsk profiles + cases
w("typedef struct { const char *name; double mark, space; int baud, preamble_bits; } med_afsk_profile_t;")
w("static const med_afsk_profile_t MED_AFSK_PROFILES[3] = {")
for name in ("standard", "handheld", "aux-cable"):
    p = afsk_profiles[name]
    w(f"  {{{cstr(name)}, {cdouble(p['mark'])}, {cdouble(p['space'])}, {int(p['baud'])}, "
      f"{int(p['preamble_bits'])}}},")
w("};")
w("typedef struct { const char *name; const char *profile; int fec; uint8_t frame[17]; int n_bits;")
w("                 const char *bits; } med_afsk_case_t;")
w("static const med_afsk_case_t MED_AFSK_CASES[] = {")
for c in afsk_cases:
    w(f"  {{{cstr(c['name'])}, {cstr(c['profile'])}, {1 if c['fec'] else 0}, "
      f"{cbytes(bytes.fromhex(c['frame']))}, {c['n_bits']},")
    w(f"   {cstr(c['bits'])}}},")
w("};")
w("#define MED_N_AFSK ((int)(sizeof(MED_AFSK_CASES) / sizeof(MED_AFSK_CASES[0])))")
w("")
w("#endif /* DCF_MEDIUM_VECTORS_GEN_H */")
with open(hdr_path, "w") as fh:
    fh.write("\n".join(L) + "\n")
print(f"  INFO  wrote {hdr_path} ({os.path.getsize(hdr_path)} bytes)")
print("ALL MEDIUM LAWS HOLD")
