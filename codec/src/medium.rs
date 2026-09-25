// SPDX-License-Identifier: LGPL-3.0-only
//! DCF-Medium — the medium codecs that carry the 17-byte `DeModFrame` quantum over
//! every medium, byte-certified against `Documentation/medium_vectors.json`.
//!
//! A *medium codec* is a deterministic pair
//! `encode: [frame] -> representation` / `decode: representation -> ([frame], diagnostics)`
//! that transports the quantum WITHOUT parsing it beyond the frame gate (sync `0xD3` +
//! version nibble 1 + CRC-16/CCITT-FALSE). Media sit *beneath* the quantum, so the
//! 246-vector wire certificate is untouched. Canonical reference:
//! `python/MCP/mediumlab_core.py`; normative spec: `Documentation/DCF_MEDIUM_SPEC.md`.
//!
//! Families (all byte-certified; the analog ones to their symbol / bit stream):
//!
//! | family          | representation                                                      |
//! |-----------------|---------------------------------------------------------------------|
//! | `stream`        | `.dcf` file / stdio: concatenated frames, byte-wise resync on decode |
//! | `hex`           | 34 lowercase hex chars + `\n` per frame                              |
//! | `udp_proto`     | ProtoMessage envelope `>BIQI`, msg_type `FRAME = 12`                 |
//! | `udp_bare`      | a bare 17-B frame or a 32-B SuperPack pair per datagram              |
//! | `l2eth`         | `[n u16 BE][SuperPack * ceil(n/2)]` Ethernet payload                 |
//! | `hydra_symbols` | HydraModem M-FSK tone-index stream (port of `hydramodem/src/hydra_*.c`) |
//! | `afsk_bits`     | `python/modem` AFSK on-air bit stream (`acoustic_frame.encode_bits`) |
//!
//! `hydra` and `afsk` are two DIFFERENT acoustic media (different tones, sync word and
//! FEC); they do not interoperate with each other.

use crate::superpack::{self, SuperPackError, SUPER_LEN};
use crate::{crc16_ccitt, fec, FRAME_SIZE, SYNC_BYTE};

/// One DeModFrame, the unit every medium carries.
pub type Frame17 = [u8; FRAME_SIZE];

/// Frame length (17).
pub const FRAME_LEN: usize = FRAME_SIZE;
const VERSION: u8 = 1;

/// Errors raised by the medium codecs.
#[derive(Debug, PartialEq, Eq)]
pub enum MediumError {
    /// A buffer that must be exactly one 17-byte frame was not.
    BadLength,
    /// ProtoMessage shorter than its 17-byte header.
    ShortHeader,
    /// ProtoMessage `payload_len` overruns the datagram.
    Overrun,
    /// l2eth payload shorter than the 2-byte count.
    ShortBatch,
    /// l2eth payload shorter than `2 + 32*ceil(n/2)`.
    TruncatedBatch,
    /// More than 65535 frames in one l2eth batch.
    TooManyFrames,
    /// A SuperPack could not be packed or unpacked.
    SuperPack(SuperPackError),
    /// A HydraModem profile that `hydra_profile_init` rejects (or unknown name).
    BadProfile(&'static str),
    /// Unknown AFSK profile name.
    UnknownAfskProfile,
}

impl From<SuperPackError> for MediumError {
    fn from(e: SuperPackError) -> Self {
        MediumError::SuperPack(e)
    }
}

impl std::fmt::Display for MediumError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MediumError::BadLength => write!(f, "need a 17-byte frame"),
            MediumError::ShortHeader => write!(f, "message shorter than 17-byte header"),
            MediumError::Overrun => write!(f, "payload length exceeds message size"),
            MediumError::ShortBatch => write!(f, "short batch"),
            MediumError::TruncatedBatch => write!(f, "truncated batch"),
            MediumError::TooManyFrames => write!(f, "too many frames for one batch"),
            MediumError::SuperPack(e) => write!(f, "superpack: {:?}", e),
            MediumError::BadProfile(why) => write!(f, "bad hydra profile: {}", why),
            MediumError::UnknownAfskProfile => write!(f, "unknown afsk profile"),
        }
    }
}

impl std::error::Error for MediumError {}

// ── the frame gate (the existing validity rule; media never parse further) ──────

/// True iff `frame` is a valid DeModFrame: 17 bytes, sync 0xD3, version nibble 1,
/// CRC-16/CCITT-FALSE over bytes `[0..15)` equal to bytes `[15..17)` (big-endian).
/// Unlike `Frame::decode`, the type nibble is NOT checked (media never parse further).
pub fn gate(frame: &[u8]) -> bool {
    frame.len() == FRAME_LEN
        && frame[0] == SYNC_BYTE
        && frame[1] >> 4 == VERSION
        && crc16_ccitt(&frame[..15]) == (u16::from(frame[15]) << 8 | u16::from(frame[16]))
}

fn to_frame(b: &[u8]) -> Frame17 {
    let mut f = [0u8; FRAME_LEN];
    f.copy_from_slice(&b[..FRAME_LEN]);
    f
}

// ══ stream (.dcf file, stdio) ═══════════════════════════════════════════════════

/// Concatenate 17-byte frames (the `.dcf` / stdio representation).
pub fn stream_encode(frames: &[Frame17]) -> Vec<u8> {
    let mut out = Vec::with_capacity(frames.len() * FRAME_LEN);
    for f in frames {
        out.extend_from_slice(f);
    }
    out
}

/// Byte-wise resync scan of `buf` from offset `i`. Pushes gated frames; returns
/// `(i, skipped)` with `buf.len() - i < 17` on return. At offset `i`: if the 17-byte
/// window passes the gate, emit it and `i += 17`; else `i += 1`, `skipped += 1`.
fn scan(buf: &[u8], mut i: usize, mut skipped: usize, frames: &mut Vec<Frame17>) -> (usize, usize) {
    if buf.len() < FRAME_LEN {
        return (i, skipped);
    }
    let last = buf.len() - FRAME_LEN; // highest offset with a full window
    while i <= last {
        if buf[i] != SYNC_BYTE {
            // fast-forward to the next 0xD3 that still has a full window (pure speed-up)
            match buf[i + 1..=last].iter().position(|&b| b == SYNC_BYTE) {
                Some(p) => {
                    skipped += p + 1;
                    i += p + 1;
                    continue;
                }
                None => {
                    skipped += last + 1 - i;
                    i = last + 1;
                    break;
                }
            }
        }
        let w = &buf[i..i + FRAME_LEN];
        if gate(w) {
            frames.push(to_frame(w));
            i += FRAME_LEN;
        } else {
            i += 1;
            skipped += 1;
        }
    }
    (i, skipped)
}

