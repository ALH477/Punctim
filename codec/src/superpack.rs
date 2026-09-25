// SPDX-License-Identifier: LGPL-3.0-only
//! DCF SuperPack — a 32-byte container that losslessly carries TWO 17-byte
//! `DeModFrame` quanta under a single joint CRC.
//!
//! Two raw frames cost `2 * 17 = 34` bytes. When you are already sending frames
//! in pairs (a descriptor + its first data fragment, or two fragments of one
//! message), most of the second header is recoverable from context: both inner
//! sync bytes are the constant `0xD3`, and each inner CRC is a pure function of
//! its own 15 leading bytes. SuperPack drops those 6 redundant bytes and spends
//! 4 back on one outer sync, a type/version tag, and ONE joint CRC over the whole
//! container — net `34 -> 32` bytes and a strictly stronger integrity check.
//!
//! **Why it is the lower-latency option:** a SuperPack puts a frame pair on the
//! wire as a *single* datagram instead of two — one packet, one IP/UDP header,
//! one syscall — so paired traffic crosses the network with strictly less
//! per-pair overhead and latency than emitting the two frames separately.
//!
//! Unpack reconstructs each inner frame bit-exact, so the two emitted frames are
//! ordinary, fully valid `DeModFrame`s and the 246-vector wire certificate is
//! untouched. SuperPack is a *container adapter*, never a change to the quantum.

use crate::{crc16_ccitt, SYNC_BYTE};

/// 4-bit type nibble that tags a SuperPack container.
pub const SUPER_TYPE: u8 = 0x05;
/// Total container length in bytes.
pub const SUPER_LEN: usize = 32;
/// A frame's reconstructable core: bytes `[1..15]` (everything but sync and CRC).
pub const CORE_LEN: usize = 14;
const VERSION: u8 = 1;
/// `sflags` byte = version nibble (1) in the high nibble, SUPER type in the low.
const SFLAGS: u8 = (VERSION << 4) | SUPER_TYPE;

/// Errors raised while packing or unpacking a SuperPack.
#[derive(Debug, PartialEq, Eq)]
pub enum SuperPackError {
    BadLength,
    BadSync,
    BadVersion,
    BadType,
    InnerCrc,
    JointCrc,
    InnerDecode,
}

/// The 14 reconstructable bytes of a 17-byte `DeModFrame` (drop sync + CRC).
/// Validates sync, version nibble, and the inner CRC so a corrupt frame is never packed.
fn frame_core(frame: &[u8; 17]) -> Result<[u8; CORE_LEN], SuperPackError> {
    if frame[0] != SYNC_BYTE {
        return Err(SuperPackError::BadSync);
    }
    if frame[1] >> 4 != VERSION {
        return Err(SuperPackError::BadVersion);
    }
    let stored = u16::from(frame[15]) << 8 | u16::from(frame[16]);
    if crc16_ccitt(&frame[..15]) != stored {
        return Err(SuperPackError::InnerCrc);
    }
    let mut core = [0u8; CORE_LEN];
    core.copy_from_slice(&frame[1..15]);
    Ok(core)
}

/// Restore a full 17-byte `DeModFrame` from its 14-byte core (sync + recomputed CRC).
fn rebuild_frame(core: &[u8; CORE_LEN]) -> [u8; 17] {
    let mut out = [0u8; 17];
    out[0] = SYNC_BYTE;
    out[1..15].copy_from_slice(core);
    let crc = crc16_ccitt(&out[..15]);
    out[15] = (crc >> 8) as u8;
    out[16] = crc as u8;
    out
}

