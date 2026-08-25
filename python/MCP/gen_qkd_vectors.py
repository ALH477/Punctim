# SPDX-License-Identifier: LGPL-3.0-only
"""Executable laws + golden-vector generator for the DCF-QKD key-ID beacon L2 framing.

Mirrors gen_text_vectors.py: it first asserts the framing/reassembly laws hold, then
emits the finite vectors that the C and Rust implementations certify against
byte-for-byte.  The key-ID beacon is an adapter over the 17-byte DeModFrame, so none
of this touches the 246-vector wire certificate.

Usage:  python3 gen_qkd_vectors.py [qkd_vectors.json]
  Writes  <path>                   (beacon framing + reassembly)
  and     <dir>/qkd_vectors.gen.h  (dependency-free C test header)
Exit 0 iff every law holds.  Commit identical copies to Documentation/ and python/MCP/.

Note the beacon carries only the key_ID — a non-secret 128-bit identifier.  No key
material appears in these vectors, on the wire, or anywhere in this adapter.
"""
import json
import os
import random
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qkdlab_core import (
    packetize, KeyIdReassembler, channel_id, key_id_bytes,
    FRAG_BITS, FRAGS, KEY_ID_BYTES, MAX_EPOCH, FCTRL, BROADCAST,
)
from wirelab_core import decode

random.seed(0x9C1D)
ok = lambda name: print(f"  PASS  {name}")


def hexframes(frames):
    return [f.hex() for f in frames]


# ── Law A: packetize → reassemble is identity (in-order) ──────────────────────
def reassemble_all(frames, accept_dst=None):
    r = KeyIdReassembler(accept_dst=accept_dst)
    out = []
    for f in frames:
        out += r.push(f)
    out += r.finalize()
    return out


framing_cases = []
# Epochs exercise both rails (0 and 16383) plus interior values; the key_IDs cover
# the nil UUID, the all-ones UUID, two realistic ETSI 014 ids, and a random one.
cases = [
    (0,         "00000000-0000-0000-0000-000000000000", 0x000000, 0x0001, BROADCAST),
    (1,         "ffffffff-ffff-ffff-ffff-ffffffffffff", 0x012345, 0x0001, 0x0002),
    (63,        "574bace1-4c27-49a1-babd-663fdb624d00", 0x0FEDCB, 0x00A1, channel_id("qkd")),
    (4096,      "d54b214e-60eb-4f1c-afb0-62a7b23fc20e", 0xFFFFFF, 0xBEEF, 0x1234),
    (MAX_EPOCH, str(uuid.UUID(bytes=bytes(random.randrange(256) for _ in range(16)))),
     0x0000FF, 0xFFFE, channel_id("madqci")),
]
for epoch, kid, ts_us, src, dst in cases:
    frames = packetize(kid, epoch, ts_us, src, dst)
    raw = key_id_bytes(kid)

    # frame-count law: a key_ID is ALWAYS exactly 4 frames — no descriptor, no padding
    assert len(frames) == FRAGS
    # every emitted frame is an ordinary valid CTRL DeModFrame on this epoch
    for idx, f in enumerate(frames):
        d = decode(f)
        assert d["frame_type"] == FCTRL
        assert (d["seq"] >> FRAG_BITS) == epoch
        assert (d["seq"] & ((1 << FRAG_BITS) - 1)) == idx
        assert d["src"] == src and d["dst"] == dst and d["ts_us"] == ts_us
        # payload law: fragment idx carries key_id_bytes[idx*4 .. +4], verbatim
        assert bytes.fromhex(d["payload"]) == raw[idx * 4:idx * 4 + 4]
    # identity
    events = reassemble_all(frames)
    assert events == [("key_id", epoch, ts_us, src, dst, str(uuid.UUID(bytes=raw)))], events

    framing_cases.append({
        "src": src, "dst": dst, "epoch": epoch, "ts_us": ts_us,
        "key_id": str(uuid.UUID(bytes=raw)), "key_id_bytes": raw.hex(),
        "frames": hexframes(frames),
    })
