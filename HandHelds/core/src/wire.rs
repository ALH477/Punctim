// SPDX-License-Identifier: LGPL-3.0-only
//! DCF wire quantum for handhelds — the certified 17-byte `DeModFrame` plus the
//! 32-byte SuperPack container, reimplemented `core`-only (no `std`, no `alloc`,
//! no external crates) so it builds on the DSi (ARM9) and PSP (MIPS) `no_std`
//! targets.
//!
//! This is **byte-identical** to the reference codecs in `codec/frame.rs` and
//! `codec/src/superpack.rs`, so handheld traffic is on-air compatible with the
//! rest of the Punctim mesh and satisfies the same 246-vector wire certificate.
//! Anchors pinned by [`selftest`]:
//!   * `crc16_ccitt("123456789") == 0x29B1`
//!   * `crc16_ccitt([0u8; 15])  == 0x4EC3`
//!   * zero-core SuperPack joint CRC `== 0x5B75`
//!
//! On top of the quantum it provides a DCF-Game-style L2 fragmenter
//! ([`packetize`] / [`Reassembler`]) — one message becomes `1 + ceil(len/4)`
//! ordinary `DATA` frames — so a whole game/chat message crosses the wire as
//! valid `DeModFrame`s. See `Documentation/WIRE_QUANTUM_SPEC.md`,
//! `Documentation/SUPERPACK_SPEC.md`, and `Documentation/DCF_GAME_SPEC.md`.

#![allow(dead_code)]

// ── Wire quantum constants ──────────────────────────────────────────────────
pub const FRAME_SIZE: usize = 17;
pub const SYNC_BYTE: u8 = 0xD3;
pub const BROADCAST: u16 = 0xFFFF;
const VERSION: u8 = 1;
const CRC_COVER: usize = 15; // bytes [0..14] are covered by the frame CRC

// ── CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) ───────────────────────────
// Identical algorithm to the C/Rust/Python/Go/… references; this is the
// cross-language wire-compatibility primitive.
pub fn crc16_ccitt(data: &[u8]) -> u16 {
    let mut crc: u16 = 0xFFFF;
    for &byte in data {
        crc ^= (byte as u16) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 {
                (crc << 1) ^ 0x1021
            } else {
                crc << 1
            };
        }
    }
    crc
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum FrameType {
    Data = 0,
    Ack = 1,
    Beacon = 2,
    Ctrl = 3,
}