/// Combine two valid 17-byte `DeModFrame`s into one 32-byte SuperPack.
pub fn pack(frame_a: &[u8; 17], frame_b: &[u8; 17]) -> Result<[u8; SUPER_LEN], SuperPackError> {
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

/// True iff `buf` looks like a SuperPack (length + sync + version/type tag).
pub fn is_superpack(buf: &[u8]) -> bool {
    buf.len() == SUPER_LEN && buf[0] == SYNC_BYTE && buf[1] == SFLAGS
}

/// The frame gate: sync `0xD3` + version nibble 1 + CRC-16/CCITT-FALSE. The 4-bit type
/// nibble is NOT part of it — reserved types 4..15 are valid on the wire (the golden
/// certificate's encode basis carries types 4 and 8), so a container never rejects them.
fn gate(frame: &[u8; 17]) -> bool {
    frame[0] == SYNC_BYTE
        && frame[1] >> 4 == VERSION
        && crc16_ccitt(&frame[..15]) == (u16::from(frame[15]) << 8 | u16::from(frame[16]))
}

/// Split a 32-byte SuperPack back into `(frame_a, frame_b)`, each a bit-exact,
/// fully valid 17-byte `DeModFrame` (any type nibble). Returns `Err` on any integrity
/// failure — exactly the checks of `python/MCP/superpack.py:unpack`.
pub fn unpack(buf: &[u8]) -> Result<([u8; 17], [u8; 17]), SuperPackError> {
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
    let stored = u16::from(buf[30]) << 8 | u16::from(buf[31]);
    if crc16_ccitt(&buf[..30]) != stored {
        return Err(SuperPackError::JointCrc);
    }
    let mut core_a = [0u8; CORE_LEN];
    let mut core_b = [0u8; CORE_LEN];
    core_a.copy_from_slice(&buf[2..2 + CORE_LEN]);
    core_b.copy_from_slice(&buf[2 + CORE_LEN..2 + 2 * CORE_LEN]);
    let frame_a = rebuild_frame(&core_a);
    let frame_b = rebuild_frame(&core_b);
    // Belt and braces: the rebuilt frames must themselves pass the frame gate. (Not
    // `Frame::decode`, which also rejects the reserved type nibbles 4..15 and does not
    // check the version nibble — both diverge from the Python/C references.)
    if !gate(&frame_a) || !gate(&frame_b) {
        return Err(SuperPackError::InnerDecode);
    }
    Ok((frame_a, frame_b))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Frame;

    #[test]
    fn roundtrip_and_anchor() {
        let a = Frame::new(1, crate::FrameType::Data, 0x0102, 0x0304, 0x0506, [0xde, 0xad, 0xbe, 0xef], 0x010203).encode();
        let b = Frame::new(1, crate::FrameType::Ctrl, 0x1f, 0x0007, 0xffff, [b'c', b'h', b'a', b't'], 0x0000ff).encode();
        let sp = pack(&a, &b).unwrap();
        assert_eq!(sp.len(), 32);
        assert!(is_superpack(&sp));
        assert_eq!(unpack(&sp).unwrap(), (a, b));

        let zero = Frame::new(1, crate::FrameType::Data, 0, 0, 0, [0, 0, 0, 0], 0).encode();
        let spz = pack(&zero, &zero).unwrap();
        assert_eq!(u16::from(spz[30]) << 8 | u16::from(spz[31]), 0x5B75);
    }

    fn gated(t: u8, seq: u16) -> [u8; 17] {
        let mut f = [0u8; 17];
        f[0] = SYNC_BYTE;
        f[1] = (VERSION << 4) | t;
        f[2..4].copy_from_slice(&seq.to_be_bytes());
        let crc = crc16_ccitt(&f[..15]);
        f[15..17].copy_from_slice(&crc.to_be_bytes());
        f
    }

    fn unhex(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }

    /// The frame gate is sync + version nibble 1 + CRC — the type nibble is NOT gated, so
    /// every type 0..15 must round-trip (as in python/MCP/superpack.py and
    /// codec/demod_superpack.h), and an inner core whose version nibble is not 1 is
    /// rejected (as the Python reference's `decode` does).
    #[test]
    fn any_type_nibble_roundtrips() {
        for t in 0..16u8 {
            let (a, b) = (gated(t, 0x0404), gated(15 - t, 0xFFF0));
            let sp = pack(&a, &b).unwrap();
            assert_eq!(unpack(&sp).unwrap(), (a, b), "type {t}");
        }
        // medium_vectors.json udp_bare "pair_type4_type15"
        let t4 = unhex("d314040400040400040404040404044b96");
        let t15 = unhex("d31ffff00f0ff0f00ff00ff00f0f0f5365");
        let sp = unhex("d31514040400040400040404040404041ffff00f0ff0f00ff00ff00f0f0f0cb8");
        let (a, b) = unpack(&sp).unwrap();
        assert_eq!((a.to_vec(), b.to_vec()), (t4.clone(), t15.clone()));
        let pa: [u8; 17] = t4.try_into().unwrap();
        let pb: [u8; 17] = t15.try_into().unwrap();
        assert_eq!(pack(&pa, &pb).unwrap().to_vec(), sp);
    }

    #[test]
    fn inner_version_nibble_is_gated() {
        let mut sp = pack(&gated(0, 1), &gated(0, 2)).unwrap();
        sp[16] = 2 << 4; // frame B's flags byte -> version nibble 2 (type 0, a known type)
        let crc = crc16_ccitt(&sp[..30]);
        sp[30..32].copy_from_slice(&crc.to_be_bytes());
        assert_eq!(unpack(&sp), Err(SuperPackError::InnerDecode));
    }
}