/// Byte-wise resync decode of a stream -> `(frames, skipped_bytes, tail_bytes)`.
/// `skipped_bytes` counts offsets rejected by the gate; the trailing < 17 bytes that can
/// never hold a frame are `tail_bytes` (not counted as skipped).
pub fn stream_decode(buf: &[u8]) -> (Vec<Frame17>, usize, usize) {
    let mut frames = Vec::new();
    let (i, skipped) = scan(buf, 0, 0, &mut frames);
    (frames, skipped, buf.len() - i)
}

/// Incremental stream decoder. `feed(chunk)` returns the frames completed so far and
/// keeps a carry of at most 16 bytes, so any chunking yields exactly
/// [`stream_decode`]'s frames and `skipped_bytes`; [`StreamScanner::flush`] returns
/// (and clears) the final tail bytes.
#[derive(Debug, Default, Clone)]
pub struct StreamScanner {
    carry: Vec<u8>,
    skipped: usize,
    frames_out: usize,
}

impl StreamScanner {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed the next chunk; returns the frames it completed.
    pub fn feed(&mut self, chunk: &[u8]) -> Vec<Frame17> {
        let mut frames = Vec::new();
        let mut buf = std::mem::take(&mut self.carry);
        buf.extend_from_slice(chunk);
        let (i, skipped) = scan(&buf, 0, self.skipped, &mut frames);
        buf.drain(..i);
        self.carry = buf;
        self.skipped = skipped;
        self.frames_out += frames.len();
        frames
    }

    /// Offsets rejected by the gate so far.
    pub fn skipped_bytes(&self) -> usize {
        self.skipped
    }

    /// Frames emitted so far.
    pub fn frames_out(&self) -> usize {
        self.frames_out
    }

    /// Bytes currently carried (a possible frame prefix), <= 16.
    pub fn pending(&self) -> usize {
        self.carry.len()
    }

    /// Return (and clear) the carried tail bytes.
    pub fn flush(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.carry)
    }
}

// ══ hex (text lines) ════════════════════════════════════════════════════════════

/// One frame per line: 34 lowercase hex characters + `"\n"`.
pub fn hex_encode(frames: &[Frame17]) -> String {
    let mut out = String::with_capacity(frames.len() * (2 * FRAME_LEN + 1));
    for f in frames {
        out.push_str(&to_hex(f));
        out.push('\n');
    }
    out
}

/// Lowercase hex of arbitrary bytes.
pub fn to_hex(b: &[u8]) -> String {
    const D: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(2 * b.len());
    for &x in b {
        s.push(D[(x >> 4) as usize] as char);
        s.push(D[(x & 15) as usize] as char);
    }
    s
}

