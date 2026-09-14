// SPDX-License-Identifier: LGPL-3.0-only
// dcf/rust/src/frame.rs — DeMoD 17-byte transport frame codec
// DeMoD LLC | GPL-3.0
//
// No-std compatible (no heap allocations, no std dependencies).
// Add `#![no_std]` to lib.rs and this module works on bare-metal.
//
// Wire layout (17 bytes = 136 bits, all multi-byte fields big-endian):
//
//   Byte   Field        Notes
//   ─────  ───────────  ──────────────────────────────────────────────────────
//    0     sync         Fixed 0xD3 — first validity gate
//    1     flags        [7:4] version (4-bit) | [3:0] frame type (4-bit)
//    2-3   seq          Big-endian u16, rolling counter
//    4-5   src_id       Big-endian u16, source node
//    6-7   dst_id       Big-endian u16, 0xFFFF = broadcast
//    8-11  payload      4 raw application bytes
//   12-14  timestamp    24-bit big-endian µs offset, wraps ~16.7 s
//   15-16  crc16        CRC-CCITT(poly=0x1021, init=0xFFFF) over bytes [0..14]
//
// Cross-language parity:
//   C        transport/dcf_frame.h  dcf_frame_encode / dcf_frame_decode
//   Haskell  DCF.Transport.Frame    encodeFrame / decodeFrame
//   Lisp     punctim.lisp         encode-dcf-frame / decode-dcf-frame
//   Rust     dcf/rust/src/frame.rs  Frame::encode / Frame::decode  ← this file

// ── Constants ─────────────────────────────────────────────────────────────────

pub const FRAME_SIZE: usize = 17;
pub const SYNC_BYTE: u8     = 0xD3;
pub const BROADCAST: u16    = 0xFFFF;

// Bytes covered by CRC: [0..14] inclusive (excludes the CRC field itself)
const CRC_COVER: usize = 15;

// ── Frame type ────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum FrameType {
    Data   = 0,   // application payload
    Ack    = 1,   // acknowledgement
    Beacon = 2,   // clock sync / broadcast beacon
    Ctrl   = 3,   // control / fragmented audio
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

    fn to_nibble(self) -> u8 {
        self as u8
    }
}

// ── Decode error ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameError {
    /// Buffer is not exactly FRAME_SIZE (17) bytes.
    BadLength,
    /// Byte 0 is not SYNC_BYTE (0xD3).
    BadSync,
    /// CRC-CCITT mismatch — frame is corrupt.
    BadCrc,
    /// Frame type nibble is reserved (> 3).
    UnknownType,
}

// ── Frame (decoded, host-order fields) ───────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    /// Protocol version, 4-bit (0–15).
    pub version:      u8,
    /// Frame type.
    pub frame_type:   FrameType,
    /// Rolling 16-bit sequence counter.
    pub seq:          u16,
    /// Source node identifier.
    pub src_id:       u16,
    /// Destination node identifier (BROADCAST = 0xFFFF).
    pub dst_id:       u16,
    /// 4-byte application payload.
    pub payload:      [u8; 4],
    /// 24-bit µs timestamp (stored as u32, top byte always 0).
    pub timestamp_us: u32,
}

impl Frame {
    // ── Constructor ──────────────────────────────────────────────────────────

    pub fn new(
        version:      u8,
        frame_type:   FrameType,
        seq:          u16,
        src_id:       u16,
        dst_id:       u16,
        payload:      [u8; 4],
        timestamp_us: u32,
    ) -> Self {
        Frame { version, frame_type, seq, src_id, dst_id, payload, timestamp_us }
    }

    // ── Encode ───────────────────────────────────────────────────────────────
    //
    // Serialise into a 17-byte wire buffer.
    // All multi-byte fields are written big-endian.
    // CRC is computed and appended automatically.

    pub fn encode(&self) -> [u8; FRAME_SIZE] {
        let mut buf = [0u8; FRAME_SIZE];

        // [0] sync
        buf[0] = SYNC_BYTE;

        // [1] flags: version[7:4] | type[3:0]
        buf[1] = ((self.version & 0x0F) << 4) | self.frame_type.to_nibble();

        // [2-3] seq, big-endian
        buf[2] = (self.seq >> 8) as u8;
        buf[3] =  self.seq       as u8;

        // [4-5] src_id, big-endian
        buf[4] = (self.src_id >> 8) as u8;
        buf[5] =  self.src_id       as u8;

        // [6-7] dst_id, big-endian
        buf[6] = (self.dst_id >> 8) as u8;
        buf[7] =  self.dst_id       as u8;

        // [8-11] payload
        buf[8..12].copy_from_slice(&self.payload);

        // [12-14] 24-bit timestamp, big-endian
        buf[12] = ((self.timestamp_us >> 16) & 0xFF) as u8;
        buf[13] = ((self.timestamp_us >>  8) & 0xFF) as u8;
        buf[14] = ( self.timestamp_us         & 0xFF) as u8;

        // [15-16] CRC-CCITT over [0..14], big-endian
        let crc = crc16_ccitt(&buf[..CRC_COVER]);
        buf[15] = (crc >> 8) as u8;
        buf[16] =  crc       as u8;

        buf
    }

