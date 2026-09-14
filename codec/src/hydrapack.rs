// SPDX-License-Identifier: LGPL-3.0-only
//! HydraPack — universal serialization layer for Punctim.
//!
//! HydraPack is the single point at which an abstract value becomes either a
//! short burst of 4-byte quanta (for the quantum / adapter path) or a contiguous
//! byte buffer (for the DCF-Pipe data plane). It never invents a new wire format:
//! the 17-byte DeModFrame remains the only certified quantum, and Pipe control
//! messages remain ordinary frame payloads.
//!
//! Two emission planes (pure, deterministic given schema + value):
//! * **Quantum** — packed_size <= threshold (default 120 B)
//!   → `Vec<[u8; 4]>`, ready for adapter framing / SuperPack.
//! * **Pipe** — packed_size > threshold
//!   → contiguous byte buffer + `(schema_id, version, FNV-1a checksum)`.
//!
//! Bit-packing is big-endian (MSB-first), zero-padded to a byte boundary.
//! All multi-byte integers are big-endian. The 246-vector wire certificate is
//! untouched — HydraPack feeds payload bytes to adapters and buffers to Pipe.

use crate::pipe;

// ── Constants ────────────────────────────────────────────────────────────────
pub const HYDRAPACK_VERSION: u8 = 1;
pub const DEFAULT_THRESHOLD: usize = 120;
pub const QUANTUM_LEN: usize = 4;
pub const DESC_PAYLOAD_MAX: usize = 255;
pub const OPENPIPE_LEN: usize = 17;

// ── Field kinds ──────────────────────────────────────────────────────────────
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Kind {
    U = 0,
    I = 1,
    Bool = 2,
    Enum = 3,
    Bits = 4,
    Struct = 5,
}

// ── Schema model (declarative) ───────────────────────────────────────────────
#[derive(Debug, Clone)]
pub struct Field {
    pub name: &'static str,
    pub kind: Kind,
    pub width: u8,
    pub sub_fields: &'static [Field],
}

impl Field {
    pub const fn u(name: &'static str, width: u8) -> Self {
        Field { name, kind: Kind::U, width, sub_fields: &[] }
    }
    pub const fn i(name: &'static str, width: u8) -> Self {
        Field { name, kind: Kind::I, width, sub_fields: &[] }
    }
    pub const fn bool_(name: &'static str) -> Self {
        Field { name, kind: Kind::Bool, width: 0, sub_fields: &[] }
    }
    pub const fn enum_(name: &'static str, width: u8) -> Self {
        Field { name, kind: Kind::Enum, width, sub_fields: &[] }
    }
    pub const fn bits(name: &'static str, width: u8) -> Self {
        Field { name, kind: Kind::Bits, width, sub_fields: &[] }
    }
    pub const fn struct_(name: &'static str, subs: &'static [Field]) -> Self {
        Field { name, kind: Kind::Struct, width: 0, sub_fields: subs }
    }

    pub const fn packed_bits(&self) -> usize {
        match self.kind {
            Kind::Bool => 1,
            Kind::Struct => {
                let mut total = 0;
                let mut i = 0;
                while i < self.sub_fields.len() {
                    total += self.sub_fields[i].packed_bits();
                    i += 1;
                }
                total
            }
            _ => self.width as usize,
        }
    }
}

#[derive(Debug, Clone)]
pub struct Schema {
    pub schema_id: u16,
    pub version: u8,
    pub fields: &'static [Field],
}

impl Schema {
    pub const fn new(schema_id: u16, version: u8, fields: &'static [Field]) -> Self {
        Schema { schema_id, version, fields }
    }

    pub const fn packed_bits(&self) -> usize {
        let mut total = 0;
        let mut i = 0;
        while i < self.fields.len() {
            total += self.fields[i].packed_bits();
            i += 1;
        }
        total
    }

    pub fn packed_size(&self) -> usize {
        (self.packed_bits() + 7) / 8
    }
}

// ── Value model (flattened int + bool arrays) ────────────────────────────────
// A value is a flat list of signed ints and bools, matching schema field order.
// Struct sub-fields are inlined. This avoids heap allocation on the hot path.

pub const MAX_FIELDS: usize = 16;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Value {
    pub ints: [i64; MAX_FIELDS],
    pub bools: [bool; MAX_FIELDS],
    pub n: usize, // number of leaf fields (ints + bools)
}

impl Value {
    pub fn from_slice(ints: &[i64], bools: &[bool]) -> Self {
        let mut v = Value::default();
        v.n = ints.len().max(bools.len());
        for (i, &x) in ints.iter().enumerate() { v.ints[i] = x; }
        for (i, &b) in bools.iter().enumerate() { v.bools[i] = b; }
        v
    }
}