fn hexval(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

/// Parse an even-length hex string (either case) into bytes; `None` on any non-hex char.
pub fn from_hex(s: &str) -> Option<Vec<u8>> {
    let b = s.as_bytes();
    if b.len() % 2 != 0 {
        return None;
    }
    b.chunks(2)
        .map(|p| Some(hexval(p[0])? << 4 | hexval(p[1])?))
        .collect()
}

fn is_ws(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\r' | 0x0B | 0x0C)
}

/// Decode one hex line: `Ok(None)` for a blank/comment line, `Ok(Some(frame))` for a
/// good line, `Err(())` for a bad line.
fn hex_line(line: &[u8]) -> Result<Option<Frame17>, ()> {
    let mut a = 0;
    let mut z = line.len();
    while a < z && is_ws(line[a]) {
        a += 1;
    }
    while z > a && is_ws(line[z - 1]) {
        z -= 1;
    }
    let s = &line[a..z];
    if s.is_empty() || s[0] == b'#' {
        return Ok(None);
    }
    if s.len() != 2 * FRAME_LEN {
        return Err(());
    }
    let mut f = [0u8; FRAME_LEN];
    for (k, p) in s.chunks(2).enumerate() {
        f[k] = hexval(p[0]).ok_or(())? << 4 | hexval(p[1]).ok_or(())?;
    }
    Ok(Some(f))
}

/// Parse hex lines (bytes; non-ASCII lines are bad lines) -> `(frames, bad_lines)`.
/// See [`hex_decode`].
pub fn hex_decode_bytes(text: &[u8]) -> (Vec<Frame17>, usize) {
    let mut frames = Vec::new();
    let mut bad = 0;
    for line in text.split(|&c| c == b'\n') {
        match hex_line(line) {
            Ok(Some(f)) => frames.push(f),
            Ok(None) => {}
            Err(()) => bad += 1,
        }
    }
    (frames, bad)
}

/// Parse hex lines -> `(frames, bad_lines)`. Lines are split on `"\n"`; leading/trailing
/// ASCII whitespace (space, tab, CR, VT, FF) is stripped; blank lines and lines starting
/// with `#` are skipped; uppercase is accepted; any other line that is not exactly 34 hex
/// digits counts as a bad line. Frames are returned raw (NOT gated — the caller gates).
pub fn hex_decode(text: &str) -> (Vec<Frame17>, usize) {
    hex_decode_bytes(text.as_bytes())
}

// ══ udp dialect "proto" (ProtoMessage envelope) ═════════════════════════════════
// Byte-identical to python/dcf/proto.py, go/node/proto.go, rust/src/lib.rs, C dcf_proto.h:
//   [0] msg_type u8 | [1:5] sequence u32 | [5:13] timestamp u64 | [13:17] payload_len u32

/// ProtoMessage header length (17).
pub const PROTO_HEADER_LEN: usize = 17;
/// msg_type of a bare DeModFrame carried as the multi-transport unit.
pub const MSG_FRAME: u8 = 12;
/// The ProtoMessage msg_type registry (name, id).
pub const MSG_TYPES: [(&str, u8); 12] = [
    ("POSITION", 1),
    ("AUDIO", 2),
    ("GAME_EVENT", 3),
    ("STATE_SYNC", 4),
    ("RELIABLE", 5),
    ("ACK", 6),
    ("PING", 7),
    ("PONG", 8),
    ("GAME_DCF", 9),
    ("TEXT_DCF", 10),
    ("MESH", 11),
    ("FRAME", 12),
];

/// Serialize one ProtoMessage (17-byte big-endian header + payload).
pub fn proto_encode(msg_type: u8, sequence: u32, timestamp: u64, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(PROTO_HEADER_LEN + payload.len());
    out.push(msg_type);
    out.extend_from_slice(&sequence.to_be_bytes());
    out.extend_from_slice(&timestamp.to_be_bytes());
    out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    out.extend_from_slice(payload);
    out
}

/// Parse one ProtoMessage -> `(msg_type, sequence, timestamp, payload)`. Errors on a
/// short header or a `payload_len` that overruns the datagram (the Go/Rust/C guards).
/// Bytes after `payload_len` are ignored.
pub fn proto_decode(datagram: &[u8]) -> Result<(u8, u32, u64, &[u8]), MediumError> {
    if datagram.len() < PROTO_HEADER_LEN {
        return Err(MediumError::ShortHeader);
    }
    let t = datagram[0];
    let seq = u32::from_be_bytes([datagram[1], datagram[2], datagram[3], datagram[4]]);
    let mut ts = [0u8; 8];
    ts.copy_from_slice(&datagram[5..13]);
    let plen =
        u32::from_be_bytes([datagram[13], datagram[14], datagram[15], datagram[16]]) as usize;
    if datagram.len() - PROTO_HEADER_LEN < plen {
        return Err(MediumError::Overrun);
    }
    Ok((
        t,
        seq,
        u64::from_be_bytes(ts),
        &datagram[PROTO_HEADER_LEN..PROTO_HEADER_LEN + plen],
    ))
}

/// One frame as a 34-byte `ProtoMessage(MSG_FRAME, seq, ts, len 17, frame)`.
pub fn proto_frame_encode(
    frame: &Frame17,
    seq: u32,
    ts: u64,
) -> [u8; PROTO_HEADER_LEN + FRAME_LEN] {
    let mut out = [0u8; PROTO_HEADER_LEN + FRAME_LEN];
    out[0] = MSG_FRAME;
    out[1..5].copy_from_slice(&seq.to_be_bytes());
    out[5..13].copy_from_slice(&ts.to_be_bytes());
    out[13..17].copy_from_slice(&(FRAME_LEN as u32).to_be_bytes());
    out[17..].copy_from_slice(frame);
    out
}

/// The carried frame, or `None` unless msg_type == 12 and payload_len == 17 (types
/// 1..11 are adapter envelopes, not frames on this medium). Not gated.
pub fn proto_frame_decode(datagram: &[u8]) -> Option<Frame17> {
    match proto_decode(datagram) {
        Ok((MSG_FRAME, _, _, p)) if p.len() == FRAME_LEN => Some(to_frame(p)),
        _ => None,
    }
}

// ── SuperPack unpack with the medium gate ────────────────────────────────────────

/// Split a 32-byte SuperPack into its two frames — [`superpack::unpack`], which checks
/// exactly what the Python/C references check (`python/MCP/superpack.py:unpack`): length,
/// sync, version nibble, SUPER type, joint CRC, then each rebuilt inner frame against the
/// frame [`gate`] (sync + version nibble 1 + CRC). Every gated frame is carried, whatever
/// its type nibble (0..15) — media never parse the frame beyond the gate.
pub fn superpack_unpack(buf: &[u8]) -> Result<(Frame17, Frame17), SuperPackError> {
    superpack::unpack(buf)
}

// ══ udp dialect "bare" (17-B frame / 32-B SuperPack per datagram) ═══════════════

/// Streaming pairer for the bare dialect: consecutive valid frames become one 32-byte
/// SuperPack; a lone frame is released raw by [`BarePairer::flush`]. A frame that fails
/// the gate (only reachable with validation off) cannot be SuperPacked: it flushes any
/// held frame first (order is kept) and goes raw — the `dcf.transport.UdpTransport` rule.
#[derive(Debug, Default, Clone)]
pub struct BarePairer {
    held: Option<Frame17>,
}

impl BarePairer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Push one frame; returns the datagrams now ready (0, 1 or 2).
    pub fn push(&mut self, frame: &Frame17) -> Vec<Vec<u8>> {
        if !gate(frame) {
            let mut out = self.flush();
            out.push(frame.to_vec());
            return out;
        }
        match self.held.take() {
            None => {
                self.held = Some(*frame);
                Vec::new()
            }
            Some(a) => match superpack::pack(&a, frame) {
                Ok(sp) => vec![sp.to_vec()],
                Err(_) => vec![a.to_vec(), frame.to_vec()],
            },
        }
    }

    /// True while a lone frame is waiting for its partner.
    pub fn has_pending(&self) -> bool {
        self.held.is_some()
    }

    /// Release the held frame (if any) as a raw 17-byte datagram.
    pub fn flush(&mut self) -> Vec<Vec<u8>> {
        self.held
            .take()
            .map(|f| vec![f.to_vec()])
            .unwrap_or_default()
    }
}

/// Datagrams for a frame sequence: with `pair`, consecutive pairs become one 32-byte
/// SuperPack and a lone trailing frame goes raw (17 B); without, every frame goes raw.
pub fn bare_encode(frames: &[Frame17], pair: bool) -> Vec<Vec<u8>> {
    if !pair {
        return frames.iter().map(|f| f.to_vec()).collect();
    }
    let mut p = BarePairer::new();
    let mut out = Vec::new();
    for f in frames {
        out.extend(p.push(f));
    }
    out.extend(p.flush());
    out
}

/// Frames carried by one bare datagram: a valid 32-byte SuperPack -> its 2 frames; a
/// 17-byte datagram -> it (not gated — the caller gates); anything else -> none.
pub fn bare_decode(datagram: &[u8]) -> Vec<Frame17> {
    if datagram.len() == SUPER_LEN && superpack::is_superpack(datagram) {
        return match superpack_unpack(datagram) {
            Ok((a, b)) => vec![a, b],
            Err(_) => Vec::new(),
        };
    }
    if datagram.len() == FRAME_LEN {
        return vec![to_frame(datagram)];
    }
    Vec::new()
}

