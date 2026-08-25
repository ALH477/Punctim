// SPDX-License-Identifier: LGPL-3.0-only
//! Rust certification for the DCF-QKD key-ID beacon L2 framing — diffs the Rust
//! implementation against the cross-language golden vectors
//! (Documentation/qkd_vectors.json). Passing this == byte-agreement with the C and
//! Python references.
//!
//! No key material appears here or on the wire: the beacon carries only the key_ID,
//! a non-secret 128-bit identifier.

use dcf_wire_codec::qkd::{
    channel_id, packetize, KeyIdReassembler, LostBeacon, BROADCAST, FRAGS, KEY_ID_BYTES,
    MAX_EPOCH,
};
use serde::Deserialize;
use std::path::Path;

// ── JSON shapes (only the fields we assert on) ──────────────────────────────
#[derive(Deserialize)]
struct QkdVectors {
    framing: Vec<FramingCase>,
    reassembly: Vec<ReasmCase>,
}
#[derive(Deserialize)]
struct FramingCase {
    src: u16,
    dst: u16,
    epoch: u16,
    ts_us: u32,
    key_id_bytes: String,
    frames: Vec<String>,
}
#[derive(Deserialize)]
struct ReasmCase {
    name: String,
    accept_dst: Option<u16>,
    input_frames: Vec<String>,
    keys: Vec<KeyCase>,
    lost: Vec<LostCase>,
}
#[derive(Deserialize)]
struct KeyCase {
    epoch: u16,
    ts_us: u32,
    src: u16,
    dst: u16,
    key_id_bytes: String,
}
#[derive(Deserialize)]
struct LostCase {
    epoch: u16,
    src: u16,
    dst: u16,
}

fn load<T: for<'de> Deserialize<'de>>(name: &str) -> T {
    let dir = std::env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| ".".into());
    for p in [
        format!("{}/../Documentation/{}", dir, name),
        format!("{}/../python/MCP/{}", dir, name),
    ] {
        if Path::new(&p).exists() {
            let data = std::fs::read_to_string(&p).unwrap();
            return serde_json::from_str(&data).unwrap_or_else(|e| panic!("parse {}: {}", p, e));
        }
    }
    panic!("{} not found (run python3 python/MCP/gen_qkd_vectors.py)", name);
}

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn to_hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}
fn key16(s: &str) -> [u8; KEY_ID_BYTES] {
    hex(s).as_slice().try_into().expect("key_ID is 16 bytes")
}

#[test]
fn framing_matches_golden() {
    let v: QkdVectors = load("qkd_vectors.json");
    for (i, c) in v.framing.iter().enumerate() {
        let key_id = key16(&c.key_id_bytes);
        let frames = packetize(&key_id, c.epoch, c.ts_us, c.src, c.dst).unwrap();
        // the invariant that makes this adapter descriptor-free
        assert_eq!(frames.len(), FRAGS, "framing[{}] must be exactly 4 frames", i);
        assert_eq!(frames.len(), c.frames.len(), "framing[{}] frame count", i);
        for (f, exp) in frames.iter().zip(&c.frames) {
            assert_eq!(to_hex(f), exp.to_lowercase(), "framing[{}] frame bytes", i);
        }
    }
    println!("PASS  {} framing cases packetize byte-identically", v.framing.len());
}

#[test]
fn reassembly_matches_golden() {
    let v: QkdVectors = load("qkd_vectors.json");
    for c in &v.reassembly {
        let mut r = KeyIdReassembler::with_accept_dst(c.accept_dst);
        let mut got = Vec::new();
        for h in &c.input_frames {
            let bytes = hex(h);
            let arr: [u8; 17] = bytes.as_slice().try_into().unwrap();
            let out = r.push(&arr);
            assert!(out.lost.is_empty(), "{}: no eviction expected", c.name);
            if let Some(k) = out.key {
                got.push(k);
            }
        }
        assert_eq!(got.len(), c.keys.len(), "{}: key count", c.name);
        for (k, e) in got.iter().zip(&c.keys) {
            assert_eq!(k.epoch, e.epoch, "{}: epoch", c.name);
            assert_eq!(k.ts_us, e.ts_us, "{}: ts_us", c.name);
            assert_eq!(k.src, e.src, "{}: src", c.name);
            assert_eq!(k.dst, e.dst, "{}: dst", c.name);
            assert_eq!(
                to_hex(&k.key_id),
                e.key_id_bytes.to_lowercase(),
                "{}: key_ID bytes",
                c.name
            );
        }
        let want: Vec<LostBeacon> = c
            .lost
            .iter()
            .map(|l| LostBeacon {
                epoch: l.epoch,
                src: l.src,
                dst: l.dst,
            })
            .collect();
        assert_eq!(r.finalize(), want, "{}: lost set", c.name);
    }
    println!("PASS  {} reassembly cases", v.reassembly.len());
}

#[test]
fn key_id_arithmetic_and_bounds() {
    // The whole reason this adapter needs no descriptor.
    assert_eq!(KEY_ID_BYTES, FRAGS * 4);
    let key_id = [0u8; KEY_ID_BYTES];
    assert!(packetize(&key_id, MAX_EPOCH, 0, 1, 1).is_ok());
    assert!(packetize(&key_id, MAX_EPOCH + 1, 0, 1, 1).is_err());
    println!("PASS  16 B key_ID = exactly 4 fragments; epoch rail enforced");
}

#[test]
fn channel_rendezvous_anchor() {
    // crc16_ccitt("123456789") == 0x29B1 is the repo-wide CRC anchor.
    assert_eq!(channel_id("123456789"), 0x29B1);
    assert_eq!(channel_id(""), BROADCAST);
    println!("PASS  channel_id rendezvous anchor holds");
}
