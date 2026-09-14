# SPDX-License-Identifier: LGPL-3.0-only
"""HydraPack core — the universal serialization layer for Punctim.

HydraPack is the single point at which an abstract value becomes either a short
burst of 4-byte quanta (for the quantum / adapter path) or a contiguous byte
buffer (for the DCF-Pipe data plane).  It never invents a new wire format: the
17-byte DeModFrame remains the only certified quantum, and Pipe control messages
remain ordinary frame payloads.  HydraPack only decides *how* application values
are turned into the representations already defined by the quantum and by Pipe.

Two emission planes (pure, deterministic given the schema and the value):
  * Quantum path — packed_size <= threshold (default 120 B)
      -> ordered list of 4-byte payloads, ready for adapter framing / SuperPack.
        - Single-quantum (packed <= 4 B): one bare quantum, no descriptor.
        - Multi-quantum  (packed  > 4 B): descriptor quantum + data quanta.
  * Pipe path    — packed_size >  threshold
      -> contiguous byte buffer + metadata (schema_id, version, FNV-1a checksum),
         handed to a DCF-Pipe sender.

This module is the byte-exact *contract* (like pipelab_core / textlab_core):
pure pack/unpack functions with no I/O, pinned across C (codec/demod_hydrapack.h),
Rust (codec/src/hydrapack.rs), and Python by Documentation/hydrapack_vectors.json.
The 246-vector wire certificate is untouched — HydraPack feeds payload bytes to
adapters and buffers to Pipe; it never touches DeModFrame.

All multi-byte integers are big-endian (matching the rest of the DCF wire).
Bit-packing is MSB-first, zero-padded to a byte boundary on the final byte.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipelab_core import fnv1a32, pack_open, unpack_open

# ── Version / constants ──────────────────────────────────────────────────────
HYDRAPACK_VERSION = 1
DEFAULT_THRESHOLD = 120          # recommended quantum/pipe boundary (bytes)
DESC_LEN = 4                     # descriptor quantum is exactly 4 bytes
QUANTUM_LEN = 4                  # every quantum is exactly 4 bytes
DESC_PAYLOAD_MAX = 255           # descriptor's payload_byte_len is a u8

# ── Field kinds ──────────────────────────────────────────────────────────────
KIND_U = "u"
KIND_I = "i"
KIND_BOOL = "bool"
KIND_ENUM = "enum"
KIND_BITS = "bits"
KIND_STRUCT = "struct"

_ALL_KINDS = (KIND_U, KIND_I, KIND_BOOL, KIND_ENUM, KIND_BITS, KIND_STRUCT)


# ── Bit-level codec (big-endian, MSB-first) ──────────────────────────────────
class BitWriter:
    """Accumulate a big-endian bit stream (MSB-first), zero-pad the tail."""

    def __init__(self):
        self._acc = 0
        self._nbits = 0

    def write(self, value, nbits):
        """Write the low *nbits* bits of *value* into the stream (MSB-first)."""
        if nbits <= 0:
            return
        mask = (1 << nbits) - 1
        self._acc = (self._acc << nbits) | (int(value) & mask)
        self._nbits += nbits

    def to_bytes(self):
        """Pad with zero bits to a byte boundary and return big-endian bytes."""
        pad = (-self._nbits) % 8
        acc = self._acc << pad
        nbytes = (self._nbits + pad) // 8
        return acc.to_bytes(nbytes, "big") if nbytes else b""

    @property
    def bit_count(self):
        return self._nbits


class BitReader:
    """Read a big-endian bit stream (MSB-first) from a byte buffer."""

    def __init__(self, data):
        self._data = bytes(data)
        self._pos = 0

    def read(self, nbits, signed=False):
        """Read *nbits* from the stream.  If *signed*, two's-complement sign-extend."""
        if nbits <= 0:
            return 0
        value = 0
        for _ in range(nbits):
            byte_idx = self._pos >> 3
            bit_idx = 7 - (self._pos & 7)
            if byte_idx < len(self._data):
                value = (value << 1) | ((self._data[byte_idx] >> bit_idx) & 1)
            else:
                value = value << 1
            self._pos += 1
        if signed and nbits > 0 and (value >> (nbits - 1)) & 1:
            value -= (1 << nbits)
        return value


