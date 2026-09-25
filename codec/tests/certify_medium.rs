// SPDX-License-Identifier: LGPL-3.0-only
//! Rust certification for DCF-Medium — diffs the Rust medium codecs against the
//! cross-language golden vectors (Documentation/medium_vectors.json). Passing this ==
//! byte-agreement with the Python reference (python/MCP/mediumlab_core.py) and every
//! other certified language, for every family: stream, hex, udp_proto, udp_bare, l2eth,
//! hydra_symbols (to the symbol stream) and afsk_bits (to the bit stream).

use dcf_wire_codec::crc16_ccitt;
use dcf_wire_codec::medium::{
    afsk_bits_decode, afsk_bits_encode, afsk_profile, bare_decode, bare_encode, crc8_afsk, gate,
    hex_decode, hex_encode, hydra_symbols_decode, hydra_symbols_encode, l2_batch, l2_unbatch,
    proto_decode, proto_encode, proto_frame_decode, proto_frame_encode, stream_decode,
    stream_encode, to_hex, Frame17, HydraFec, HydraProfile, StreamScanner, AFSK_SYNC,
    HYDRA_SYNC_WORD, L2_FILLER, L2_HDR, MSG_FRAME, PROTO_HEADER_LEN,
};
use dcf_wire_codec::superpack::SUPER_LEN;
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Deserialize)]
struct Vectors {
    anchors: Anchors,
    basis: Vec<Basis>,
    families: Families,
}
#[derive(Deserialize)]
struct Anchors {
    crc_123456789: u16,
    crc_zero15: u16,
    frame_len: usize,
    proto_header_len: usize,
    msg_frame: u8,
    super_len: usize,
    l2_hdr: usize,
    l2_filler: String,
    hydra_sync_word: u16,
    afsk_sync: u8,
    afsk_crc8_123456789: u8,
}
#[derive(Deserialize)]
struct Basis {
    #[serde(rename = "type")]
    ftype: u8,
    seq: u16,
    src: u16,
    dst: u16,
    payload: String,
    ts: u32,
    hex: String,
}
#[derive(Deserialize)]
struct Families {
    stream: Fam<StreamCase>,
    hex: Fam<HexCase>,
    udp_proto: UdpProto,
    udp_bare: Fam<BareCase>,
    l2eth: L2Eth,
    hydra_symbols: Hydra,
    afsk_bits: Afsk,
}
#[derive(Deserialize)]
struct Fam<T> {
    cases: Vec<T>,
}
#[derive(Deserialize)]
struct StreamCase {
    name: String,
    input: String,
    frames: Vec<String>,
    skipped_bytes: usize,
    tail_bytes: usize,
}
#[derive(Deserialize)]
struct HexCase {
    name: String,
    frames: Vec<String>,
    text: String,
    decode_input: String,
    decoded: Vec<String>,
    bad_lines: usize,
}
#[derive(Deserialize)]
struct UdpProto {
    header_len: usize,
    msg_frame: u8,
    types: BTreeMap<String, u8>,
    cases: Vec<ProtoCase>,
}
#[derive(Deserialize)]
struct ProtoCase {
    name: String,
    #[serde(rename = "type")]
    mtype: u8,
    seq: u32,
    ts: u64,
    ts_hex: String,
    payload: String,
    datagram: String,
    accept_as_frame: bool,
}
#[derive(Deserialize)]
struct BareCase {
    name: String,
    frames: Vec<String>,
    datagrams: Vec<String>,
}
#[derive(Deserialize)]
struct L2Eth {
    hdr: usize,
    filler: String,
    cases: Vec<L2Case>,
}
#[derive(Deserialize)]
struct L2Case {
    name: String,
    frames: Vec<String>,
    payload: String,
}
#[derive(Deserialize)]
struct Hydra {
    profiles: BTreeMap<String, HydraUser>,
    cases: Vec<HydraCase>,
}
#[derive(Deserialize)]
struct HydraUser {
    sample_rate: f64,
    baud: f64,
    n_tones: u32,
    base_freq: f64,
    tone_spacing: f64,
    preamble_syms: u32,
    sync_word: u16,
    fec_mode: u8,
    interleave: u8,
    tx_gain: f64,
}
#[derive(Deserialize)]
struct HydraCase {
    name: String,
    profile: String,
    fec: String,
    interleave: u8,
    n_tones: u32,
    frame: String,
    coded_bits: usize,
    interleave_stride: usize,
    total_syms: usize,
    symbols: String,
}
#[derive(Deserialize)]
struct Afsk {
    profiles: BTreeMap<String, AfskUser>,
    cases: Vec<AfskCase>,
}
#[derive(Deserialize)]
struct AfskUser {
    mark: f64,
    space: f64,
    baud: u32,
    preamble_bits: usize,
}
#[derive(Deserialize)]
struct AfskCase {
    name: String,
    profile: String,
    fec: bool,
    frame: String,
    n_bits: usize,
    bits: String,
}