impl FrameType {
    fn from_nibble(n: u8) -> Option<Self> {
        match n & 0x0F {
            0 => Some(FrameType::Data),
            1 => Some(FrameType::Ack),
            2 => Some(FrameType::Beacon),
            3 => Some(FrameType::Ctrl),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameError {
    BadLength,
    BadSync,
    BadCrc,
    UnknownType,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Frame {
    pub version: u8,
    pub frame_type: FrameType,
    pub seq: u16,
    pub src_id: u16,
    pub dst_id: u16,
    pub payload: [u8; 4],
    pub timestamp_us: u32,
}

impl Frame {
    pub fn new(
        version: u8,
        frame_type: FrameType,
        seq: u16,
        src_id: u16,
        dst_id: u16,
        payload: [u8; 4],
        timestamp_us: u32,
    ) -> Self {
        Frame { version, frame_type, seq, src_id, dst_id, payload, timestamp_us }
    }

    /// Serialise into a 17-byte wire buffer (all multi-byte fields big-endian),
    /// computing and appending the CRC.
    pub fn encode(&self) -> [u8; FRAME_SIZE] {
        let mut buf = [0u8; FRAME_SIZE];
        buf[0] = SYNC_BYTE;
        buf[1] = ((self.version & 0x0F) << 4) | (self.frame_type as u8);
        buf[2] = (self.seq >> 8) as u8;
        buf[3] = self.seq as u8;
        buf[4] = (self.src_id >> 8) as u8;
        buf[5] = self.src_id as u8;
        buf[6] = (self.dst_id >> 8) as u8;
        buf[7] = self.dst_id as u8;
        buf[8..12].copy_from_slice(&self.payload);
        buf[12] = ((self.timestamp_us >> 16) & 0xFF) as u8;
        buf[13] = ((self.timestamp_us >> 8) & 0xFF) as u8;
        buf[14] = (self.timestamp_us & 0xFF) as u8;
        let crc = crc16_ccitt(&buf[..CRC_COVER]);
        buf[15] = (crc >> 8) as u8;
        buf[16] = crc as u8;
        buf
    }

    /// Parse a 17-byte buffer; only `Ok` on a fully valid frame (sync + CRC + type).
    pub fn decode(buf: &[u8]) -> Result<Self, FrameError> {
        if buf.len() != FRAME_SIZE {
            return Err(FrameError::BadLength);
        }
        if buf[0] != SYNC_BYTE {
            return Err(FrameError::BadSync);
        }
        let crc_calc = crc16_ccitt(&buf[..CRC_COVER]);
        let crc_stored = ((buf[15] as u16) << 8) | (buf[16] as u16);
        if crc_calc != crc_stored {
            return Err(FrameError::BadCrc);
        }
        let frame_type = FrameType::from_nibble(buf[1] & 0x0F).ok_or(FrameError::UnknownType)?;
        let mut payload = [0u8; 4];
        payload.copy_from_slice(&buf[8..12]);
        Ok(Frame {
            version: (buf[1] >> 4) & 0x0F,
            frame_type,
            seq: ((buf[2] as u16) << 8) | buf[3] as u16,
            src_id: ((buf[4] as u16) << 8) | buf[5] as u16,
            dst_id: ((buf[6] as u16) << 8) | buf[7] as u16,
            payload,
            timestamp_us: ((buf[12] as u32) << 16) | ((buf[13] as u32) << 8) | (buf[14] as u32),
        })
    }

    /// Non-allocating structural check (sync + CRC).
    pub fn is_valid(buf: &[u8]) -> bool {
        buf.len() == FRAME_SIZE
            && buf[0] == SYNC_BYTE
            && crc16_ccitt(&buf[..CRC_COVER]) == (((buf[15] as u16) << 8) | buf[16] as u16)
    }
}

// ── SuperPack: two 17-byte frames in one 32-byte container ───────────────────
pub const SUPER_TYPE: u8 = 0x05;
pub const SUPER_LEN: usize = 32;
pub const CORE_LEN: usize = 14;
const SFLAGS: u8 = (VERSION << 4) | SUPER_TYPE;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SuperPackError {
    BadLength,
    BadSync,
    BadVersion,
    BadType,
    InnerCrc,
    JointCrc,
    InnerDecode,
}

fn frame_core(frame: &[u8; FRAME_SIZE]) -> Result<[u8; CORE_LEN], SuperPackError> {
    if frame[0] != SYNC_BYTE {
        return Err(SuperPackError::BadSync);
    }
    if frame[1] >> 4 != VERSION {
        return Err(SuperPackError::BadVersion);
    }
    let stored = (u16::from(frame[15]) << 8) | u16::from(frame[16]);
    if crc16_ccitt(&frame[..15]) != stored {
        return Err(SuperPackError::InnerCrc);
    }
    let mut core = [0u8; CORE_LEN];
    core.copy_from_slice(&frame[1..15]);
    Ok(core)
}

fn rebuild_frame(core: &[u8; CORE_LEN]) -> [u8; FRAME_SIZE] {
    let mut out = [0u8; FRAME_SIZE];
    out[0] = SYNC_BYTE;
    out[1..15].copy_from_slice(core);
    let crc = crc16_ccitt(&out[..15]);
    out[15] = (crc >> 8) as u8;
    out[16] = crc as u8;
    out
}

/// Combine two valid frames into one 32-byte SuperPack — the lower-latency option
/// for paired sends (one datagram, one header, one syscall instead of two).
pub fn pack(
    frame_a: &[u8; FRAME_SIZE],
    frame_b: &[u8; FRAME_SIZE],
) -> Result<[u8; SUPER_LEN], SuperPackError> {
    let core_a = frame_core(frame_a)?;
    let core_b = frame_core(frame_b)?;
    let mut out = [0u8; SUPER_LEN];
    out[0] = SYNC_BYTE;
    out[1] = SFLAGS;
    out[2..2 + CORE_LEN].copy_from_slice(&core_a);
    out[2 + CORE_LEN..2 + 2 * CORE_LEN].copy_from_slice(&core_b);
    let crc = crc16_ccitt(&out[..30]);
    out[30] = (crc >> 8) as u8;
    out[31] = crc as u8;
    Ok(out)
}

pub fn is_superpack(buf: &[u8]) -> bool {
    buf.len() == SUPER_LEN && buf[0] == SYNC_BYTE && buf[1] == SFLAGS
}

/// Split a SuperPack back into two bit-exact, fully valid `DeModFrame`s.
pub fn unpack(buf: &[u8]) -> Result<([u8; FRAME_SIZE], [u8; FRAME_SIZE]), SuperPackError> {
    if buf.len() != SUPER_LEN {
        return Err(SuperPackError::BadLength);
    }
    if buf[0] != SYNC_BYTE {
        return Err(SuperPackError::BadSync);
    }
    if buf[1] >> 4 != VERSION {
        return Err(SuperPackError::BadVersion);
    }
    if buf[1] & 0x0F != SUPER_TYPE {
        return Err(SuperPackError::BadType);
    }
    let stored = (u16::from(buf[30]) << 8) | u16::from(buf[31]);
    if crc16_ccitt(&buf[..30]) != stored {
        return Err(SuperPackError::JointCrc);
    }
    let mut core_a = [0u8; CORE_LEN];
    let mut core_b = [0u8; CORE_LEN];
    core_a.copy_from_slice(&buf[2..2 + CORE_LEN]);
    core_b.copy_from_slice(&buf[2 + CORE_LEN..2 + 2 * CORE_LEN]);
    let frame_a = rebuild_frame(&core_a);
    let frame_b = rebuild_frame(&core_b);
    Frame::decode(&frame_a).map_err(|_| SuperPackError::InnerDecode)?;
    Frame::decode(&frame_b).map_err(|_| SuperPackError::InnerDecode)?;
    Ok((frame_a, frame_b))
}

// ── DCF-Game-style L2 fragmenter (DATA frames) ──────────────────────────────
// seq = packet_id[15:5] | frag_idx[4:0]
//   frag_idx 0 : descriptor [payload_len, frag_total, msg_type_id, flags]
//   frag_idx k : data bytes[(k-1)*4 .. +4]  (last frame zero-padded)
// payload_len <= 124 (31 data fragments * 4). Mirrors codec/src/game.rs so the
// handheld interoperates with the certified DCF-Game adapter.
pub const FRAG_BITS: u8 = 5;
pub const MAX_FRAGS: usize = 31;
pub const MAX_PAYLOAD: usize = MAX_FRAGS * 4; // 124
/// Worst-case frame count for one message: 1 descriptor + 31 data frames.
pub const MAX_FRAMES: usize = 1 + MAX_FRAGS;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WireError {
    PayloadTooLarge,
    OutTooSmall,
}

fn ceil_div4(n: usize) -> usize {
    (n + 3) / 4
}

/// Serialise one message into `1 + ceil(len/4)` `DeModFrame` `DATA` frames written
/// into `out`. Returns the number of frames written. `payload` must be <= 124 B.
pub fn packetize(
    payload: &[u8],
    packet_id: u16,
    src: u16,
    dst: u16,
    ts_us: u32,
    msg_type_id: u8,
    flags: u8,
    out: &mut [[u8; FRAME_SIZE]],
) -> Result<usize, WireError> {
    if payload.len() > MAX_PAYLOAD {
        return Err(WireError::PayloadTooLarge);
    }
    let frag_total = ceil_div4(payload.len());
    let need = 1 + frag_total;
    if out.len() < need {
        return Err(WireError::OutTooSmall);
    }
    let pid = packet_id & 0x07FF; // 11-bit packet id

    // frag_idx 0 — descriptor
    let desc_seq = pid << FRAG_BITS;
    let desc = [payload.len() as u8, frag_total as u8, msg_type_id, flags];
    out[0] = Frame::new(VERSION, FrameType::Data, desc_seq, src, dst, desc, ts_us).encode();

    // frag_idx 1..frag_total — data (last chunk zero-padded)
    for k in 1..=frag_total {
        let off = (k - 1) * 4;
        let mut chunk = [0u8; 4];
        let end = core::cmp::min(off + 4, payload.len());
        chunk[..end - off].copy_from_slice(&payload[off..end]);
        let seq = (pid << FRAG_BITS) | (k as u16);
        out[k] = Frame::new(VERSION, FrameType::Data, seq, src, dst, chunk, ts_us).encode();
    }
    Ok(need)
}

/// Single-message reassembler: accepts `DATA` frames (descriptor + fragments) for
/// one in-flight `packet_id` and yields the payload once complete. Sized for the
/// 124-byte game/chat quantum; no heap, one slot (turn-based handheld traffic).
pub struct Reassembler {
    active: bool,
    packet_id: u16,
    have_desc: bool,
    payload_len: u8,
    frag_total: u8,
    pub msg_type_id: u8,
    pub flags: u8,
    present: u32, // bit k = data fragment k present (1..=31)
    data: [u8; MAX_PAYLOAD],
}

impl Reassembler {
    pub fn new() -> Self {
        Reassembler {
            active: false,
            packet_id: 0,
            have_desc: false,
            payload_len: 0,
            frag_total: 0,
            msg_type_id: 0,
            flags: 0,
            present: 0,
            data: [0u8; MAX_PAYLOAD],
        }
    }

    fn reset_to(&mut self, pid: u16) {
        self.active = true;
        self.packet_id = pid;
        self.have_desc = false;
        self.payload_len = 0;
        self.frag_total = 0;
        self.msg_type_id = 0;
        self.flags = 0;
        self.present = 0;
        self.data = [0u8; MAX_PAYLOAD];
    }

    /// Push one 17-byte frame. On completion, copies the message into `out` and
    /// returns `Some(len)`. Non-DATA frames, bad frames, and duplicates are ignored.
    pub fn push(&mut self, frame: &[u8], out: &mut [u8; MAX_PAYLOAD]) -> Option<usize> {
        let d = Frame::decode(frame).ok()?;
        if d.frame_type != FrameType::Data {
            return None;
        }
        let pid = d.seq >> FRAG_BITS;
        let frag_idx = (d.seq & ((1 << FRAG_BITS) - 1)) as usize;

        if !self.active || self.packet_id != pid {
            self.reset_to(pid);
        }

        if frag_idx == 0 {
            if !self.have_desc {
                self.have_desc = true;
                self.payload_len = d.payload[0];
                self.frag_total = d.payload[1];
                self.msg_type_id = d.payload[2];
                self.flags = d.payload[3];
            }
        } else if frag_idx <= MAX_FRAGS {
            let bit = 1u32 << frag_idx;
            if self.present & bit == 0 {
                self.present |= bit;
                let off = (frag_idx - 1) * 4;
                self.data[off..off + 4].copy_from_slice(&d.payload);
            }
        }
        self.try_emit(out)
    }

    fn try_emit(&mut self, out: &mut [u8; MAX_PAYLOAD]) -> Option<usize> {
        if !self.have_desc {
            return None;
        }
        for k in 1..=(self.frag_total as usize) {
            if self.present & (1u32 << k) == 0 {
                return None;
            }
        }
        let len = self.payload_len as usize;
        out[..len].copy_from_slice(&self.data[..len]);
        self.active = false;
        self.have_desc = false;
        Some(len)
    }
}

impl Default for Reassembler {
    fn default() -> Self {
        Self::new()
    }
}

/// Runtime sanity check of the cross-language anchors. Returns `true` iff this
/// codec agrees with the rest of the mesh on the wire. Cheap enough to call on
/// device at boot.
pub fn selftest() -> bool {
    crc16_ccitt(b"123456789") == 0x29B1
        && crc16_ccitt(&[0u8; 15]) == 0x4EC3
        && {
            let zero = Frame::new(1, FrameType::Data, 0, 0, 0, [0, 0, 0, 0], 0).encode();
            match pack(&zero, &zero) {
                Ok(sp) => ((u16::from(sp[30]) << 8) | u16::from(sp[31])) == 0x5B75,
                Err(_) => false,
            }
        }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc_anchors() {
        assert_eq!(crc16_ccitt(b"123456789"), 0x29B1);
        assert_eq!(crc16_ccitt(&[0u8; 15]), 0x4EC3);
    }

    #[test]
    fn frame_roundtrip_and_layout() {
        let f = Frame::new(1, FrameType::Data, 1, 1, BROADCAST, [0xDE, 0xAD, 0xBE, 0xEF], 0);
        let w = f.encode();
        assert_eq!(w[0], SYNC_BYTE);
        assert_eq!(w[1], 0x10); // version 1, Data
        assert_eq!(Frame::decode(&w).unwrap(), f);
        let mut bad = w;
        bad[9] ^= 0xFF;
        assert_eq!(Frame::decode(&bad), Err(FrameError::BadCrc));
    }

    #[test]
    fn superpack_roundtrip_and_zero_anchor() {
        let a = Frame::new(1, FrameType::Data, 0x0102, 0x0304, 0x0506, [0xde, 0xad, 0xbe, 0xef], 0x010203).encode();
        let b = Frame::new(1, FrameType::Ctrl, 0x1f, 0x0007, 0xffff, [b'c', b'h', b'a', b't'], 0x0000ff).encode();
        let sp = pack(&a, &b).unwrap();
        assert!(is_superpack(&sp));
        assert_eq!(unpack(&sp).unwrap(), (a, b));

        let zero = Frame::new(1, FrameType::Data, 0, 0, 0, [0, 0, 0, 0], 0).encode();
        let spz = pack(&zero, &zero).unwrap();
        assert_eq!((u16::from(spz[30]) << 8) | u16::from(spz[31]), 0x5B75);
    }

    #[test]
    fn packetize_reassemble_identity() {
        let msg = b"MOVE:4 and a longer chat string crossing several fragments!!";
        let mut frames = [[0u8; FRAME_SIZE]; MAX_FRAMES];
        let n = packetize(msg, 42, 7, BROADCAST, 0x010203, 0, 0, &mut frames).unwrap();
        assert_eq!(n, 1 + ((msg.len() + 3) / 4));

        let mut r = Reassembler::new();
        let mut out = [0u8; MAX_PAYLOAD];
        let mut got = None;
        for f in &frames[..n] {
            if let Some(len) = r.push(f, &mut out) {
                got = Some(len);
            }
        }
        let len = got.expect("message must reassemble");
        assert_eq!(&out[..len], msg);
    }

    #[test]
    fn reassemble_tolerates_reorder_and_dup() {
        let msg = b"hello handheld";
        let mut frames = [[0u8; FRAME_SIZE]; MAX_FRAMES];
        let n = packetize(msg, 3, 1, 2, 1, 0, 0, &mut frames).unwrap();
        let mut r = Reassembler::new();
        let mut out = [0u8; MAX_PAYLOAD];
        // deliver data frames first, with a duplicate, descriptor last
        let order: [usize; 6] = [2, 2, 3, 1, 4, 0];
        let mut got = None;
        for &i in order.iter().take(n + 2) {
            if i < n {
                if let Some(len) = r.push(&frames[i], &mut out) {
                    got = Some(len);
                }
            }
        }
        let len = got.unwrap();
        assert_eq!(&out[..len], msg);
    }

    #[test]
    fn selftest_passes() {
        assert!(selftest());
    }
}