// ══ l2eth (raw-L2 Ethernet payload; byte-identical to hydramodem/dcf-tools/snake_l2.h) ═

/// Length of the `n_frames` u16 header.
pub const L2_HDR: usize = 2;
/// The canonical zero filler frame (a valid DATA DeModFrame with every application field
/// 0) that pairs an odd trailing frame; the receiver discards it using `n_frames`.
pub const L2_FILLER: Frame17 = [
    0xd3, 0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x5b, 0x80,
];

/// Number of DeModFrames that fit one Ethernet payload of the given MTU.
pub fn l2_capacity(mtu: usize) -> usize {
    if mtu < L2_HDR {
        return 0;
    }
    ((mtu - L2_HDR) / SUPER_LEN) * 2
}

/// Batch frames into one Ethernet payload: `[n_frames u16 BE][SuperPack * ceil(n/2)]`,
/// the odd tail paired with [`L2_FILLER`].
pub fn l2_batch(frames: &[Frame17]) -> Result<Vec<u8>, MediumError> {
    let n = frames.len();
    if n > 0xFFFF {
        return Err(MediumError::TooManyFrames);
    }
    let mut out = Vec::with_capacity(L2_HDR + (n + 1) / 2 * SUPER_LEN);
    out.extend_from_slice(&(n as u16).to_be_bytes());
    for pair in frames.chunks(2) {
        let b = if pair.len() == 2 {
            &pair[1]
        } else {
            &L2_FILLER
        };
        out.extend_from_slice(&superpack::pack(&pair[0], b)?);
    }
    Ok(out)
}

/// Split an Ethernet payload back into its frames (bit-exact). Errors on a short or
/// truncated batch or a corrupt SuperPack; trailing bytes after the last SuperPack
/// (Ethernet minimum-size padding) are ignored.
pub fn l2_unbatch(buf: &[u8]) -> Result<Vec<Frame17>, MediumError> {
    if buf.len() < L2_HDR {
        return Err(MediumError::ShortBatch);
    }
    let n = usize::from(u16::from_be_bytes([buf[0], buf[1]]));
    let npairs = (n + 1) / 2;
    if buf.len() < L2_HDR + npairs * SUPER_LEN {
        return Err(MediumError::TruncatedBatch);
    }
    let mut frames = Vec::with_capacity(n);
    for k in 0..npairs {
        let off = L2_HDR + k * SUPER_LEN;
        let (a, b) = superpack_unpack(&buf[off..off + SUPER_LEN])?;
        frames.push(a);
        if frames.len() < n {
            frames.push(b);
        }
    }
    Ok(frames)
}

// ══ hydra_symbols (HydraModem M-FSK; port of hydramodem/src/hydra_{profile,frame,conv,
//    fec,interleave}.c — the C is the ground truth) ════════════════════════════════

/// Payload bytes carried by one HydraModem frame (a DeModFrame).
pub const HYDRA_DCF_BYTES: usize = 17;
/// HydraModem's own CRC-16 appended to the payload.
pub const HYDRA_CRC_BYTES: usize = 2;
/// Sync word length in bits.
pub const HYDRA_SYNC_BITS: usize = 16;
/// Message bits before FEC: (17 + 2) * 8 = 152.
pub const HYDRA_DATA_BITS: usize = (HYDRA_DCF_BYTES + HYDRA_CRC_BYTES) * 8;
/// The HydraModem sync word.
pub const HYDRA_SYNC_WORD: u16 = 0x2DD4;
const HYDRA_CONV_MEM: usize = 6;
const HYDRA_CONV_STATES: usize = 64;
const G0: u32 = 0x79; // 0171 octal; bit6 = newest tap
const G1: u32 = 0x5B; // 0133 octal

/// HydraModem FEC mode (the C `hydra_fec_mode` enum order).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HydraFec {
    None = 0,
    Rep3 = 1,
    Conv = 2,
}

impl HydraFec {
    /// Parse `"none" | "rep3" | "conv"`.
    pub fn from_name(s: &str) -> Option<Self> {
        match s {
            "none" => Some(HydraFec::None),
            "rep3" => Some(HydraFec::Rep3),
            "conv" => Some(HydraFec::Conv),
            _ => None,
        }
    }

    /// The canonical name (also the `frame_tx`/`frame_rx` flag without `--`).
    pub fn name(self) -> &'static str {
        match self {
            HydraFec::None => "none",
            HydraFec::Rep3 => "rep3",
            HydraFec::Conv => "conv",
        }
    }
}

/// A HydraModem profile: the user fields of `hydra_profile_default()` /
/// `hydra_profile_aux_cable()` plus the derived fields `hydra_profile_init()` fills.
/// Construct with [`HydraProfile::default_profile`] / [`HydraProfile::aux`] /
/// [`HydraProfile::named`], adjust user fields, then call [`HydraProfile::init`].
#[derive(Debug, Clone, PartialEq)]
pub struct HydraProfile {
    // user fields
    pub sample_rate: f64,
    pub baud: f64,
    pub n_tones: u32,
    pub base_freq: f64,
    pub tone_spacing: f64,
    pub preamble_syms: u32,
    pub sync_word: u16,
    pub fec: HydraFec,
    pub interleave: bool,
    pub tx_gain: f64,
    // derived (hydra_profile_init)
    pub bits_per_symbol: u32,
    pub samples_per_symbol: u32,
    pub lp_cut: f64,
    pub data_bits: usize,
    pub coded_bits: usize,
    pub interleave_stride: usize,
    pub sync_syms: usize,
    pub data_syms: usize,
    pub total_syms: usize,
}

impl HydraProfile {
    fn user(sample_rate: f64, baud: f64, base_freq: f64, tone_spacing: f64, preamble: u32) -> Self {
        let mut p = HydraProfile {
            sample_rate,
            baud,
            n_tones: 2,
            base_freq,
            tone_spacing,
            preamble_syms: preamble,
            sync_word: HYDRA_SYNC_WORD,
            fec: HydraFec::Conv,
            interleave: true,
            tx_gain: 0.9,
            bits_per_symbol: 0,
            samples_per_symbol: 0,
            lp_cut: 0.0,
            data_bits: 0,
            coded_bits: 0,
            interleave_stride: 0,
            sync_syms: 0,
            data_syms: 0,
            total_syms: 0,
        };
        p.init().expect("built-in hydra profile is valid");
        p
    }