fn load() -> Vectors {
    let dir = std::env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| ".".into());
    for p in [
        format!("{}/../Documentation/medium_vectors.json", dir),
        format!("{}/../python/MCP/medium_vectors.json", dir),
    ] {
        if Path::new(&p).exists() {
            let data = std::fs::read_to_string(&p).unwrap();
            return serde_json::from_str(&data).unwrap_or_else(|e| panic!("parse {}: {}", p, e));
        }
    }
    panic!("medium_vectors.json not found (run python3 python/MCP/gen_medium_vectors.py)");
}

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn frame(s: &str) -> Frame17 {
    hex(s).try_into().expect("17-byte frame")
}
fn frames(v: &[String]) -> Vec<Frame17> {
    v.iter().map(|s| frame(s)).collect()
}
fn hexes(v: &[Frame17]) -> Vec<String> {
    v.iter().map(|f| to_hex(f)).collect()
}

#[test]
fn anchors_and_basis() {
    let v = load();
    let a = &v.anchors;
    assert_eq!(crc16_ccitt(b"123456789"), a.crc_123456789);
    assert_eq!(a.crc_123456789, 0x29B1);
    assert_eq!(crc16_ccitt(&[0u8; 15]), a.crc_zero15);
    assert_eq!(a.crc_zero15, 0x4EC3);
    assert_eq!(a.frame_len, 17);
    assert_eq!(a.proto_header_len, PROTO_HEADER_LEN);
    assert_eq!(a.msg_frame, MSG_FRAME);
    assert_eq!(a.super_len, SUPER_LEN);
    assert_eq!(a.l2_hdr, L2_HDR);
    assert_eq!(a.l2_filler, to_hex(&L2_FILLER));
    assert_eq!(a.hydra_sync_word, HYDRA_SYNC_WORD);
    assert_eq!(a.afsk_sync, AFSK_SYNC);
    assert_eq!(crc8_afsk(b"123456789"), a.afsk_crc8_123456789);
    for (i, b) in v.basis.iter().enumerate() {
        let f = dcf_wire_codec::Frame {
            version: 1,
            frame_type: match b.ftype {
                0 => dcf_wire_codec::FrameType::Data,
                1 => dcf_wire_codec::FrameType::Ack,
                2 => dcf_wire_codec::FrameType::Beacon,
                _ => dcf_wire_codec::FrameType::Ctrl,
            },
            seq: b.seq,
            src_id: b.src,
            dst_id: b.dst,
            payload: hex(&b.payload).try_into().unwrap(),
            timestamp_us: b.ts,
        }
        .encode();
        assert_eq!(to_hex(&f), b.hex, "basis[{}]", i);
        assert!(gate(&f), "basis[{}] gate", i);
    }
    println!("PASS  anchors + {} basis frames", v.basis.len());
}