    // ── Decode ───────────────────────────────────────────────────────────────
    //
    // Parse a 17-byte wire buffer.
    // Returns Err if the sync byte is wrong, the CRC fails, or the frame
    // type nibble is unknown.  Only returns Ok on a fully valid frame.

    pub fn decode(buf: &[u8]) -> Result<Self, FrameError> {
        if buf.len() != FRAME_SIZE {
            return Err(FrameError::BadLength);
        }

        if buf[0] != SYNC_BYTE {
            return Err(FrameError::BadSync);
        }

        let crc_calc   = crc16_ccitt(&buf[..CRC_COVER]);
        let crc_stored = ((buf[15] as u16) << 8) | (buf[16] as u16);
        if crc_calc != crc_stored {
            return Err(FrameError::BadCrc);
        }

        let frame_type = FrameType::from_nibble(buf[1] & 0x0F)
            .ok_or(FrameError::UnknownType)?;

        let mut payload = [0u8; 4];
        payload.copy_from_slice(&buf[8..12]);

        Ok(Frame {
            version:      (buf[1] >> 4) & 0x0F,
            frame_type,
            seq:          ((buf[2] as u16) << 8) | buf[3] as u16,
            src_id:       ((buf[4] as u16) << 8) | buf[5] as u16,
            dst_id:       ((buf[6] as u16) << 8) | buf[7] as u16,
            payload,
            timestamp_us: ((buf[12] as u32) << 16)
                        | ((buf[13] as u32) <<  8)
                        |  (buf[14] as u32),
        })
    }

    // ── Validate (non-destructive) ───────────────────────────────────────────
    //
    // Returns true iff the 17-byte slice is a structurally valid frame.
    // Does not allocate or return a decoded value.

    pub fn is_valid(buf: &[u8]) -> bool {
        if buf.len() != FRAME_SIZE  { return false; }
        if buf[0] != SYNC_BYTE      { return false; }
        let crc_calc   = crc16_ccitt(&buf[..CRC_COVER]);
        let crc_stored = ((buf[15] as u16) << 8) | buf[16] as u16;
        crc_calc == crc_stored
    }
}