    /// `hydra_profile_default()`: 48 kHz, 1000 baud, 2 tones at 2000/+1000 Hz, preamble
    /// 24, sync 0x2DD4, conv FEC, interleave on (the "FEC off" header comment is stale).
    pub fn default_profile() -> Self {
        Self::user(48000.0, 1000.0, 2000.0, 1000.0, 24)
    }

    /// `hydra_profile_aux_cable()`: 48 kHz, 1200 baud, tones 1200/+1200 Hz, preamble 16,
    /// conv FEC, interleave on.
    pub fn aux() -> Self {
        Self::user(48000.0, 1200.0, 1200.0, 1200.0, 16)
    }

    /// The named profile (`"default"` or `"aux"`), initialised.
    pub fn named(name: &str) -> Option<Self> {
        match name {
            "default" => Some(Self::default_profile()),
            "aux" => Some(Self::aux()),
            _ => None,
        }
    }

    /// Recompute every derived field exactly as `hydra_profile_init()`; errors on a
    /// configuration it rejects.
    pub fn init(&mut self) -> Result<(), MediumError> {
        if !(self.sample_rate > 0.0) || !(self.baud > 0.0) {
            return Err(MediumError::BadProfile("sample_rate/baud must be > 0"));
        }
        if !(self.tone_spacing > 0.0) || !(self.base_freq > 0.0) {
            return Err(MediumError::BadProfile(
                "base_freq/tone_spacing must be > 0",
            ));
        }
        let nt = self.n_tones;
        if nt < 2 || nt & (nt - 1) != 0 {
            return Err(MediumError::BadProfile(
                "n_tones must be a power of two >= 2",
            ));
        }
        let bps = nt.trailing_zeros();
        self.bits_per_symbol = bps;
        let sps = (self.sample_rate / self.baud + 0.5) as i64;
        if sps < 2 {
            return Err(MediumError::BadProfile("samples_per_symbol < 2"));
        }
        self.samples_per_symbol = sps as u32;
        self.lp_cut = self.baud * 0.5;
        self.data_bits = HYDRA_DATA_BITS;
        self.coded_bits = match self.fec {
            HydraFec::None => HYDRA_DATA_BITS,
            HydraFec::Rep3 => 3 * HYDRA_DATA_BITS,
            HydraFec::Conv => hydra_conv_coded_len(HYDRA_DATA_BITS),
        };
        self.interleave_stride = if self.interleave {
            hydra_interleave_stride(self.coded_bits)
        } else {
            1
        };
        let b = bps as usize;
        self.sync_syms = (HYDRA_SYNC_BITS + b - 1) / b;
        self.data_syms = (self.coded_bits + b - 1) / b;
        self.total_syms = self.preamble_syms as usize + self.sync_syms + self.data_syms;
        if self.base_freq + f64::from(nt - 1) * self.tone_spacing >= 0.5 * self.sample_rate {
            return Err(MediumError::BadProfile("highest tone at/above Nyquist"));
        }
        for c in [self.base_freq / self.baud, self.tone_spacing / self.baud] {
            if (c - (c + 0.5).floor()).abs() > 1e-6 {
                return Err(MediumError::BadProfile(
                    "base_freq/tone_spacing must be integer multiples of baud",
                ));
            }
        }
        Ok(())
    }
}

/// Coded length of the K=7 conv code: `2 * (n_msg + 6)`.
pub fn hydra_conv_coded_len(n_msg: usize) -> usize {
    2 * (n_msg + HYDRA_CONV_MEM)
}

fn gcd(mut a: usize, mut b: usize) -> usize {
    while b != 0 {
        let t = a % b;
        a = b;
        b = t;
    }
    a
}

/// Deterministic coprime stride ~ sqrt(n) (`hydra_interleave_stride`).
pub fn hydra_interleave_stride(n: usize) -> usize {
    if n < 3 {
        return 1;
    }
    let mut s = 1usize;
    while s * s < n {
        s += 1;
    }
    if s >= n {
        s = n - 1;
    }
    while s < n && gcd(s, n) != 1 {
        s += 1;
    }
    if s >= n {
        s = 2;
        while s < n && gcd(s, n) != 1 {
            s += 1;
        }
        if s >= n {
            s = 1;
        }
    }
    s
}

/// Bytes -> bits, MSB-first.
pub fn bytes_to_bits(data: &[u8]) -> Vec<u8> {
    data.iter()
        .flat_map(|&b| (0..8).map(move |i| (b >> (7 - i)) & 1))
        .collect()
}

/// Bits -> bytes, MSB-first (a trailing partial byte is dropped).
pub fn bits_to_bytes(bits: &[u8]) -> Vec<u8> {
    bits.chunks_exact(8)
        .map(|c| c.iter().fold(0u8, |v, &b| (v << 1) | (b & 1)))
        .collect()
}

/// Pack bits into symbols MSB-first; the final symbol is zero-padded.
pub fn hydra_bits_to_symbols(bits: &[u8], bps: usize) -> Vec<u8> {
    let nsym = (bits.len() + bps - 1) / bps;
    (0..nsym)
        .map(|s| {
            (0..bps).fold(0u8, |v, b| {
                let idx = s * bps + b;
                (v << 1) | if idx < bits.len() { bits[idx] & 1 } else { 0 }
            })
        })
        .collect()
}

/// Unpack symbols into bits MSB-first.
pub fn hydra_symbols_to_bits(syms: &[u8], bps: usize) -> Vec<u8> {
    syms.iter()
        .flat_map(|&s| (0..bps).map(move |b| (s >> (bps - 1 - b)) & 1))
        .collect()
}

fn parity7(v: u32) -> u8 {
    ((v & 0x7F).count_ones() & 1) as u8
}

/// Trellis step: `(o0, o1, next_state)` for `(state, bit)`.
fn trellis(state: usize, bit: u8) -> (u8, u8, usize) {
    let reg = (u32::from(bit) << 6) | state as u32;
    (parity7(reg & G0), parity7(reg & G1), (reg >> 1) as usize)
}

