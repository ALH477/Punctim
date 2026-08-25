# SPDX-License-Identifier: LGPL-3.0-only
"""DCF QkdLab core — reference for the DCF-QKD key-ID beacon L2 framing.

DCF-QKD is an *adapter* over the 17-byte DeModFrame wire quantum, exactly like
DCF-Audio and DCF-Text: one ETSI GS QKD 014 ``key_ID`` is fragmented into four
ordinary DeModFrame CTRL frames whose 4-byte payloads carry the bytes, with the
fragment bookkeeping packed into the ``seq`` field.  The frames are byte-for-byte
valid DeModFrames (sync 0xD3, version nibble 1, CRC-16/CCITT), so they satisfy
the same wire invariant the 246-vector certificate pins — this module reuses the
certified codec in ``wirelab_core.py`` so nothing about the quantum is reinvented
here, and the L2 bytes are pinned cross-language by
``Documentation/qkd_vectors.json``.

("Wire quantum" is quantum as in *quanta* — an indivisible unit.  It has nothing
to do with quantum mechanics, and nothing in this module is quantum: a key_ID is
an opaque 128-bit identifier minted by external KME hardware.)

Why this adapter exists at all:
  ETSI GS QKD 014 deliberately leaves the transport of ``key_ID`` from the master
  SAE to the slave SAE **out of scope**, so every deployment has to invent its own
  carrier.  The arithmetic makes the wire quantum an unusually good fit:

      key_ID = UUID = 128 bits = 16 bytes = exactly 4 x 4-byte DeModFrame payloads

Why no descriptor fragment:
  Every other fragmenting adapter here burns frag_idx 0 on a descriptor carrying
  the byte length.  A key_ID is *always* 16 bytes, so length and frag_total are
  known a priori and there is nothing to carry in band.  All four fragments are
  data.  This is a normative design property, not an omission.

L2 framing (all frames version=1, type=CTRL(3), big-endian; see WIRE_QUANTUM_SPEC.md):
  seq (u16) = epoch[15:2] (14 bits, 0..16383) | frag_idx[1:0] (2 bits, 0..3)
  frag_idx 0..3  data : payload = key_id_bytes[idx*4 .. +4]   (no padding, ever)
  frag_total     = 4 (constant)
  src            = local SAE / node id (u16)
  dst            = rendezvous channel (u16): 0xFFFF broadcast, else crc16(passphrase)
  ts_us          = 24-bit microsecond timestamp, identical across a beacon's frames

The 14:2 split is unique among the CTRL(3) adapters (audio 11:5, cue 9:7, snake
5:11).  As everywhere else in the tree there is **no in-band adapter tag**: a node
runs exactly one reassembler per ``dst`` channel.

EXPORT / SECURITY (normative): the wire carries **only the key_ID**, a non-secret
identifier.  Key material MUST NOT be placed in a DeModFrame payload — see
``Documentation/DCF_QKD_SPEC.md``.  This module never sees a key.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wirelab_core import encode, decode, crc16_ccitt  # certified DeModFrame codec

# ── L2 framing constants ──────────────────────────────────────────────────────
FCTRL = 3                       # frame type carrying key-ID beacon fragments
FRAG_BITS = 2                   # low bits of seq hold the fragment index
FRAG_MASK = (1 << FRAG_BITS) - 1             # 0x3
FRAGS = 4                       # a key_ID is always exactly 4 fragments
KEY_ID_BYTES = 16               # 128-bit UUID
MAX_EPOCH = (1 << (16 - FRAG_BITS)) - 1      # 16383

BROADCAST = 0xFFFF              # dst meaning "every node on the wire"

DEFAULT_MAX_PENDING = 64        # incomplete beacons held before oldest is evicted


def channel_id(name):
    """Map a human channel/passphrase to a 16-bit rendezvous ``dst`` (the same
    frequency-channel trick the rest of the repo uses).  ``None``/"" => broadcast."""
    if not name:
        return BROADCAST
    return crc16_ccitt(name.encode("utf-8"))


def key_id_bytes(key_id):
    """Normalise a key_ID to its 16 raw bytes.  Accepts a UUID string (any form
    ``uuid.UUID`` accepts) or a 16-byte buffer.  Raises ValueError otherwise."""
    if isinstance(key_id, (bytes, bytearray, memoryview)):
        raw = bytes(key_id)
        if len(raw) != KEY_ID_BYTES:
            raise ValueError(f"key_ID must be exactly {KEY_ID_BYTES} bytes, got {len(raw)}")
        return raw
    try:
        return uuid.UUID(str(key_id)).bytes
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"not a valid key_ID/UUID: {key_id!r}") from exc


def packetize(key_id, epoch, ts_us, src, dst):
    """Serialise one ETSI 014 key_ID into exactly four 17-byte DeModFrame CTRL
    frames.  Returns [frag0, frag1, frag2, frag3] — there is no descriptor."""
    raw = key_id_bytes(key_id)
    if not (0 <= epoch <= MAX_EPOCH):
        raise ValueError(f"epoch must be 0..{MAX_EPOCH}")
    frames = []
    for idx in range(FRAGS):
        seq = (epoch << FRAG_BITS) | idx
        frames.append(encode(FCTRL, seq, src, dst, raw[idx * 4:idx * 4 + 4], ts_us))
    return frames


class KeyIdReassembler:
    """Stateful, order-independent reassembler for key-ID beacons.

    Beacons are keyed by (src, dst, epoch), so two SAEs may beacon the same epoch
    concurrently without cross-contamination.  push() emits a completed key_ID as
    soon as all four fragments have arrived; duplicates are ignored.

    Incomplete beacons are bounded: once more than ``max_pending`` are in flight
    the oldest is evicted and reported lost.  (A lossy link would otherwise leak
    memory indefinitely — a real defect in the upstream reference.)

    Emitted key event: ("key_id", epoch, ts_us, src, dst, key_id_str)
    Lost event (eviction or finalize): ("lost", epoch, src, dst)
    """

    def __init__(self, accept_dst=None, max_pending=DEFAULT_MAX_PENDING):
        # accept_dst: None => accept every channel; else only this dst + BROADCAST
        self._accept = accept_dst
        self._max_pending = max(1, int(max_pending))
        self._pend = {}    # (src, dst, epoch) -> dict(ts, frags{idx:4B})

    def push(self, frame):
        try:
            d = decode(frame)
        except ValueError:
            return []                      # corrupt frame: dropped, never fatal
        if d["frame_type"] != FCTRL:
            return []
        if self._accept is not None and d["dst"] not in (self._accept, BROADCAST):
            return []
        seq = d["seq"]
        epoch = seq >> FRAG_BITS
        frag_idx = seq & FRAG_MASK
        key = (d["src"], d["dst"], epoch)

        entry = self._pend.setdefault(key, {"ts": d["ts_us"], "frags": {}})
        entry["ts"] = d["ts_us"]
        if frag_idx not in entry["frags"]:
            entry["frags"][frag_idx] = bytes.fromhex(d["payload"])

        events = []
        if len(entry["frags"]) == FRAGS:
            del self._pend[key]
            raw = b"".join(entry["frags"][i] for i in range(FRAGS))
            events.append(("key_id", epoch, entry["ts"], key[0], key[1],
                           str(uuid.UUID(bytes=raw))))
        # bound the in-flight set: evict oldest-first, reporting each as lost
        while len(self._pend) > self._max_pending:
            old = next(iter(self._pend))
            del self._pend[old]
            events.append(("lost", old[2], old[0], old[1]))
        return events

    def pending(self):
        """Number of beacons with some but not all fragments received."""
        return len(self._pend)

    def finalize(self):
        """Report every still-incomplete beacon as lost (ascending src, dst,
        epoch), clearing state.  Mirrors TextReassembler.finalize."""
        events = [("lost", e, s, d) for (s, d, e) in sorted(self._pend.keys())]
        self._pend.clear()
        return events


# ── self-test (round trips key_IDs through encode -> wire -> decode) ──────────
def _selftest():
    cases = [
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "574bace1-4c27-49a1-babd-663fdb624d00",
        "d54b214e-60eb-4f1c-afb0-62a7b23fc20e",
    ]
    for i, kid in enumerate(cases):
        frames = packetize(kid, i, 123456, src=7, dst=42)
        assert len(frames) == FRAGS, f"expected {FRAGS} frames, got {len(frames)}"
        for f in frames:
            assert len(f) == 17 and f[0] == 0xD3 and (f[1] >> 4) == 1
            assert (f[1] & 0x0F) == FCTRL
        # payloads concatenate to the raw UUID with no padding
        joined = b"".join(bytes.fromhex(decode(f)["payload"]) for f in frames)
        assert joined == uuid.UUID(kid).bytes
        r = KeyIdReassembler()
        out = []
        for f in frames:
            out += r.push(f)
        assert len(out) == 1 and out[0][5] == kid, out
        assert r.pending() == 0
        # replaying a consumed fragment must not double-emit
        assert r.push(frames[-1]) == []
        assert r.finalize() == [("lost", i, 7, 42)]   # the lone replayed fragment

    # reversed + duplicated arrival still reassembles exactly once
    kid = cases[2]
    frames = packetize(kid, 9, 1, src=1, dst=2)
    r = KeyIdReassembler()
    out = []
    for f in [frames[3], frames[3], frames[1], frames[2], frames[0]]:
        out += r.push(f)
    assert len(out) == 1 and out[0][5] == kid, out

    # two interleaved epochs from one src must not cross-contaminate
    a = packetize(cases[2], 1, 5, src=1, dst=2)
    b = packetize(cases[3], 2, 5, src=1, dst=2)
    r = KeyIdReassembler()
    out = []
    for pair in zip(a, b):
        for f in pair:
            out += r.push(f)
    got = sorted(e[5] for e in out)
    assert got == sorted([cases[2], cases[3]]), got

    # same epoch from two different srcs must not cross-contaminate either
    a = packetize(cases[2], 7, 5, src=1, dst=2)
    b = packetize(cases[3], 7, 5, src=9, dst=2)
    r = KeyIdReassembler()
    out = []
    for pair in zip(a, b):
        for f in pair:
            out += r.push(f)
    assert sorted(e[5] for e in out) == sorted([cases[2], cases[3]])

    # a dropped fragment leaves the beacon incomplete -> finalize reports it lost
    frames = packetize(cases[2], 11, 1, src=1, dst=2)
    r = KeyIdReassembler()
    for j, f in enumerate(frames):
        if j == 2:
            continue
        assert r.push(f) == []
    assert r.finalize() == [("lost", 11, 1, 2)]

    # foreign dst is ignored when the reassembler is bound to a channel
    frames = packetize(cases[2], 12, 1, src=1, dst=0x1234)
    r = KeyIdReassembler(accept_dst=0x9999)
    for f in frames:
        assert r.push(f) == []
    assert r.pending() == 0

    # broadcast is always accepted
    frames = packetize(cases[2], 13, 1, src=1, dst=BROADCAST)
    r = KeyIdReassembler(accept_dst=0x9999)
    out = []
    for f in frames:
        out += r.push(f)
    assert len(out) == 1 and out[0][5] == cases[2]

    # the in-flight set is bounded: the push that exceeds max_pending evicts the
    # oldest incomplete beacon and reports it lost
    r = KeyIdReassembler(max_pending=2)
    for ep in (0, 1):
        assert r.push(packetize(cases[2], ep, 1, src=1, dst=2)[0]) == []
    assert r.pending() == 2
    evicted = r.push(packetize(cases[2], 2, 1, src=1, dst=2)[0])
    assert evicted == [("lost", 0, 1, 2)], evicted
    assert r.pending() == 2

    # bounds
    for bad in (-1, MAX_EPOCH + 1):
        try:
            packetize(cases[2], bad, 1, 1, 2)
            raise AssertionError(f"epoch {bad} should have raised")
        except ValueError:
            pass
    for bad in ("not-a-uuid", b"\x00" * 15, 12.5):
        try:
            packetize(bad, 0, 1, 1, 2)
            raise AssertionError(f"key_ID {bad!r} should have raised")
        except ValueError:
            pass

    # channel anchors
    assert channel_id("123456789") == 0x29B1
    assert channel_id(None) == BROADCAST and channel_id("") == BROADCAST

    print("qkdlab_core selftest: CERTIFIED")


if __name__ == "__main__":
    _selftest()