# ── Schema model (declarative, language-agnostic) ────────────────────────────
class Field:
    """One field in a schema.

    kind   : one of KIND_* (u, i, bool, enum, bits, struct)
    width  : bit width (ignored for bool; required for u/i/enum/bits)
    sub_fields : list[Field] (required for struct)
    enum_values : dict name->int (optional metadata for enum; wire carries the int)
    """

    __slots__ = ("name", "kind", "width", "sub_fields", "enum_values")

    def __init__(self, name, kind, width=0, sub_fields=None, enum_values=None):
        if kind not in _ALL_KINDS:
            raise ValueError(f"unknown field kind {kind!r}")
        self.name = name
        self.kind = kind
        self.width = width
        self.sub_fields = sub_fields
        self.enum_values = enum_values

    def to_dict(self):
        d = {"name": self.name, "kind": self.kind}
        if self.kind == KIND_STRUCT:
            d["sub_fields"] = [f.to_dict() for f in self.sub_fields]
        elif self.kind == KIND_BOOL:
            pass
        else:
            d["width"] = self.width
            if self.kind == KIND_ENUM and self.enum_values:
                d["enum_values"] = self.enum_values
        return d

    @classmethod
    def from_dict(cls, d):
        if d["kind"] == KIND_STRUCT:
            return cls(d["name"], d["kind"],
                       sub_fields=[cls.from_dict(f) for f in d["sub_fields"]])
        return cls(d["name"], d["kind"], width=d.get("width", 0),
                   enum_values=d.get("enum_values"))

    @property
    def packed_bits(self):
        if self.kind == KIND_BOOL:
            return 1
        if self.kind == KIND_STRUCT:
            return sum(f.packed_bits for f in self.sub_fields)
        return self.width


class Schema:
    """An ordered collection of fields with explicit widths and packing rules,
    identified by a 16-bit *schema_id* and a 4-bit *version*."""

    __slots__ = ("schema_id", "version", "fields")

    def __init__(self, schema_id, version, fields):
        if not (0 <= schema_id < (1 << 16)):
            raise ValueError("schema_id must be u16")
        if not (0 <= version < (1 << 4)):
            raise ValueError("version must fit 4 bits (0..15)")
        self.schema_id = schema_id
        self.version = version
        self.fields = list(fields)

    def to_dict(self):
        return {"id": self.schema_id, "version": self.version,
                "fields": [f.to_dict() for f in self.fields]}

    @classmethod
    def from_dict(cls, d):
        return cls(d["id"], d["version"],
                   [Field.from_dict(f) for f in d["fields"]])

    @property
    def packed_bits(self):
        return sum(f.packed_bits for f in self.fields)


def packed_size(schema, value=None):
    """Packed byte length of *value* under *schema*.

    In v0.1 (fixed-width types only) this is a pure function of the schema;
    the *value* argument is accepted for forward-compatibility with blob/optional.
    """
    if value is not None:
        return len(pack_value(schema, value))
    return (schema.packed_bits + 7) // 8


# ── Value pack / unpack (the bit-packing core) ──────────────────────────────
def _pack_field(field, value, bw):
    k = field.kind
    if k == KIND_BOOL:
        bw.write(1 if value else 0, 1)
    elif k in (KIND_U, KIND_ENUM, KIND_BITS):
        bw.write(int(value), field.width)
    elif k == KIND_I:
        bw.write(int(value), field.width)
    elif k == KIND_STRUCT:
        for sf in field.sub_fields:
            _pack_field(sf, value[sf.name], bw)
    else:
        raise ValueError(f"unhandled kind {k}")


def _unpack_field(field, br):
    k = field.kind
    if k == KIND_BOOL:
        return bool(br.read(1))
    if k in (KIND_U, KIND_ENUM, KIND_BITS):
        return br.read(field.width)
    if k == KIND_I:
        return br.read(field.width, signed=True)
    if k == KIND_STRUCT:
        return {sf.name: _unpack_field(sf, br) for sf in field.sub_fields}
    raise ValueError(f"unhandled kind {k}")


def pack_value(schema, value):
    """Bit-pack *value* (a dict keyed by field name) under *schema* → bytes."""
    bw = BitWriter()
    for f in schema.fields:
        _pack_field(f, value[f.name], bw)
    return bw.to_bytes()


def unpack_value(schema, data):
    """Inverse of pack_value — *data* is the packed bytes for *schema*."""
    br = BitReader(data)
    return {f.name: _unpack_field(f, br) for f in schema.fields}


# ── Quantum path ─────────────────────────────────────────────────────────────
#
# Multi-quantum descriptor (4 bytes, byte-aligned, big-endian):
#   B0  schema_id_hi
#   B1  schema_id_lo
#   B2  (schema_version << 4) | flags   (4-bit version nibble, 4-bit opaque flags)
#   B3  payload_byte_len                  (the packed-data byte length, 0..255)


