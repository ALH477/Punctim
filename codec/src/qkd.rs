// SPDX-License-Identifier: LGPL-3.0-only
//! DCF-QKD: ETSI GS QKD 014 key-ID beacon over the DeModFrame wire.
//!
//! The key-ID beacon is an *adapter* over the 17-byte DeModFrame quantum (`frame.rs`),
//! not a new wire format — exactly like DCF-Audio (`audio.rs`) and DCF-Text (`text.rs`).
//! One ETSI 014 `key_ID` is fragmented into exactly four ordinary CTRL frames. This L2
//! framing is byte-deterministic across C/Rust/Python — it is pinned by
//! `Documentation/qkd_vectors.json`. See `Documentation/DCF_QKD_SPEC.md`.
//!
//! ("Wire quantum" is quantum as in *quanta* — an indivisible unit. Nothing in this
//! module is quantum: a `key_ID` is an opaque 128-bit identifier minted by external KME
//! hardware.)
//!
//! Why this exists: ETSI GS QKD 014 deliberately leaves the transport of `key_ID` from
//! master SAE to slave SAE **out of scope**, so every deployment invents a carrier. The
//! arithmetic makes the quantum an unusually good fit:
//!
//! ```text
//! key_ID = UUID = 128 bits = 16 bytes = exactly 4 x 4-byte DeModFrame payloads
//! ```
//!
//! Why no descriptor: every other fragmenting adapter burns `frag_idx 0` on a length
//! descriptor. A `key_ID` is *always* 16 bytes, so length and `frag_total` are known a
//! priori. All four fragments are data — a normative design property, not an omission.
//!
//! Layout (all frames version=1, type=CTRL(3), big-endian):
//!   seq = epoch[15:2] (14 bits, 0..16383) | frag_idx[1:0] (2 bits, 0..3)
//!   frag_idx 0..3 data : payload = key_id[idx*4 .. +4]   (no padding, ever)
//!
//! The 14:2 split is unique among the CTRL(3) adapters (audio 11:5, cue 9:7, snake 5:11).
//! As everywhere else there is no in-band adapter tag: run one reassembler per `dst`.
//!
//! **Export / security (normative):** the wire carries *only* the `key_ID`, a non-secret
//! identifier. Key material MUST NOT be placed in a DeModFrame payload. This module never
//! sees a key.

use crate::{crc16_ccitt, Frame, FrameType};
use std::collections::BTreeMap;

// ── L2 constants ────────────────────────────────────────────────────────────
pub const FRAG_BITS: u16 = 2;
pub const FRAG_MASK: u16 = 0x3;
pub const FRAGS: usize = 4;
pub const KEY_ID_BYTES: usize = 16;
pub const MAX_EPOCH: u16 = (1 << (16 - FRAG_BITS)) - 1; // 16383
pub const BROADCAST: u16 = 0xFFFF;

/// Incomplete beacons held before the oldest is evicted (bounded memory on a lossy link).
pub const DEFAULT_MAX_PENDING: usize = 64;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QkdError {
    /// epoch exceeds the 14-bit field.
    BadEpoch,
}

/// Map a human channel/passphrase to a 16-bit rendezvous `dst` (the same frequency-channel
/// hash the rest of the repo uses). Empty => broadcast.
pub fn channel_id(name: &str) -> u16 {
    if name.is_empty() {
        BROADCAST
    } else {
        crc16_ccitt(name.as_bytes())
    }
}

// ── L2: packetize ───────────────────────────────────────────────────────────
/// Serialise one `key_ID` into exactly four DeModFrame CTRL frames. There is no
/// descriptor and no padding: 16 bytes divide evenly into four 4-byte payloads.
pub fn packetize(
    key_id: &[u8; KEY_ID_BYTES],
    epoch: u16,
    ts_us: u32,
    src: u16,
    dst: u16,
) -> Result<Vec<[u8; 17]>, QkdError> {
    if epoch > MAX_EPOCH {
        return Err(QkdError::BadEpoch);
    }
    let mut frames = Vec::with_capacity(FRAGS);
    for idx in 0..FRAGS {
        let seq = (epoch << FRAG_BITS) | idx as u16;
        let mut chunk = [0u8; 4];
        chunk.copy_from_slice(&key_id[idx * 4..idx * 4 + 4]);
        frames.push(Frame::new(1, FrameType::Ctrl, seq, src, dst, chunk, ts_us).encode());
    }
    Ok(frames)
}

// ── L2: reassembler ─────────────────────────────────────────────────────────
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyIdBeacon {
    pub epoch: u16,
    pub ts_us: u32,
    pub src: u16,
    pub dst: u16,
    pub key_id: [u8; KEY_ID_BYTES],
}