#[test]
fn stream_family() {
    let v = load();
    for c in &v.families.stream.cases {
        let input = hex(&c.input);
        let want = frames(&c.frames);
        let (got, skipped, tail) = stream_decode(&input);
        assert_eq!(hexes(&got), c.frames, "{}: frames", c.name);
        assert_eq!(skipped, c.skipped_bytes, "{}: skipped_bytes", c.name);
        assert_eq!(tail, c.tail_bytes, "{}: tail_bytes", c.name);
        // encode is lossless: stream_encode(frames) decodes back cleanly
        assert_eq!(
            stream_decode(&stream_encode(&want)),
            (want.clone(), 0, 0),
            "{}: lossless",
            c.name
        );
        // incremental scanner: any chunking yields the same frames + diagnostics
        for step in [1usize, 2, 3, 5, 16, 17, 18, 64] {
            let mut sc = StreamScanner::new();
            let mut acc = Vec::new();
            for ch in input.chunks(step) {
                acc.extend(sc.feed(ch));
            }
            assert_eq!(acc, want, "{}: scanner step {}", c.name, step);
            assert_eq!(
                sc.skipped_bytes(),
                c.skipped_bytes,
                "{}: scanner skipped",
                c.name
            );
            assert_eq!(sc.flush().len(), c.tail_bytes, "{}: scanner tail", c.name);
        }
    }
    println!(
        "PASS  stream: {} cases (byte-wise resync, chunk-invariant)",
        v.families.stream.cases.len()
    );
}

#[test]
fn hex_family() {
    let v = load();
    for c in &v.families.hex.cases {
        assert_eq!(hex_encode(&frames(&c.frames)), c.text, "{}: encode", c.name);
        let (got, bad) = hex_decode(&c.decode_input);
        assert_eq!(hexes(&got), c.decoded, "{}: decoded", c.name);
        assert_eq!(bad, c.bad_lines, "{}: bad_lines", c.name);
    }
    println!("PASS  hex: {} cases", v.families.hex.cases.len());
}

#[test]
fn udp_proto_family() {
    let v = load();
    let fam = &v.families.udp_proto;
    assert_eq!(fam.header_len, PROTO_HEADER_LEN);
    assert_eq!(fam.msg_frame, MSG_FRAME);
    assert_eq!(fam.types.get("FRAME"), Some(&12));
    for (name, id) in dcf_wire_codec::medium::MSG_TYPES {
        assert_eq!(fam.types.get(name), Some(&id), "msg type {}", name);
    }
    for c in &fam.cases {
        assert_eq!(format!("{:016x}", c.ts), c.ts_hex, "{}: ts_hex", c.name);
        let payload = hex(&c.payload);
        let dg = proto_encode(c.mtype, c.seq, c.ts, &payload);
        assert_eq!(to_hex(&dg), c.datagram, "{}: encode", c.name);
        let (t, s, ts, p) = proto_decode(&dg).expect("decode");
        assert_eq!(
            (t, s, ts, p),
            (c.mtype, c.seq, c.ts, &payload[..]),
            "{}: decode",
            c.name
        );
        let fr = proto_frame_decode(&dg);
        assert_eq!(
            fr.is_some(),
            c.accept_as_frame,
            "{}: accept_as_frame",
            c.name
        );
        if let Some(f) = fr {
            assert_eq!(to_hex(&f), c.payload, "{}: carried frame", c.name);
            assert_eq!(
                to_hex(&proto_frame_encode(&f, c.seq, c.ts)),
                c.datagram,
                "{}: frame_encode",
                c.name
            );
        }
        // guards: a short header and an overrunning payload_len are rejected
        assert!(
            proto_decode(&dg[..PROTO_HEADER_LEN - 1]).is_err(),
            "{}: short guard",
            c.name
        );
        if !payload.is_empty() {
            assert!(
                proto_decode(&dg[..dg.len() - 1]).is_err(),
                "{}: overrun guard",
                c.name
            );
        }
    }
    println!(
        "PASS  udp_proto: {} cases (MSG_FRAME=12, header guards)",
        fam.cases.len()
    );
}

#[test]
fn udp_bare_family() {
    let v = load();
    for c in &v.families.udp_bare.cases {
        let fs = frames(&c.frames);
        let dg: Vec<String> = bare_encode(&fs, true).iter().map(|d| to_hex(d)).collect();
        assert_eq!(dg, c.datagrams, "{}: encode", c.name);
        let back: Vec<Frame17> = c
            .datagrams
            .iter()
            .flat_map(|d| bare_decode(&hex(d)))
            .collect();
        assert_eq!(back, fs, "{}: decode", c.name);
        let raw = bare_encode(&fs, false);
        assert!(
            raw.iter().all(|d| d.len() == 17) && raw.len() == fs.len(),
            "{}: pair=0",
            c.name
        );
    }
    assert!(bare_decode(&[0u8; 20]).is_empty());
    println!("PASS  udp_bare: {} cases", v.families.udp_bare.cases.len());
}