def pack_quantum(value, schema, *, flags=0, force_descriptor=False):
    """Pack *value* into a list of 4-byte quanta (the quantum path).

    If the packed representation fits in <= 4 bytes and *force_descriptor* is
    False, emit a single bare quantum (no descriptor) — the schema is implied by
    context.  Otherwise emit a descriptor quantum followed by data quanta (the
    last data quantum is zero-padded to 4 bytes).

    Returns a list of exactly-4-byte ``bytes`` objects.
    """
    if not (0 <= flags < (1 << 4)):
        raise ValueError("flags must fit 4 bits (0..15)")
    packed = pack_value(schema, value)

    if len(packed) <= QUANTUM_LEN and not force_descriptor:
        return [packed + bytes(QUANTUM_LEN - len(packed))]

    if len(packed) > DESC_PAYLOAD_MAX:
        raise ValueError(
            f"packed {len(packed)}B exceeds descriptor max {DESC_PAYLOAD_MAX}B "
            "(use the Pipe path)")

    desc = bytes([
        (schema.schema_id >> 8) & 0xFF,
        schema.schema_id & 0xFF,
        ((schema.version & 0x0F) << 4) | (flags & 0x0F),
        len(packed) & 0xFF,
    ])
    quanta = [desc]
    for i in range(0, len(packed), QUANTUM_LEN):
        chunk = packed[i:i + QUANTUM_LEN]
        quanta.append(chunk + bytes(QUANTUM_LEN - len(chunk)))
    return quanta


def unpack_quantum(quanta, schema_registry, *, single_schema=None):
    """Reassemble a list of 4-byte quanta into ``(schema_id, version, flags, value)``.

    For a single-quantum message (no descriptor), pass *single_schema* = the Schema
    that applies by context; ``flags`` is 0 in this case.

    For a multi-quantum message, the first quantum is the descriptor naming the
    schema; *schema_registry* is a dict ``{(schema_id, version): Schema}``.
    """
    if not quanta:
        raise ValueError("empty quanta list")

    if len(quanta) == 1 and single_schema is not None:
        return (single_schema.schema_id, single_schema.version, 0,
                unpack_value(single_schema, quanta[0]))

    desc = quanta[0]
    schema_id = (desc[0] << 8) | desc[1]
    version = (desc[2] >> 4) & 0x0F
    flags = desc[2] & 0x0F
    payload_len = desc[3]

    raw = b"".join(quanta[1:])
    packed = raw[:payload_len]

    key = (schema_id, version)
    schema = schema_registry.get(key)
    if schema is None:
        raise KeyError(f"unknown schema ({schema_id}, {version})")
    return (schema_id, version, flags, unpack_value(schema, packed))


# ── Pipe path ────────────────────────────────────────────────────────────────


def pack_pipe(value, schema):
    """Pack *value* for the DCF-Pipe data plane.

    Returns ``(buffer, schema_id, schema_version, checksum)`` where *buffer* is
    the contiguous packed bytes (no HydraPack framing) and *checksum* is the
    FNV-1a 32-bit over *buffer*.  All chunking, FEC, credit, and ARQ are
    performed by DCF-Pipe.
    """
    buf = pack_value(schema, value)
    return (buf, schema.schema_id, schema.version, fnv1a32(buf))


def unpack_pipe(buf, schema):
    """Unpack a Pipe-path buffer.  The caller (DCF-Pipe) verifies the FNV-1a
    checksum via OPEN before calling this; HydraPack does not re-verify."""
    return unpack_value(schema, buf)


# ── OpenPipe — the OPEN extension for HydraPipe sessions ─────────────────────
#
# Layered on top of pipelab_core.pack_open (never modifying the certified 14-byte
# OPEN), this appends 3 bytes:
#   B14  schema_id_hi
#   B15  schema_id_lo
#   B16  (schema_version << 4) | flags
#
# Total = 17 bytes.  A plain-Pipe receiver sees a valid 14-byte OPEN and ignores
# the trailing 3 bytes (additive, fail-safe); a HydraPack receiver reads them.
OPENPIPE_LEN = 17


def pack_openpipe(session_id, total_len, chunk_size, obj_checksum,
                  schema_id, schema_version, flags=0):
    """Extended OPEN for HydraPipe: 14-byte OPEN + 3 bytes schema metadata."""
    if not (0 <= schema_id < (1 << 16)):
        raise ValueError("schema_id must be u16")
    if not (0 <= schema_version < (1 << 4)):
        raise ValueError("schema_version must fit 4 bits (0..15)")
    if not (0 <= flags < (1 << 4)):
        raise ValueError("flags must fit 4 bits (0..15)")
    base = pack_open(session_id, total_len, chunk_size, obj_checksum)   # 14 B
    ext = bytes([
        (schema_id >> 8) & 0xFF,
        schema_id & 0xFF,
        ((schema_version & 0x0F) << 4) | (flags & 0x0F),
    ])
    return base + ext