ok(f"packetize→reassemble = id on {len(framing_cases)} framing cases "
   f"(epochs {[c[0] for c in cases]})")

# frame-count invariant, stated on its own: 16 bytes / 4 bytes = 4, always
assert all(len(c["frames"]) == FRAGS for c in framing_cases)
assert KEY_ID_BYTES == FRAGS * 4
ok(f"key_ID is always {KEY_ID_BYTES}B = exactly {FRAGS} fragments (no descriptor, no padding)")

# bounds laws
for bad_epoch in (-1, MAX_EPOCH + 1):
    try:
        packetize(cases[2][1], bad_epoch, 0, 1, 1)
        raise AssertionError(f"epoch {bad_epoch} must be rejected")
    except ValueError:
        pass
ok(f"epoch outside 0..{MAX_EPOCH} rejected; epoch field is {16 - FRAG_BITS} bits")

for bad_kid in ("not-a-uuid", b"\x00" * 15, b"\x00" * 17):
    try:
        packetize(bad_kid, 0, 0, 1, 1)
        raise AssertionError(f"key_ID {bad_kid!r} must be rejected")
    except ValueError:
        pass
ok(f"malformed key_ID rejected; only a UUID or exactly {KEY_ID_BYTES} bytes is accepted")

# channel rendezvous anchor (same crc16 hash the rest of the repo uses)
assert channel_id("123456789") == 0x29B1
assert channel_id(None) == BROADCAST
ok("channel_id = crc16_ccitt rendezvous hash (anchor \"123456789\" -> 0x29B1)")


# ── Law B: reassembly under reorder / drop / duplicate / interleave / foreign dst ──
def events_to_json(events):
    keys, lost = [], []
    for e in events:
        if e[0] == "key_id":
            _, epoch, ts, src, dst, kid = e
            keys.append({"epoch": epoch, "ts_us": ts, "src": src, "dst": dst,
                         "key_id": kid, "key_id_bytes": key_id_bytes(kid).hex()})
        else:
            _, epoch, src, dst = e
            lost.append({"epoch": epoch, "src": src, "dst": dst})
    return keys, lost


reassembly_cases = []
KID_A = "574bace1-4c27-49a1-babd-663fdb624d00"
KID_B = "d54b214e-60eb-4f1c-afb0-62a7b23fc20e"
CH = 0x0002
b0 = packetize(KID_A, 5, 0x010203, 0x0001, CH)
b1 = packetize(KID_B, 6, 0x010210, 0x0001, CH)

seq_in = b0 + b1                                        # 1. in-order
seq_re = list(seq_in)                                   # 2. reordered (deterministic)
random.Random(0xBEEF).shuffle(seq_re)
seq_drop = [f for j, f in enumerate(b0) if j != 2] + b1  # 3. drop one frag of b0 -> lost
seq_dup = [b0[0], b0[0], b0[1], b0[1]] + b0[2:] + b1     # 4. duplicate
seq_ilv = [f for pair in zip(b0, b1) for f in pair]      # 5. epochs interleaved 1:1
# 6. same epoch, two different srcs — must not cross-contaminate
s0 = packetize(KID_A, 7, 0x020304, 0x0001, CH)
s1 = packetize(KID_B, 7, 0x020304, 0x0009, CH)
seq_2src = [f for pair in zip(s0, s1) for f in pair]

for name, stream, accept in [("in_order", seq_in, None),
                             ("reordered", seq_re, None),
                             ("frag_drop_lost", seq_drop, None),
                             ("duplicate", seq_dup, None),
                             ("interleaved_epochs", seq_ilv, None),
                             ("two_srcs_same_epoch", seq_2src, None),
                             ("foreign_dst_ignored", seq_in, 0x9999)]:
    events = reassemble_all(stream, accept_dst=accept)
    keys, lost = events_to_json(events)
    reassembly_cases.append({"name": name, "accept_dst": accept,
                             "input_frames": hexframes(stream),
                             "keys": keys, "lost": lost})