/// A beacon that never completed: reported on eviction or by `finalize`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LostBeacon {
    pub epoch: u16,
    pub src: u16,
    pub dst: u16,
}

#[derive(Default)]
struct Slot {
    ts_us: u32,
    age: u64,
    frags: BTreeMap<u16, [u8; 4]>,
}

/// Stateful, order-independent reassembler for key-ID beacons.
///
/// Beacons are keyed by `(src, dst, epoch)`, so two SAEs may beacon the same epoch
/// concurrently without cross-contamination. `push` emits a completed `key_ID` as soon as
/// all four fragments arrive; duplicates are ignored. Incomplete beacons are bounded: once
/// more than `max_pending` are in flight the oldest is evicted and reported lost — a lossy
/// link would otherwise leak memory indefinitely.
pub struct KeyIdReassembler {
    slots: BTreeMap<(u16, u16, u16), Slot>, // (src, dst, epoch) => sorted like the refs
    accept_dst: Option<u16>,
    max_pending: usize,
    clock: u64,
}

impl Default for KeyIdReassembler {
    fn default() -> Self {
        Self {
            slots: BTreeMap::new(),
            accept_dst: None,
            max_pending: DEFAULT_MAX_PENDING,
            clock: 0,
        }
    }
}

/// What one `push` produced: at most one completed key, plus any evicted beacons.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PushOutcome {
    pub key: Option<KeyIdBeacon>,
    pub lost: Vec<LostBeacon>,
}

impl KeyIdReassembler {
    pub fn new() -> Self {
        Self::default()
    }

    /// Bind the reassembler to one rendezvous channel; other `dst`s (except broadcast)
    /// are ignored. `None` accepts every channel.
    pub fn with_accept_dst(accept_dst: Option<u16>) -> Self {
        Self {
            accept_dst,
            ..Self::default()
        }
    }

    pub fn with_max_pending(mut self, max_pending: usize) -> Self {
        self.max_pending = max_pending.max(1);
        self
    }

    /// Number of beacons with some but not all fragments received.
    pub fn pending(&self) -> usize {
        self.slots.len()
    }

    pub fn push(&mut self, frame: &[u8; 17]) -> PushOutcome {
        let mut out = PushOutcome::default();
        let d = match Frame::decode(&frame[..]) {
            Ok(d) => d,
            Err(_) => return out, // corrupt frame: dropped, never fatal
        };
        if d.frame_type != FrameType::Ctrl {
            return out;
        }
        if let Some(acc) = self.accept_dst {
            if d.dst_id != acc && d.dst_id != BROADCAST {
                return out;
            }
        }
        let epoch = d.seq >> FRAG_BITS;
        let frag_idx = d.seq & FRAG_MASK;
        let key = (d.src_id, d.dst_id, epoch);

        self.clock += 1;
        let clock = self.clock;
        let entry = self.slots.entry(key).or_insert_with(|| Slot {
            age: clock,
            ..Slot::default()
        });
        entry.ts_us = d.timestamp_us;
        entry.frags.entry(frag_idx).or_insert(d.payload);

        if entry.frags.len() == FRAGS {
            let slot = self.slots.remove(&key).expect("just checked");
            let mut key_id = [0u8; KEY_ID_BYTES];
            for (i, chunk) in (0..FRAGS as u16).filter_map(|i| slot.frags.get(&i)).enumerate() {
                key_id[i * 4..i * 4 + 4].copy_from_slice(chunk);
            }
            out.key = Some(KeyIdBeacon {
                epoch,
                ts_us: slot.ts_us,
                src: key.0,
                dst: key.1,
                key_id,
            });
        }

        // bound the in-flight set: evict oldest-first, reporting each as lost
        while self.slots.len() > self.max_pending {
            let oldest = self
                .slots
                .iter()
                .min_by_key(|(_, s)| s.age)
                .map(|(k, _)| *k)
                .expect("non-empty");
            self.slots.remove(&oldest);
            out.lost.push(LostBeacon {
                epoch: oldest.2,
                src: oldest.0,
                dst: oldest.1,
            });
        }
        out
    }