/// K=7 r=1/2 convolutional encoder (G0=0171, G1=0133): n message bits ->
/// `2*(n+6)` coded bits, tail-flushed to state 0 (`hydra_conv.c`).
pub fn hydra_conv_encode(bits: &[u8]) -> Vec<u8> {
    let mut state = 0usize;
    let mut out = Vec::with_capacity(hydra_conv_coded_len(bits.len()));
    for i in 0..bits.len() + HYDRA_CONV_MEM {
        let b = if i < bits.len() { bits[i] & 1 } else { 0 };
        let (o0, o1, ns) = trellis(state, b);
        out.push(o0);
        out.push(o1);
        state = ns;
    }
    out
}

/// Hard-decision Viterbi: coded bits (`2*(n+6)`) -> n message bits. The C soft decoder's
/// correlation metric on +/-1 hard values (bit 1 -> +1, bit 0 -> -1), the same state/bit
/// iteration order and strict `>` tie rule, start state 0, traceback from state 0.
/// `None` on a bad coded length.
pub fn hydra_conv_decode_hard(coded: &[u8]) -> Option<Vec<u8>> {
    let nc = coded.len();
    if nc % 2 != 0 || nc / 2 <= HYDRA_CONV_MEM {
        return None;
    }
    let l = nc / 2;
    let n_msg = l - HYDRA_CONV_MEM;
    let mut pm: [Option<i32>; HYDRA_CONV_STATES] = [None; HYDRA_CONV_STATES];
    pm[0] = Some(0);
    let mut tb_prev = vec![[0u8; HYDRA_CONV_STATES]; l];
    let mut tb_bit = vec![[0u8; HYDRA_CONV_STATES]; l];
    for t in 0..l {
        let s0: i32 = if coded[2 * t] & 1 != 0 { 1 } else { -1 };
        let s1: i32 = if coded[2 * t + 1] & 1 != 0 { 1 } else { -1 };
        let mut npm: [Option<i32>; HYDRA_CONV_STATES] = [None; HYDRA_CONV_STATES];
        for s in 0..HYDRA_CONV_STATES {
            let base = match pm[s] {
                Some(v) => v,
                None => continue,
            };
            for b in 0..2u8 {
                let (o0, o1, ns) = trellis(s, b);
                let cand = base + if o0 != 0 { s0 } else { -s0 } + if o1 != 0 { s1 } else { -s1 };
                if npm[ns].map_or(true, |cur| cand > cur) {
                    npm[ns] = Some(cand);
                    tb_prev[t][ns] = s as u8;
                    tb_bit[t][ns] = b;
                }
            }
        }
        pm = npm;
    }
    let mut s = 0usize;
    let mut bits = vec![0u8; l];
    for t in (0..l).rev() {
        bits[t] = tb_bit[t][s];
        s = tb_prev[t][s] as usize;
    }
    bits.truncate(n_msg);
    Some(bits)
}

/// Repetition-3 encoder (`hydra_fec.c`).
pub fn hydra_rep3_encode(bits: &[u8]) -> Vec<u8> {
    bits.iter().flat_map(|&b| [b & 1; 3]).collect()
}

/// Repetition-3 majority decoder.
pub fn hydra_rep3_decode(coded: &[u8]) -> Vec<u8> {
    coded
        .chunks_exact(3)
        .map(|c| u8::from((c[0] & 1) + (c[1] & 1) + (c[2] & 1) >= 2))
        .collect()
}

/// TX gather: `out[i] = in[(i*stride) % n]`.
pub fn hydra_interleave(bits: &[u8], stride: usize) -> Vec<u8> {
    let n = bits.len();
    (0..n).map(|i| bits[(i * stride) % n]).collect()
}

/// RX scatter: `out[(i*stride) % n] = in[i]` — the exact inverse of [`hydra_interleave`].
pub fn hydra_deinterleave(bits: &[u8], stride: usize) -> Vec<u8> {
    let n = bits.len();
    let mut out = vec![0u8; n];
    for (i, &b) in bits.iter().enumerate() {
        out[(i * stride) % n] = b;
    }
    out
}

fn hydra_sync_bits(p: &HydraProfile) -> Vec<u8> {
    bytes_to_bits(&p.sync_word.to_be_bytes())
}

const SYMCHARS: &[u8; 16] = b"0123456789abcdef";

/// The full TX symbol stream of `hydra_frame_build()` as a string, one lowercase hex
/// digit per symbol (tone index; needs `n_tones <= 16`):
/// `[preamble: alternating tone 0 / tone n_tones-1, starting with 0]`
/// `[sync_word bits -> symbols]` `[interleave?(fec(frame || CRC16be(frame))) -> symbols]`.
/// `p` must be initialised ([`HydraProfile::init`]).
pub fn hydra_symbols_encode(p: &HydraProfile, frame: &Frame17) -> Result<String, MediumError> {
    if p.n_tones > 16 {
        return Err(MediumError::BadProfile("symbol strings need n_tones <= 16"));
    }
    let mut field = frame.to_vec();
    field.extend_from_slice(&crc16_ccitt(frame).to_be_bytes());
    let data = bytes_to_bits(&field);
    let mut coded = match p.fec {
        HydraFec::None => data,
        HydraFec::Rep3 => hydra_rep3_encode(&data),
        HydraFec::Conv => hydra_conv_encode(&data),
    };
    if coded.len() != p.coded_bits {
        return Err(MediumError::BadProfile("profile not initialised"));
    }
    if p.interleave {
        coded = hydra_interleave(&coded, p.interleave_stride);
    }
    let bps = p.bits_per_symbol as usize;
    let hi = (p.n_tones - 1) as u8;
    let mut syms: Vec<u8> = (0..p.preamble_syms)
        .map(|k| if k & 1 == 1 { hi } else { 0 })
        .collect();
    syms.extend(hydra_bits_to_symbols(&hydra_sync_bits(p), bps));
    syms.extend(hydra_bits_to_symbols(&coded, bps));
    debug_assert_eq!(syms.len(), p.total_syms);
    Ok(syms.iter().map(|&s| SYMCHARS[s as usize] as char).collect())
}