by_name = {c["name"]: c for c in reassembly_cases}
assert by_name["in_order"]["lost"] == [] and len(by_name["in_order"]["keys"]) == 2
assert by_name["reordered"]["lost"] == [] and len(by_name["reordered"]["keys"]) == 2
assert (len(by_name["frag_drop_lost"]["keys"]) == 1
        and by_name["frag_drop_lost"]["lost"] == [{"epoch": 5, "src": 0x0001, "dst": CH}])
assert by_name["duplicate"]["lost"] == [] and len(by_name["duplicate"]["keys"]) == 2
assert by_name["interleaved_epochs"]["lost"] == [] and len(by_name["interleaved_epochs"]["keys"]) == 2
assert by_name["two_srcs_same_epoch"]["lost"] == []
assert sorted(k["key_id"] for k in by_name["two_srcs_same_epoch"]["keys"]) == sorted([KID_A, KID_B])
assert by_name["foreign_dst_ignored"]["keys"] == [] and by_name["foreign_dst_ignored"]["lost"] == []
ok("reassembly correct under reorder, fragment-drop→lost, duplicate, interleaved "
   "epochs, two srcs on one epoch, and foreign-dst rejection")


# ── Anchor: a worked example on a named rendezvous channel ────────────────────
ANCHOR_KID = "574bace1-4c27-49a1-babd-663fdb624d00"
anchor_frames = packetize(ANCHOR_KID, 0x0A, 0x010203, 0x00A1, channel_id("qkd"))

qkd_vectors = {
    "format": "DCF-QKD key-ID beacon L2 framing v1 "
              "(adapter over the 17-byte DeModFrame quantum)",
    "spec": "seq = epoch[15:2] | frag_idx[1:0]; frag_idx 0..3 = key_id_bytes[idx*4 .. +4]; "
            "no descriptor, no padding (a key_ID is always 16 B); type=CTRL(3), version=1",
    "constants": {"frag_bits": FRAG_BITS, "frags": FRAGS, "key_id_bytes": KEY_ID_BYTES,
                  "max_epoch": MAX_EPOCH, "frame_type_ctrl": FCTRL,
                  "broadcast": BROADCAST},
    "anchors": {
        "exampleKeyIdBeacon": {
            "epoch": 0x0A, "ts_us": 0x010203, "src": 0x00A1,
            "dst": channel_id("qkd"), "key_id": ANCHOR_KID,
            "key_id_bytes": key_id_bytes(ANCHOR_KID).hex(),
            "frames": hexframes(anchor_frames),
        }
    },
    "theorem": ("L2 framing is a fixed bit-placement adapter over the certified DeModFrame; "
                "matching these framing + reassembly vectors pins the C and Rust implementations "
                "to this reference on the entire input space.  The key_ID is opaque to L2 and "
                "always 16 bytes, so the beacon needs no descriptor and the vectors are "
                "invariant to key_ID content.  The 246-vector wire certificate is untouched."),
    "framing": framing_cases,
    "reassembly": reassembly_cases,
}


# ── C header emitter (dependency-free vectors for the C cert test) ────────────
def carr(b):
    return "{" + ",".join(f"0x{x:02X}" for x in b) + "}"


C_MAX_IN_FRAMES = 16          # longest reassembly stream in the vectors
C_MAX_KEYS = 4                # most keys emitted by one stream
C_MAX_LOST = 4