#[test]
fn l2eth_family() {
    let v = load();
    let fam = &v.families.l2eth;
    assert_eq!(fam.hdr, L2_HDR);
    assert_eq!(fam.filler, to_hex(&L2_FILLER));
    for c in &fam.cases {
        let fs = frames(&c.frames);
        assert_eq!(
            to_hex(&l2_batch(&fs).unwrap()),
            c.payload,
            "{}: batch",
            c.name
        );
        let pl = hex(&c.payload);
        assert_eq!(l2_unbatch(&pl).unwrap(), fs, "{}: unbatch", c.name);
        assert!(
            l2_unbatch(&pl[..pl.len() - 1]).is_err(),
            "{}: truncation rejected",
            c.name
        );
    }
    println!("PASS  l2eth: {} cases", fam.cases.len());
}

#[test]
fn hydra_symbols_family() {
    let v = load();
    let fam = &v.families.hydra_symbols;
    for (name, u) in &fam.profiles {
        let p = HydraProfile::named(name).unwrap_or_else(|| panic!("profile {}", name));
        assert_eq!(p.sample_rate, u.sample_rate, "{}", name);
        assert_eq!(p.baud, u.baud, "{}", name);
        assert_eq!(p.n_tones, u.n_tones, "{}", name);
        assert_eq!(p.base_freq, u.base_freq, "{}", name);
        assert_eq!(p.tone_spacing, u.tone_spacing, "{}", name);
        assert_eq!(p.preamble_syms, u.preamble_syms, "{}", name);
        assert_eq!(p.sync_word, u.sync_word, "{}", name);
        assert_eq!(p.fec as u8, u.fec_mode, "{}", name);
        assert_eq!(u8::from(p.interleave), u.interleave, "{}", name);
        assert_eq!(p.tx_gain, u.tx_gain, "{}", name);
    }
    for c in &fam.cases {
        let mut p = HydraProfile::named(&c.profile).expect("profile");
        p.fec = HydraFec::from_name(&c.fec).expect("fec");
        p.interleave = c.interleave != 0;
        p.n_tones = c.n_tones;
        p.init().expect("init");
        assert_eq!(p.coded_bits, c.coded_bits, "{}: coded_bits", c.name);
        assert_eq!(
            p.interleave_stride, c.interleave_stride,
            "{}: stride",
            c.name
        );
        assert_eq!(p.total_syms, c.total_syms, "{}: total_syms", c.name);
        let f = frame(&c.frame);
        let s = hydra_symbols_encode(&p, &f).expect("encode");
        assert_eq!(s, c.symbols, "{}: symbols", c.name);
        assert_eq!(
            hydra_symbols_decode(&p, &c.symbols),
            Some(f),
            "{}: decode",
            c.name
        );
    }
    println!(
        "PASS  hydra_symbols: {} cases (encode == vector, decode(encode) == frame)",
        fam.cases.len()
    );
}

#[test]
fn afsk_bits_family() {
    let v = load();
    let fam = &v.families.afsk_bits;
    for (name, u) in &fam.profiles {
        let p = afsk_profile(name).unwrap_or_else(|| panic!("afsk profile {}", name));
        assert_eq!(
            (p.mark, p.space, p.baud, p.preamble_bits),
            (u.mark, u.space, u.baud, u.preamble_bits),
            "{}",
            name
        );
    }
    for c in &fam.cases {
        let f = frame(&c.frame);
        let b = afsk_bits_encode(&f, &c.profile, c.fec).expect("encode");
        assert_eq!(b.len(), c.n_bits, "{}: n_bits", c.name);
        assert_eq!(b, c.bits, "{}: bits", c.name);
        assert_eq!(
            afsk_bits_decode(&c.bits, &c.profile, c.fec),
            Some(f),
            "{}: decode",
            c.name
        );
    }
    println!(
        "PASS  afsk_bits: {} cases (encode == vector, decode(encode) == frame)",
        fam.cases.len()
    );
}