/// Inverse of [`hydra_symbols_encode`] with hard decisions: strip the preamble by count,
/// check the 16 sync bits, deinterleave, FEC-decode (none / rep3 majority / hard
/// Viterbi), then the CRC-16 check. `None` on wrong length, a bad symbol, a sync
/// mismatch or a CRC mismatch.
pub fn hydra_symbols_decode(p: &HydraProfile, symbols: &str) -> Option<Frame17> {
    if symbols.len() != p.total_syms {
        return None;
    }
    let syms: Vec<u8> = symbols.bytes().map(hexval).collect::<Option<Vec<u8>>>()?;
    if syms.iter().any(|&s| u32::from(s) >= p.n_tones) {
        return None;
    }
    let bps = p.bits_per_symbol as usize;
    let mut off = p.preamble_syms as usize;
    let sync = hydra_symbols_to_bits(&syms[off..off + p.sync_syms], bps);
    if sync[..HYDRA_SYNC_BITS] != hydra_sync_bits(p)[..] {
        return None;
    }
    off += p.sync_syms;
    let mut coded = hydra_symbols_to_bits(&syms[off..off + p.data_syms], bps);
    coded.truncate(p.coded_bits);
    if p.interleave {
        coded = hydra_deinterleave(&coded, p.interleave_stride);
    }
    let data = match p.fec {
        HydraFec::None => coded[..HYDRA_DATA_BITS].to_vec(),
        HydraFec::Rep3 => hydra_rep3_decode(&coded),
        HydraFec::Conv => hydra_conv_decode_hard(&coded)?,
    };
    let field = bits_to_bytes(&data);
    if field.len() < HYDRA_DCF_BYTES + HYDRA_CRC_BYTES {
        return None;
    }
    let frame = to_frame(&field);
    let crc = u16::from_be_bytes([field[HYDRA_DCF_BYTES], field[HYDRA_DCF_BYTES + 1]]);
    if crc16_ccitt(&frame) == crc {
        Some(frame)
    } else {
        None
    }
}

// ══ afsk_bits (python/modem acoustic_frame: preamble + 0x7E + frame+crc8 | RS16 + postamble) ═

/// The AFSK sync byte.
pub const AFSK_SYNC: u8 = 0x7E;
/// Alternating postamble bits after the payload.
pub const AFSK_POSTAMBLE_BITS: usize = 16;
/// RS parity bytes in AFSK fec mode.
pub const AFSK_NPARITY: usize = fec::RS_DEFAULT_NPARITY;

/// One AFSK tone plan (`acoustic_frame.PROFILES`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AfskProfile {
    pub name: &'static str,
    pub mark: f64,
    pub space: f64,
    pub baud: u32,
    pub preamble_bits: usize,
}

/// `standard`, `handheld`, `aux-cable`.
pub const AFSK_PROFILES: [AfskProfile; 3] = [
    AfskProfile {
        name: "standard",
        mark: 1200.0,
        space: 2200.0,
        baud: 300,
        preamble_bits: 80,
    },
    AfskProfile {
        name: "handheld",
        mark: 1200.0,
        space: 1800.0,
        baud: 300,
        preamble_bits: 240,
    },
    AfskProfile {
        name: "aux-cable",
        mark: 1000.0,
        space: 1500.0,
        baud: 1200,
        preamble_bits: 16,
    },
];

/// Look up an AFSK profile by name.
pub fn afsk_profile(name: &str) -> Option<AfskProfile> {
    AFSK_PROFILES.iter().copied().find(|p| p.name == name)
}

/// Poly-0x31 CRC-8 (MSB-first, init 0x00, non-reflected) — the deployed Faust modem's
/// frame check (labelled "MAXIM" there; it is not reflected).
pub fn crc8_afsk(data: &[u8]) -> u8 {
    let mut crc = 0u8;
    for &b in data {
        crc ^= b;
        for _ in 0..8 {
            crc = if crc & 0x80 != 0 {
                (crc << 1) ^ 0x31
            } else {
                crc << 1
            };
        }
    }
    crc
}

/// The on-air AFSK bit stream (`'0'`/`'1'` string) of `acoustic_frame.encode_bits`:
/// preamble (alternating 0,1.. x `preamble_bits`) + 0x7E + (frame+crc8 | RS16 codeword)
/// + 16 alternating postamble bits.
pub fn afsk_bits_encode(
    frame: &Frame17,
    profile: &str,
    fec_mode: bool,
) -> Result<String, MediumError> {
    let p = afsk_profile(profile).ok_or(MediumError::UnknownAfskProfile)?;
    let payload = if fec_mode {
        fec::rs_encode(frame, AFSK_NPARITY)
    } else {
        let mut v = frame.to_vec();
        v.push(crc8_afsk(frame));
        v
    };
    let mut bits: Vec<u8> = (0..p.preamble_bits).map(|i| (i % 2) as u8).collect();
    bits.extend(bytes_to_bits(&[AFSK_SYNC]));
    bits.extend(bytes_to_bits(&payload));
    bits.extend((0..AFSK_POSTAMBLE_BITS).map(|i| (i % 2) as u8));
    Ok(bits
        .iter()
        .map(|&b| if b != 0 { '1' } else { '0' })
        .collect())
}