def emit_c_header():
    assert all(len(c["input_frames"]) <= C_MAX_IN_FRAMES for c in reassembly_cases)
    assert all(len(c["keys"]) <= C_MAX_KEYS for c in reassembly_cases)
    assert all(len(c["lost"]) <= C_MAX_LOST for c in reassembly_cases)
    L = ['/* GENERATED by python/MCP/gen_qkd_vectors.py — DO NOT EDIT. */',
         '#ifndef DCF_QKD_VECTORS_GEN_H', '#define DCF_QKD_VECTORS_GEN_H',
         '#include <stdint.h>', '']
    # framing
    L += ['typedef struct { uint16_t src, dst, epoch; uint32_t ts_us;',
          f'  uint8_t key_id[{KEY_ID_BYTES}]; uint8_t n_frames; uint8_t frames[{FRAGS}][17]; }} qv_framing_t;',
          'static const qv_framing_t QV_FRAMING[] = {']
    for c in framing_cases:
        kid = bytes.fromhex(c["key_id_bytes"])
        frs = [bytes.fromhex(h) for h in c["frames"]]
        fr = ",".join(carr(f) for f in frs)
        L.append(f'  {{0x{c["src"]:04X},0x{c["dst"]:04X},0x{c["epoch"]:04X},'
                 f'0x{c["ts_us"]:06X}u,{carr(kid)},{len(frs)},{{{fr}}}}},')
    L += ['};', 'static const int QV_N_FRAMING = (int)(sizeof(QV_FRAMING)/sizeof(QV_FRAMING[0]));', '']
    # reassembly
    L += ['typedef struct { uint16_t epoch; uint32_t ts_us; uint16_t src, dst;',
          f'  uint8_t key_id[{KEY_ID_BYTES}]; }} qv_key_t;',
          'typedef struct { uint16_t epoch, src, dst; } qv_lost_t;',
          'typedef struct { const char *name; int accept_dst; /* -1 = accept all */',
          f'  uint8_t n_in; uint8_t in_frames[{C_MAX_IN_FRAMES}][17];',
          f'  uint8_t n_key; qv_key_t keys[{C_MAX_KEYS}];',
          f'  uint8_t n_lost; qv_lost_t lost[{C_MAX_LOST}]; }} qv_reasm_t;',
          'static const qv_reasm_t QV_REASM[] = {']
    for rc in reassembly_cases:
        ins = [bytes.fromhex(h) for h in rc["input_frames"]]
        inf = ",".join(carr(f) for f in ins)
        keys = ",".join(
            f'{{0x{k["epoch"]:04X},0x{k["ts_us"]:06X}u,0x{k["src"]:04X},0x{k["dst"]:04X},'
            f'{carr(bytes.fromhex(k["key_id_bytes"]))}}}' for k in rc["keys"])
        lost = ",".join(f'{{0x{x["epoch"]:04X},0x{x["src"]:04X},0x{x["dst"]:04X}}}'
                        for x in rc["lost"])
        acc = -1 if rc["accept_dst"] is None else rc["accept_dst"]
        L.append(f'  {{"{rc["name"]}",{acc},{len(ins)},{{{inf}}},'
                 f'{len(rc["keys"])},{{{keys if keys else "{0}"}}},'
                 f'{len(rc["lost"])},{{{lost if lost else "{0}"}}}}},')
    L += ['};', 'static const int QV_N_REASM = (int)(sizeof(QV_REASM)/sizeof(QV_REASM[0]));', '',
          '#endif /* DCF_QKD_VECTORS_GEN_H */', '']
    return "\n".join(L)


out_json = sys.argv[1] if len(sys.argv) > 1 else "qkd_vectors.json"
out_dir = os.path.dirname(out_json) or "."
out_h = os.path.join(out_dir, "qkd_vectors.gen.h")
with open(out_json, "w") as fh:
    json.dump(qkd_vectors, fh, indent=1)
with open(out_h, "w") as fh:
    fh.write(emit_c_header())

print(f"  INFO  wrote {out_json} ({os.path.getsize(out_json)} bytes, "
      f"{len(framing_cases)} framing, {len(reassembly_cases)} reassembly)")
print(f"  INFO  wrote {out_h} ({os.path.getsize(out_h)} bytes)")
print("ALL QKD LAWS HOLD")