fn count_leaves(fields: &[Field]) -> usize {
    let mut n = 0;
    for f in fields {
        n += count_field_leaves(f);
    }
    n
}

fn count_field_leaves(f: &Field) -> usize {
    match f.kind {
        Kind::Struct => f.sub_fields.iter().map(count_field_leaves).sum(),
        _ => 1,
    }
}

fn flatten(fields: &[Field], value: &Value, idx: &mut usize, bidx: &mut usize) -> Vec<(i64, bool)> {
    let mut out = Vec::new();
    for f in fields {
        flatten_field(f, value, idx, bidx, &mut out);
    }
    out
}

fn flatten_field(f: &Field, value: &Value, idx: &mut usize, bidx: &mut usize, out: &mut Vec<(i64, bool)>) {
    match f.kind {
        Kind::Struct => {
            for sf in f.sub_fields {
                flatten_field(sf, value, idx, bidx, out);
            }
        }
        Kind::Bool => {
            out.push((0, value.bools[*bidx]));
            *bidx += 1;
            *idx += 1;
        }
        _ => {
            out.push((value.ints[*idx], false));
            *idx += 1;
        }
    }
}

// ── Bit-level codec (big-endian, MSB-first) ──────────────────────────────────
struct BitWriter {
    acc: u128,
    nbits: u32,
}

impl BitWriter {
    fn new() -> Self { BitWriter { acc: 0, nbits: 0 } }

    fn write(&mut self, value: i64, nbits: u8) {
        if nbits == 0 { return; }
        let mask = (1u128 << nbits) - 1;
        let bits = (value as u64 as u128) & mask;
        self.acc = (self.acc << nbits as u32) | bits;
        self.nbits += nbits as u32;
    }

    fn to_bytes(&self) -> Vec<u8> {
        let pad = (8 - (self.nbits % 8)) % 8;
        let nbytes = (self.nbits + pad) / 8;
        if nbytes == 0 { return vec![]; }
        let acc = self.acc << pad;
        let be = acc.to_be_bytes();
        let offset = 16 - nbytes as usize;
        be[offset..offset + nbytes as usize].to_vec()
    }
}

struct BitReader<'a> {
    data: &'a [u8],
    pos: u32,
}