/// Inverse (`acoustic_frame.decode_bits`: bit-level 0x7E search, then the crc8 check or
/// RS decode). `None` on no sync, a short payload, a CRC mismatch or an uncorrectable
/// codeword. `profile` is validated for symmetry; the bit layer is preamble agnostic.
pub fn afsk_bits_decode(bits: &str, profile: &str, fec_mode: bool) -> Option<Frame17> {
    afsk_profile(profile)?;
    let lst: Vec<u8> = bits
        .bytes()
        .map(|c| match c {
            b'0' => Some(0),
            b'1' => Some(1),
            _ => None,
        })
        .collect::<Option<Vec<u8>>>()?;
    let pat = bytes_to_bits(&[AFSK_SYNC]);
    let pos = lst.windows(pat.len()).position(|w| w == &pat[..])? + pat.len();
    let data = bits_to_bytes(&lst[pos..]);
    if fec_mode {
        let n = FRAME_LEN + AFSK_NPARITY;
        if data.len() < n {
            return None;
        }
        let (msg, _) = fec::rs_decode(&data[..n], AFSK_NPARITY, Some(FRAME_LEN)).ok()?;
        return Some(to_frame(&msg));
    }
    if data.len() < FRAME_LEN + 1 {
        return None;
    }
    if crc8_afsk(&data[..FRAME_LEN]) != data[FRAME_LEN] {
        return None;
    }
    Some(to_frame(&data))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Frame, FrameType};

    fn f1() -> Frame17 {
        Frame::new(
            1,
            FrameType::Ctrl,
            0x1234,
            0x0001,
            0xFFFF,
            [0xde, 0xad, 0xbe, 0xef],
            0xAB12CD,
        )
        .encode()
    }
    fn f2() -> Frame17 {
        Frame::new(
            1,
            FrameType::Data,
            0x0102,
            0x0304,
            0x0506,
            [0xca, 0xfe, 0xba, 0xbe],
            0x010203,
        )
        .encode()
    }

    #[test]
    fn stream_resync_and_chunk_invariance() {
        let (a, b) = (f1(), f2());
        assert_eq!(to_hex(&a), "d31312340001ffffdeadbeefab12cd24c0");
        let mut blob = vec![0x00, 0xd3, 0x1f];
        blob.extend_from_slice(&a);
        blob.extend_from_slice(b"junk\xd3");
        blob.extend_from_slice(&b);
        blob.extend_from_slice(&[0xd3, 0x13, 0x00]);
        let (fr, sk, tail) = stream_decode(&blob);
        assert_eq!(fr, vec![a, b]);
        assert_eq!(tail, 3);
        for step in 1..25 {
            let mut sc = StreamScanner::new();
            let mut got = Vec::new();
            for c in blob.chunks(step) {
                got.extend(sc.feed(c));
            }
            assert_eq!(got, fr);
            assert_eq!(sc.skipped_bytes(), sk);
            assert_eq!(sc.flush().len(), tail);
        }
    }

    #[test]
    fn hex_proto_bare_l2() {
        let (a, b) = (f1(), f2());
        assert_eq!(hex_decode(&hex_encode(&[a, b])), (vec![a, b], 0));
        let t = format!("# c\n\n  {}\r\nzz\n", to_hex(&a).to_uppercase());
        assert_eq!(hex_decode(&t), (vec![a], 1));
        let g = proto_encode(1, 42, 0x0102030405060708, &[1, 2, 3]);
        assert_eq!(to_hex(&g), "010000002a010203040506070800000003010203");
        assert_eq!(
            proto_decode(&g).unwrap(),
            (1, 42, 0x0102030405060708, &[1u8, 2, 3][..])
        );
        assert_eq!(proto_decode(&[1u8; 16]), Err(MediumError::ShortHeader));
        assert_eq!(proto_decode(&g[..g.len() - 1]), Err(MediumError::Overrun));
        let d = proto_frame_encode(&a, 1, 0);
        assert_eq!(proto_frame_decode(&d), Some(a));
        assert_eq!(proto_frame_decode(&proto_encode(7, 1, 0, &[])), None);
        let dg = bare_encode(&[a, b, a], true);
        assert_eq!(dg.iter().map(|x| x.len()).collect::<Vec<_>>(), vec![32, 17]);
        assert_eq!(
            dg.iter().flat_map(|x| bare_decode(x)).collect::<Vec<_>>(),
            vec![a, b, a]
        );
        for n in 0..6 {
            let fs: Vec<Frame17> = [a, b, a, b, a][..n].to_vec();
            let pl = l2_batch(&fs).unwrap();
            assert_eq!(pl.len(), L2_HDR + (n + 1) / 2 * 32);
            assert_eq!(l2_unbatch(&pl).unwrap(), fs);
        }
        assert!(gate(&L2_FILLER));
    }

    #[test]
    fn bare_and_l2_carry_reserved_type_nibbles() {
        // a gated frame with type nibble 8 (the golden encode_basis has 4 and 8)
        let mut f = f2();
        f[1] = 0x18;
        let crc = crc16_ccitt(&f[..15]);
        f[15..17].copy_from_slice(&crc.to_be_bytes());
        assert!(gate(&f));
        let dg = bare_encode(&[f, f1()], true);
        assert_eq!(dg.len(), 1);
        assert_eq!(bare_decode(&dg[0]), vec![f, f1()]);
        assert_eq!(l2_unbatch(&l2_batch(&[f]).unwrap()).unwrap(), vec![f]);
    }

    #[test]
    fn hydra_profiles_and_roundtrip() {
        let want = [("default", [192, 496, 356]), ("aux", [184, 488, 348])];
        for (name, totals) in want {
            for (k, fec) in [HydraFec::None, HydraFec::Rep3, HydraFec::Conv]
                .into_iter()
                .enumerate()
            {
                for il in [false, true] {
                    let mut p = HydraProfile::named(name).unwrap();
                    p.fec = fec;
                    p.interleave = il;
                    p.init().unwrap();
                    assert_eq!(p.coded_bits, [152, 456, 316][k]);
                    assert_eq!(p.total_syms, totals[k]);
                    if il {
                        assert_eq!(p.interleave_stride, [13, 23, 19][k]);
                    }
                    let s = hydra_symbols_encode(&p, &f1()).unwrap();
                    assert_eq!(s.len(), p.total_syms);
                    assert_eq!(hydra_symbols_decode(&p, &s), Some(f1()));
                }
            }
        }
        let bits = bytes_to_bits(&f1());
        assert_eq!(
            hydra_conv_decode_hard(&hydra_conv_encode(&bits)).unwrap(),
            bits
        );
    }

    #[test]
    fn afsk_lengths_and_roundtrip() {
        assert_eq!(crc8_afsk(b"123456789"), 0xA2);
        for (prof, l0, l1) in [
            ("standard", 248, 368),
            ("handheld", 408, 528),
            ("aux-cable", 184, 304),
        ] {
            for (fecm, ln) in [(false, l0), (true, l1)] {
                let b = afsk_bits_encode(&f1(), prof, fecm).unwrap();
                assert_eq!(b.len(), ln);
                assert_eq!(afsk_bits_decode(&b, prof, fecm), Some(f1()));
            }
        }
    }
}