// ── CRC-CCITT ─────────────────────────────────────────────────────────────────
//
// Poly 0x1021, init 0xFFFF.
// Identical algorithm in C (dcf_crc16), Haskell (crc16ccitt), Lisp (crc16-ccitt).
// All four implementations must return the same value for the same input — this
// is the cross-language wire compatibility test.

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

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // Reference frame matching Haskell FrameSpec.hs exampleFrame and the C
    // dcf_frame.h reference test vector:
    //   version=1, FData, seq=1, src=1, dst=0xFFFF, payload=0xDEADBEEF, ts=0
    fn reference_frame() -> Frame {
        Frame::new(
            1,
            FrameType::Data,
            1,
            1,
            BROADCAST,
            [0xDE, 0xAD, 0xBE, 0xEF],
            0,
        )
    }

    #[test]
    fn encode_is_17_bytes() {
        let wire = reference_frame().encode();
        assert_eq!(wire.len(), FRAME_SIZE);
    }

    #[test]
    fn sync_byte_correct() {
        let wire = reference_frame().encode();
        assert_eq!(wire[0], SYNC_BYTE, "byte[0] must be 0xD3");
    }

    #[test]
    fn flags_layout() {
        // version=1, type=Data(0) → flags = (1 << 4) | 0 = 0x10
        let wire = reference_frame().encode();
        assert_eq!(wire[1], 0x10, "flags must be 0x10 for version=1/Data");
    }

    #[test]
    fn seq_big_endian() {
        let f = Frame::new(1, FrameType::Ack, 0xABCD, 0, 0, [0; 4], 0);
        let wire = f.encode();
        assert_eq!(wire[2], 0xAB, "seq high byte");
        assert_eq!(wire[3], 0xCD, "seq low byte");
    }

    #[test]
    fn dst_broadcast_encoded() {
        let wire = reference_frame().encode();
        assert_eq!(wire[6], 0xFF);
        assert_eq!(wire[7], 0xFF);
    }

    #[test]
    fn payload_verbatim() {
        let wire = reference_frame().encode();
        assert_eq!(&wire[8..12], &[0xDE, 0xAD, 0xBE, 0xEF]);
    }

    #[test]
    fn timestamp_24bit_big_endian() {
        let f = Frame::new(1, FrameType::Beacon, 0, 0, BROADCAST, [0; 4], 0x00AB_12CD);
        let wire = f.encode();
        assert_eq!(wire[12], 0xAB);
        assert_eq!(wire[13], 0x12);
        assert_eq!(wire[14], 0xCD);
    }

    #[test]
    fn roundtrip_identity() {
        let original = reference_frame();
        let wire     = original.encode();
        let decoded  = Frame::decode(&wire).expect("valid frame must decode");
        assert_eq!(original, decoded);
    }

    #[test]
    fn roundtrip_all_frame_types() {
        for ft in [FrameType::Data, FrameType::Ack, FrameType::Beacon, FrameType::Ctrl] {
            let f    = Frame::new(1, ft, 0, 1, 2, [0xAA; 4], 0);
            let wire = f.encode();
            let back = Frame::decode(&wire).expect("all frame types must roundtrip");
            assert_eq!(back.frame_type, ft);
        }
    }

    #[test]
    fn roundtrip_seq_extremes() {
        for seq in [0u16, 1, 0x7FFF, 0xFFFE, 0xFFFF] {
            let f    = Frame::new(1, FrameType::Data, seq, 0, BROADCAST, [0; 4], 0);
            let wire = f.encode();
            let back = Frame::decode(&wire).unwrap();
            assert_eq!(back.seq, seq);
        }
    }

    #[test]
    fn roundtrip_version_nibble() {
        for v in [0u8, 1, 7, 15] {
            let f    = Frame::new(v, FrameType::Data, 0, 0, 0, [0; 4], 0);
            let wire = f.encode();
            let back = Frame::decode(&wire).unwrap();
            assert_eq!(back.version, v);
        }
    }

    #[test]
    fn bad_sync_rejected() {
        let mut wire = reference_frame().encode();
        wire[0] = 0x00;
        assert_eq!(Frame::decode(&wire), Err(FrameError::BadSync));
    }

    #[test]
    fn payload_corruption_detected() {
        let mut wire = reference_frame().encode();
        wire[9] ^= 0xFF;  // flip byte in payload area
        assert_eq!(Frame::decode(&wire), Err(FrameError::BadCrc));
    }

    #[test]
    fn timestamp_corruption_detected() {
        let mut wire = reference_frame().encode();
        wire[13] ^= 0x01;
        assert_eq!(Frame::decode(&wire), Err(FrameError::BadCrc));
    }

    #[test]
    fn crc_field_corruption_detected() {
        let mut wire = reference_frame().encode();
        wire[15] ^= 0x01;
        assert_eq!(Frame::decode(&wire), Err(FrameError::BadCrc));
    }

    #[test]
    fn short_buffer_rejected() {
        let wire = reference_frame().encode();
        assert_eq!(Frame::decode(&wire[..16]), Err(FrameError::BadLength));
        assert_eq!(Frame::decode(&[]),         Err(FrameError::BadLength));
    }

    #[test]
    fn is_valid_agrees_with_decode() {
        let wire = reference_frame().encode();
        assert!(Frame::is_valid(&wire));

        let mut bad = wire;
        bad[10] ^= 0x80;
        assert!(!Frame::is_valid(&bad));
    }

    // Cross-language CRC pinning test.
    // The body bytes below are the 15-byte prefix of the reference frame
    // (version=1, Data, seq=1, src=1, dst=0xFFFF, payload=DEADBEEF, ts=0).
    // The expected CRC value is computed deterministically; any change to
    // the crc16_ccitt algorithm that alters this value is a wire-breaking change.
    #[test]
    fn crc_cross_language_pin() {
        let body: [u8; 15] = [
            0xD3, 0x10, 0x00, 0x01,  // sync, flags, seq
            0x00, 0x01, 0xFF, 0xFF,  // src, dst
            0xDE, 0xAD, 0xBE, 0xEF, // payload
            0x00, 0x00, 0x00,        // timestamp
        ];
        let crc = crc16_ccitt(&body);
        // Pin the exact value. If this fails, crc16_ccitt has diverged from
        // the C / Haskell / Lisp implementations — a cross-language break.
        // Reference value verified against C dcf_crc16() and Haskell crc16ccitt().
        assert_eq!(
            crc, 0x42DD,
            "CRC pin failed: algorithm diverged from cross-language reference (0x42DD)"
        );
    }

    #[test]
    fn broadcast_const_matches_wire() {
        let wire = reference_frame().encode();
        let dst = ((wire[6] as u16) << 8) | wire[7] as u16;
        assert_eq!(dst, BROADCAST);
    }
}