impl<'a> BitReader<'a> {
    fn new(data: &'a [u8]) -> Self { BitReader { data, pos: 0 } }

    fn read(&mut self, nbits: u8, signed: bool) -> i64 {
        if nbits == 0 { return 0; }
        let mut value: i64 = 0;
        for _ in 0..nbits {
            let byte_idx = (self.pos >> 3) as usize;
            let bit_idx = 7 - (self.pos & 7);
            let bit = if byte_idx < self.data.len() {
                (self.data[byte_idx] >> bit_idx) & 1
            } else { 0 };
            value = (value << 1) | bit as i64;
            self.pos += 1;
        }
        if signed && nbits > 0 && (value >> (nbits - 1)) & 1 == 1 {
            value -= 1i64 << nbits;
        }
        value
    }
}

// ── Value pack/unpack ───────────────────────────────────────────────────────
pub fn pack_value(schema: &Schema, value: &Value) -> Vec<u8> {
    let mut bw = BitWriter::new();
    let mut idx = 0;
    let mut bidx = 0;
    let flat = flatten(schema.fields, value, &mut idx, &mut bidx);
    let mut fi = 0;
    fn pack_fields(fields: &[Field], flat: &[(i64, bool)], fi: &mut usize, bw: &mut BitWriter) {
        for f in fields {
            match f.kind {
                Kind::Bool => { bw.write(if flat[*fi].1 { 1 } else { 0 }, 1); *fi += 1; }
                Kind::Struct => { pack_fields(f.sub_fields, flat, fi, bw); }
                _ => { bw.write(flat[*fi].0, f.width); *fi += 1; }
            }
        }
    }
    pack_fields(schema.fields, &flat, &mut fi, &mut bw);
    bw.to_bytes()
}

pub fn unpack_value(schema: &Schema, data: &[u8]) -> Value {
    let n_leaves = count_leaves(schema.fields);
    let mut br = BitReader::new(data);
    let mut ints = [0i64; MAX_FIELDS];
    let mut bools = [false; MAX_FIELDS];
    let mut idx = 0;
    let mut bidx = 0;
    fn unpack_fields(fields: &[Field], br: &mut BitReader, ints: &mut [i64], bools: &mut [bool], idx: &mut usize, bidx: &mut usize) {
        for f in fields {
            match f.kind {
                Kind::Bool => { bools[*bidx] = br.read(1, false) != 0; *bidx += 1; *idx += 1; }
                Kind::Struct => { unpack_fields(f.sub_fields, br, ints, bools, idx, bidx); }
                Kind::I => { ints[*idx] = br.read(f.width, true); *idx += 1; }
                _ => { ints[*idx] = br.read(f.width, false); *idx += 1; }
            }
        }
    }
    unpack_fields(schema.fields, &mut br, &mut ints, &mut bools, &mut idx, &mut bidx);
    Value { ints, bools, n: n_leaves }
}

// ── Quantum path ────────────────────────────────────────────────────────────
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum QuantumError {
    PackedTooLarge,
}

pub fn pack_quantum(schema: &Schema, value: &Value, flags: u8, force_descriptor: bool) -> Result<Vec<[u8; 4]>, QuantumError> {
    let packed = pack_value(schema, value);
    if packed.len() <= QUANTUM_LEN && !force_descriptor {
        let mut q = [0u8; 4];
        q[..packed.len()].copy_from_slice(&packed);
        return Ok(vec![q]);
    }
    if packed.len() > DESC_PAYLOAD_MAX {
        return Err(QuantumError::PackedTooLarge);
    }
    let desc = [
        (schema.schema_id >> 8) as u8,
        (schema.schema_id & 0xFF) as u8,
        ((schema.version & 0x0F) << 4) | (flags & 0x0F),
        packed.len() as u8,
    ];
    let mut quanta = vec![desc];
    for chunk in packed.chunks(QUANTUM_LEN) {
        let mut q = [0u8; 4];
        q[..chunk.len()].copy_from_slice(chunk);
        quanta.push(q);
    }
    Ok(quanta)
}

pub struct UnpackedQuantum {
    pub schema_id: u16,
    pub version: u8,
    pub flags: u8,
    pub value: Value,
}

pub fn unpack_quantum_single(quanta: &[[u8; 4]], schema: &Schema) -> UnpackedQuantum {
    UnpackedQuantum {
        schema_id: schema.schema_id,
        version: schema.version,
        flags: 0,
        value: unpack_value(schema, &quanta[0]),
    }
}

pub fn unpack_quantum_multi(quanta: &[[u8; 4]], schema: &Schema) -> UnpackedQuantum {
    let desc = &quanta[0];
    let schema_id = ((desc[0] as u16) << 8) | desc[1] as u16;
    let version = (desc[2] >> 4) & 0x0F;
    let flags = desc[2] & 0x0F;
    let payload_len = desc[3] as usize;
    let mut raw = Vec::with_capacity(payload_len);
    for q in &quanta[1..] {
        if raw.len() >= payload_len { break; }
        let remaining = payload_len - raw.len();
        let take = remaining.min(4);
        raw.extend_from_slice(&q[..take]);
    }
    UnpackedQuantum { schema_id, version, flags, value: unpack_value(schema, &raw) }
}

// ── Pipe path ────────────────────────────────────────────────────────────────
pub struct PipeOutput {
    pub buffer: Vec<u8>,
    pub schema_id: u16,
    pub version: u8,
    pub checksum: u32,
}

pub fn pack_pipe(schema: &Schema, value: &Value) -> PipeOutput {
    let buffer = pack_value(schema, value);
    let checksum = pipe::fnv1a32(&buffer);
    PipeOutput {
        schema_id: schema.schema_id,
        version: schema.version,
        checksum,
        buffer,
    }
}

pub fn unpack_pipe(buf: &[u8], schema: &Schema) -> Value {
    unpack_value(schema, buf)
}

// ── OpenPipe ─────────────────────────────────────────────────────────────────
pub fn pack_openpipe(session_id: u16, total_len: u32, chunk_size: u16,
                     obj_checksum: u32, schema_id: u16, schema_version: u8,
                     flags: u8) -> Vec<u8> {
    let mut out = pipe::pack_open(session_id, total_len, chunk_size, obj_checksum); // 14 B
    out.push((schema_id >> 8) as u8);
    out.push((schema_id & 0xFF) as u8);
    out.push(((schema_version & 0x0F) << 4) | (flags & 0x0F));
    out // 17 B
}

pub struct OpenPipeFields {
    pub session_id: u16,
    pub total_len: u32,
    pub chunk_size: u16,
    pub checksum: u32,
    pub schema_id: u16,
    pub schema_version: u8,
    pub flags: u8,
}

pub fn unpack_openpipe(buf: &[u8]) -> Option<OpenPipeFields> {
    if buf.len() < OPENPIPE_LEN { return None; }
    let (session_id, total_len, chunk_size, checksum) = pipe::unpack_open(&buf[..14])?;
    let schema_id = ((buf[14] as u16) << 8) | buf[15] as u16;
    let vf = buf[16];
    Some(OpenPipeFields {
        session_id, total_len, chunk_size, checksum,
        schema_id,
        schema_version: (vf >> 4) & 0x0F,
        flags: vf & 0x0F,
    })
}

// ── Plane selection ──────────────────────────────────────────────────────────
pub fn plane_select(schema: &Schema, threshold: usize) -> &'static str {
    if schema.packed_size() <= threshold { "quantum" } else { "pipe" }
}