def unpack_openpipe(buf):
    """Inverse of pack_openpipe.

    Returns ``(session_id, total_len, chunk_size, checksum, schema_id, schema_version, flags)``.
    Raises ValueError if the first 14 bytes are not a valid OPEN.
    """
    session, total_len, chunk_size, checksum = unpack_open(buf[:14])
    if len(buf) < OPENPIPE_LEN:
        raise ValueError("OpenPipe needs >= 17 bytes")
    schema_id = (buf[14] << 8) | buf[15]
    vf = buf[16]
    schema_version = (vf >> 4) & 0x0F
    flags = vf & 0x0F
    return (session, total_len, chunk_size, checksum,
            schema_id, schema_version, flags)


# ── Plane selection ──────────────────────────────────────────────────────────


def plane_select(schema, value=None, threshold=DEFAULT_THRESHOLD):
    """Pure, deterministic: ``'quantum'`` if packed_size <= *threshold*, else ``'pipe'``."""
    if value is not None:
        ps = len(pack_value(schema, value))
    else:
        ps = (schema.packed_bits + 7) // 8
    return "quantum" if ps <= threshold else "pipe"


# ── Self-test ────────────────────────────────────────────────────────────────


def _selftest():
    # Schema 0: single-quantum (29 bits = 4 bytes)
    s0 = Schema(0, 1, [
        Field("x", KIND_U, 10),
        Field("y", KIND_U, 10),
        Field("v", KIND_I, 8),
        Field("hot", KIND_BOOL),
    ])
    assert packed_size(s0) == 4

    v0 = {"x": 1000, "y": 700, "v": -3, "hot": True}
    q0 = pack_quantum(v0, s0)
    assert len(q0) == 1 and len(q0[0]) == 4, "single-quantum emits exactly one 4-byte quantum"
    r0 = unpack_quantum(q0, {}, single_schema=s0)
    assert r0 == (0, 1, 0, v0), r0

    # Schema 3: multi-quantum (56 bits = 7 bytes)
    s3 = Schema(3, 1, [
        Field("x", KIND_U, 12),
        Field("y", KIND_U, 12),
        Field("vx", KIND_I, 8),
        Field("vy", KIND_I, 8),
        Field("heading", KIND_U, 8),
        Field("flags", KIND_U, 8),
    ])
    assert packed_size(s3) == 7

    v3 = {"x": 3500, "y": 2800, "vx": -12, "vy": 7, "heading": 180, "flags": 0x42}
    q3 = pack_quantum(v3, s3, flags=0x5)
    assert len(q3) == 1 + (7 + 3) // 4  # 1 desc + 2 data
    assert q3[0] == bytes([(3 >> 8) & 0xFF, 3 & 0xFF, (1 << 4) | 0x5, 7])
    reg = {(3, 1): s3}
    r3 = unpack_quantum(q3, reg)
    assert r3 == (3, 1, 0x5, v3), r3

    # Pipe path
    buf, sid, ver, ck = pack_pipe(v3, s3)
    assert sid == 3 and ver == 1
    assert ck == fnv1a32(buf)
    assert unpack_pipe(buf, s3) == v3

    # OpenPipe round-trip
    op = pack_openpipe(7, 100000, 1400, 0xDEADBEEF, 3, 1, 0x5)
    assert len(op) == 17
    assert op[:14] == pack_open(7, 100000, 1400, 0xDEADBEEF)
    assert unpack_openpipe(op) == (7, 100000, 1400, 0xDEADBEEF, 3, 1, 0x5)

    # Plane selection
    assert plane_select(s0) == "quantum"   # 4 bytes <= 120
    assert plane_select(s3) == "quantum"    # 7 bytes <= 120
    big = Schema(99, 1, [Field(f"b{i}", KIND_U, 8) for i in range(121)])
    assert plane_select(big) == "pipe"      # 121 bytes > 120

    # Struct nesting
    s5 = Schema(5, 1, [
        Field("pos", KIND_STRUCT, sub_fields=[
            Field("x", KIND_U, 12), Field("y", KIND_U, 12)]),
        Field("vel", KIND_STRUCT, sub_fields=[
            Field("vx", KIND_I, 8), Field("vy", KIND_I, 8)]),
        Field("id", KIND_U, 8),
    ])
    v5 = {"pos": {"x": 3500, "y": 2800}, "vel": {"vx": -1, "vy": 2}, "id": 42}
    assert unpack_value(s5, pack_value(s5, v5)) == v5
    q5 = pack_quantum(v5, s5)
    assert unpack_quantum(q5, {(5, 1): s5}) == (5, 1, 0, v5)

    # FNV anchors (same as pipelab_core)
    assert fnv1a32(b"") == 0x811C9DC5
    assert fnv1a32(b"hello") == 0x4F9F2CAB

    print("hydrapack_core selftest: CERTIFIED")


if __name__ == "__main__":
    _selftest()