    /// Report every still-incomplete beacon as lost (ascending src, dst, epoch — the same
    /// order the C and Python references use), clearing state.
    pub fn finalize(&mut self) -> Vec<LostBeacon> {
        let lost: Vec<LostBeacon> = self
            .slots
            .keys()
            .map(|(src, dst, epoch)| LostBeacon {
                epoch: *epoch,
                src: *src,
                dst: *dst,
            })
            .collect(); // BTreeMap => sorted by (src, dst, epoch)
        self.slots.clear();
        lost
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KID_A: [u8; 16] = [
        0x57, 0x4b, 0xac, 0xe1, 0x4c, 0x27, 0x49, 0xa1, 0xba, 0xbd, 0x66, 0x3f, 0xdb, 0x62,
        0x4d, 0x00,
    ];
    const KID_B: [u8; 16] = [
        0xd5, 0x4b, 0x21, 0x4e, 0x60, 0xeb, 0x4f, 0x1c, 0xaf, 0xb0, 0x62, 0xa7, 0xb2, 0x3f,
        0xc2, 0x0e,
    ];

    #[test]
    fn a_key_id_is_always_exactly_four_frames() {
        let frames = packetize(&KID_A, 5, 0x010203, 0x00A1, 0xFFFF).unwrap();
        assert_eq!(frames.len(), FRAGS);
        assert_eq!(KEY_ID_BYTES, FRAGS * 4);
        // payloads concatenate to the key_ID with no padding
        let mut joined = Vec::new();
        for f in &frames {
            let d = Frame::decode(&f[..]).unwrap();
            assert_eq!(d.frame_type, FrameType::Ctrl);
            joined.extend_from_slice(&d.payload);
        }
        assert_eq!(joined, KID_A.to_vec());
    }

    #[test]
    fn reassembles_regardless_of_frame_order() {
        let frames = packetize(&KID_A, 5, 0x010203, 0x00A1, 0xFFFF).unwrap();
        let mut r = KeyIdReassembler::new();
        let mut got = None;
        for f in frames.iter().rev() {
            if let Some(k) = r.push(f).key {
                got = Some(k);
            }
        }
        let k = got.expect("beacon reassembles regardless of frame order");
        assert_eq!(k.epoch, 5);
        assert_eq!(k.src, 0x00A1);
        assert_eq!(k.key_id, KID_A);
        assert_eq!(r.pending(), 0);
    }

    #[test]
    fn same_epoch_from_two_srcs_does_not_cross_contaminate() {
        let a = packetize(&KID_A, 7, 5, 0x0001, 0x0002).unwrap();
        let b = packetize(&KID_B, 7, 5, 0x0009, 0x0002).unwrap();
        let mut r = KeyIdReassembler::new();
        let mut got = Vec::new();
        for (fa, fb) in a.iter().zip(b.iter()) {
            if let Some(k) = r.push(fa).key {
                got.push(k.key_id);
            }
            if let Some(k) = r.push(fb).key {
                got.push(k.key_id);
            }
        }
        got.sort();
        let mut want = vec![KID_A, KID_B];
        want.sort();
        assert_eq!(got, want);
    }

    #[test]
    fn dropped_fragment_is_reported_lost() {
        let frames = packetize(&KID_A, 11, 1, 0x0001, 0x0002).unwrap();
        let mut r = KeyIdReassembler::new();
        for (j, f) in frames.iter().enumerate() {
            if j == 2 {
                continue;
            }
            assert!(r.push(f).key.is_none());
        }
        assert_eq!(
            r.finalize(),
            vec![LostBeacon {
                epoch: 11,
                src: 0x0001,
                dst: 0x0002
            }]
        );
    }

    #[test]
    fn foreign_dst_is_ignored_and_broadcast_accepted() {
        let mut r = KeyIdReassembler::with_accept_dst(Some(0x9999));
        for f in &packetize(&KID_A, 12, 1, 0x0001, 0x1234).unwrap() {
            assert!(r.push(f).key.is_none());
        }
        assert_eq!(r.pending(), 0);
        let mut got = None;
        for f in &packetize(&KID_A, 13, 1, 0x0001, BROADCAST).unwrap() {
            if let Some(k) = r.push(f).key {
                got = Some(k);
            }
        }
        assert_eq!(got.unwrap().key_id, KID_A);
    }

    #[test]
    fn in_flight_set_is_bounded_and_evicts_oldest() {
        let mut r = KeyIdReassembler::new().with_max_pending(2);
        for epoch in 0..2u16 {
            let f = packetize(&KID_A, epoch, 1, 0x0001, 0x0002).unwrap();
            assert!(r.push(&f[0]).lost.is_empty());
        }
        assert_eq!(r.pending(), 2);
        let f = packetize(&KID_A, 2, 1, 0x0001, 0x0002).unwrap();
        assert_eq!(
            r.push(&f[0]).lost,
            vec![LostBeacon {
                epoch: 0,
                src: 0x0001,
                dst: 0x0002
            }]
        );
        assert_eq!(r.pending(), 2);
    }

    #[test]
    fn epoch_above_the_rail_is_rejected() {
        assert_eq!(
            packetize(&KID_A, MAX_EPOCH + 1, 0, 1, 1),
            Err(QkdError::BadEpoch)
        );
    }

    #[test]
    fn channel_rendezvous_anchor() {
        assert_eq!(channel_id("123456789"), 0x29B1);
        assert_eq!(channel_id(""), BROADCAST);
    }
}