#[cfg(test)]
mod tests {
    use super::*;

    static SUB_POS: [Field; 2] = [Field::u("x", 12), Field::u("y", 12)];
    static SUB_VEL: [Field; 2] = [Field::i("vx", 8), Field::i("vy", 8)];

    static S0: Schema = Schema::new(0, 1, &[
        Field::u("x", 10), Field::u("y", 10), Field::i("v", 8), Field::bool_("hot"),
    ]);
    static S3: Schema = Schema::new(3, 1, &[
        Field::u("x", 12), Field::u("y", 12), Field::i("vx", 8), Field::i("vy", 8),
        Field::u("heading", 8), Field::u("flags", 8),
    ]);
    static S5: Schema = Schema::new(5, 1, &[
        Field::struct_("pos", &SUB_POS),
        Field::struct_("vel", &SUB_VEL),
        Field::u("id", 8),
    ]);

    #[test]
    fn single_quantum_roundtrip() {
        let v = Value::from_slice(&[1000, 700, -3], &[true]);
        let q = pack_quantum(&S0, &v, 0, false).unwrap();
        assert_eq!(q.len(), 1);
        let r = unpack_quantum_single(&q, &S0);
        assert_eq!(r.schema_id, 0);
        assert_eq!(r.version, 1);
        assert_eq!(r.flags, 0);
        assert_eq!(r.value.ints[0], 1000);
        assert_eq!(r.value.ints[1], 700);
        assert_eq!(r.value.ints[2], -3);
        assert!(r.value.bools[0]);
    }

    #[test]
    fn multi_quantum_roundtrip() {
        let v = Value::from_slice(&[3500, 2800, -12, 7, 180, 0x42], &[]);
        let q = pack_quantum(&S3, &v, 0x5, false).unwrap();
        assert!(q.len() > 1);
        let r = unpack_quantum_multi(&q, &S3);
        assert_eq!(r.schema_id, 3);
        assert_eq!(r.version, 1);
        assert_eq!(r.flags, 0x5);
        assert_eq!(r.value.ints[0], 3500);
        assert_eq!(r.value.ints[2], -12);
    }

    #[test]
    fn struct_roundtrip() {
        let v = Value::from_slice(&[3500, 2800, -1, 2, 42], &[]);
        let q = pack_quantum(&S5, &v, 0, false).unwrap();
        let r = unpack_quantum_multi(&q, &S5);
        assert_eq!(r.value.ints[0], 3500);
        assert_eq!(r.value.ints[4], 42);
    }

    #[test]
    fn pipe_roundtrip() {
        let v = Value::from_slice(&[3500, 2800, -12, 7, 180, 0x42], &[]);
        let out = pack_pipe(&S3, &v);
        assert_eq!(out.schema_id, 3);
        assert_eq!(out.checksum, pipe::fnv1a32(&out.buffer));
        let back = unpack_pipe(&out.buffer, &S3);
        assert_eq!(back.ints[0], 3500);
    }

    #[test]
    fn openpipe_roundtrip() {
        let op = pack_openpipe(7, 100000, 1400, 0xDEADBEEF, 3, 1, 0x5);
        assert_eq!(op.len(), 17);
        let r = unpack_openpipe(&op).unwrap();
        assert_eq!(r.session_id, 7);
        assert_eq!(r.total_len, 100000);
        assert_eq!(r.chunk_size, 1400);
        assert_eq!(r.checksum, 0xDEADBEEF);
        assert_eq!(r.schema_id, 3);
        assert_eq!(r.schema_version, 1);
        assert_eq!(r.flags, 0x5);
    }

    #[test]
    fn fnv_anchors() {
        assert_eq!(pipe::fnv1a32(b""), 0x811C9DC5);
        assert_eq!(pipe::fnv1a32(b"hello"), 0x4F9F2CAB);
    }
}