// SPDX-License-Identifier: LGPL-3.0-only
//! punctim — the DCF medium tool (Rust implementation; std-only, hand-rolled argv).
//!
//! Reads DeModFrames from any medium and emits them on any other, byte-deterministically;
//! encodes/decodes single frames; certifies the medium codecs against the golden vectors.
//! The same CLI exists in Python (reference), C, Go and Node — see
//! `Documentation/DCF_MEDIUM_SPEC.md` and `python/punctim.py`:
//!
//! ```text
//! punctim version [--json]
//! punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
//!                 [--no-validate] [--stats] [--queue N]
//! punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
//! punctim decode  (HEX | --stdin) [--json]
//! punctim certify [--vectors DIR] [--family NAME ...]
//! ```
//!
//! Exit codes: 0 ok · 1 I/O error · 2 usage · 3 medium unsupported · 4 certification
//! failed · 5 invalid frame · 6 `--expect` not met.
//!
//! Media in this build: `file:` `stdio:` `hex:` `udp:` (proto + bare) and `hydra:` (via the
//! HydraModem `frame_tx`/`frame_rx` tools). `loop:` and `l2eth:` are in-process/raw-socket
//! media of the Python runtime, and `afsk:`/`audio:`/`sdr:`/`janus:` need the Python modems:
//! they parse but exit 3 (medium unsupported) here.
//!
//! Determinism rule (normative): for finite inputs (file without follow, hex, stdio),
//! `punctim io` produces byte-identical output to every other language's `punctim` for
//! identical input and URI (`udp:dialect=proto` needs `ts=0`, the default).

use dcf_wire_codec::crc16_ccitt;
use dcf_wire_codec::medium::{self as m, Frame17};
use serde_json::Value;
use std::collections::{BTreeMap, HashSet, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, ToSocketAddrs, UdpSocket};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const EXIT_OK: i32 = 0;
const EXIT_IO: i32 = 1;
const EXIT_USAGE: i32 = 2;
const EXIT_UNSUPPORTED: i32 = 3;
const EXIT_CERT: i32 = 4;
const EXIT_INVALID: i32 = 5;
const EXIT_EXPECT: i32 = 6;

const IMPL: &str = "rust";
const VERSION: &str = env!("CARGO_PKG_VERSION");
/// The media this build can open (others parse, then exit 3).
const MEDIA: [&str; 5] = ["file", "stdio", "hex", "udp", "hydra"];
const FAMILIES: [&str; 7] = [
    "stream",
    "hex",
    "udp_proto",
    "udp_bare",
    "l2eth",
    "hydra_symbols",
    "afsk_bits",
];

const HELP: &str = "\
usage: punctim {version,io,encode,decode,certify} ...

DCF medium tool: move DeModFrames between any two media, deterministically
(Documentation/DCF_MEDIUM_SPEC.md).

  punctim version [--json]
  punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
                  [--no-validate] [--stats] [--queue N]
  punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
  punctim decode  (HEX | --stdin) [--json]
  punctim certify [--vectors DIR] [--family NAME ...]

media (this build): file:path=,append=,follow=,mode=  stdio:  hex:[path=,append=,follow=]
  udp:dialect=proto|bare,bind=,peer=a|b,pair=1,flush_ms=20,ts=0|now,seq_start=1
  hydra:in=DIR,out=DIR,profile=default|aux,fec=none|rep3|conv,interleave=0|1,
        base_freq=,tone_spacing=,baud=,n_tones=,impl=tool,tx=,rx=
  (loop: l2eth: afsk: audio: sdr: janus: -> exit 3 in the Rust build)
exit: 0 ok, 1 I/O, 2 usage, 3 medium unsupported, 4 cert failed, 5 invalid frame,
      6 --expect not met
";

// ── errors ────────────────────────────────────────────────────────────────────
#[derive(Debug)]
enum Fail {
    Usage(String),
    Unsupported(String),
    Io(io::Error),
}

impl From<io::Error> for Fail {
    fn from(e: io::Error) -> Self {
        Fail::Io(e)
    }
}

type Res<T> = Result<T, Fail>;

fn usage<T>(msg: impl Into<String>) -> Res<T> {
    Err(Fail::Usage(msg.into()))
}

// ── SIGINT / SIGTERM: set a flag; a second signal exits at once ────────────────
#[cfg(unix)]
mod sig {
    use std::sync::atomic::{AtomicBool, Ordering};
    pub static STOP: AtomicBool = AtomicBool::new(false);
    extern "C" {
        fn signal(signum: i32, handler: usize) -> usize;
        fn _exit(code: i32) -> !;
    }
    extern "C" fn on_signal(_: i32) {
        if STOP.swap(true, Ordering::SeqCst) {
            unsafe { _exit(130) }
        }
    }
    pub fn install() {
        let h = on_signal as extern "C" fn(i32) as usize;
        unsafe {
            signal(2, h); // SIGINT
            signal(15, h); // SIGTERM
        }
    }
    pub fn stopped() -> bool {
        STOP.load(Ordering::SeqCst)
    }
}

#[cfg(not(unix))]
mod sig {
    pub fn install() {}
    pub fn stopped() -> bool {
        false
    }
}

// ── argv ──────────────────────────────────────────────────────────────────────
/// Parsed `--flag value` / `--flag=value` options + positionals. Valued flags take the
/// next argument verbatim; `multi` flags take every following argument up to the next
/// `--flag` (argparse `nargs="+"`); a repeated flag keeps its last value.
struct Opts {
    vals: BTreeMap<String, Vec<String>>,
    pos: Vec<String>,
}

impl Opts {
    fn parse(argv: &[String], valued: &[&str], boolean: &[&str], multi: &[&str]) -> Res<Opts> {
        let mut vals: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut pos = Vec::new();
        let mut i = 0;
        while i < argv.len() {
            let a = &argv[i];
            i += 1;
            if a == "-h" || a == "--help" {
                print!("{}", HELP);
                std::process::exit(EXIT_OK);
            }
            if !a.starts_with("--") || a == "--" {
                pos.push(a.clone());
                continue;
            }
            let (name, inline) = match a.find('=') {
                Some(k) => (a[2..k].to_string(), Some(a[k + 1..].to_string())),
                None => (a[2..].to_string(), None),
            };
            if valued.contains(&name.as_str()) {
                let v = match inline {
                    Some(v) => v,
                    None => {
                        if i >= argv.len() {
                            return usage(format!("argument --{}: expected one argument", name));
                        }
                        i += 1;
                        argv[i - 1].clone()
                    }
                };
                vals.insert(name, vec![v]);
            } else if boolean.contains(&name.as_str()) {
                if inline.is_some() {
                    return usage(format!("argument --{}: ignored explicit argument", name));
                }
                vals.insert(name, Vec::new());
            } else if multi.contains(&name.as_str()) {
                let mut v: Vec<String> = inline.into_iter().collect();
                while i < argv.len() && !argv[i].starts_with("--") {
                    v.push(argv[i].clone());
                    i += 1;
                }
                if v.is_empty() {
                    return usage(format!(
                        "argument --{}: expected at least one argument",
                        name
                    ));
                }
                vals.insert(name, v);
            } else {
                return usage(format!("unrecognized arguments: {}", a));
            }
        }
        Ok(Opts { vals, pos })
    }

    fn get(&self, k: &str) -> Option<&str> {
        self.vals.get(k).and_then(|v| v.first()).map(|s| s.as_str())
    }

    fn has(&self, k: &str) -> bool {
        self.vals.contains_key(k)
    }
}

/// Python `int(s, 0)`-style: optional sign, `0x`/`0o`/`0b` prefixes, else decimal.
fn parse_int0(s: &str) -> Option<i128> {
    let t = s.trim();
    let (neg, t) = match t.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    let lower = t.to_ascii_lowercase();
    let v = if let Some(h) = lower.strip_prefix("0x") {
        i128::from_str_radix(h, 16).ok()?
    } else if let Some(o) = lower.strip_prefix("0o") {
        i128::from_str_radix(o, 8).ok()?
    } else if let Some(b) = lower.strip_prefix("0b") {
        i128::from_str_radix(b, 2).ok()?
    } else {
        if t.is_empty() || !t.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        t.parse::<i128>().ok()?
    };
    Some(if neg { -v } else { v })
}

/// punctim's own number rule (Python `_num`/`_int`): `0x` hex, else decimal.
fn parse_int_hex_or_dec(s: &str) -> Option<i128> {
    let t = s.trim();
    let (neg, body) = match t.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    let v = if body.len() >= 2 && body[..2].eq_ignore_ascii_case("0x") {
        let h = &body[2..];
        if h.is_empty() || !h.bytes().all(|c| c.is_ascii_hexdigit()) {
            return None;
        }
        i128::from_str_radix(h, 16).ok()?
    } else {
        if body.is_empty() || !body.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        body.parse::<i128>().ok()?
    };
    Some(if neg { -v } else { v })
}

fn num_arg(s: &str, what: &str, lo: i128, hi: i128) -> Res<i128> {
    let v = match parse_int_hex_or_dec(s) {
        Some(v) => v,
        None => {
            return usage(format!(
                "{}: '{}' is not an integer (decimal or 0x hex)",
                what, s
            ))
        }
    };
    if v < lo || v > hi {
        return usage(format!("{}: {} out of range {}..{}", what, v, lo, hi));
    }
    Ok(v)
}

fn nonneg_arg(o: &Opts, k: &str) -> Res<Option<u64>> {
    match o.get(k) {
        None => Ok(None),
        Some(s) => match parse_int0(s) {
            Some(v) if v >= 0 && v <= u64::MAX as i128 => Ok(Some(v as u64)),
            Some(_) => usage(format!("argument --{}: must be >= 0", k)),
            None => usage(format!("argument --{}: invalid int value: '{}'", k, s)),
        },
    }
}

// ── JSON output (Python json.dumps(..., separators=(",", ":")), ensure_ascii) ──
fn json_str(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            '\u{8}' => o.push_str("\\b"),
            '\u{c}' => o.push_str("\\f"),
            c if (' '..='~').contains(&c) => o.push(c),
            c => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    o.push_str(&format!("\\u{:04x}", u));
                }
            }
        }
    }
    o.push('"');
    o
}

/// Python `repr(round(x, 3))` for a non-negative float.
fn json_secs(x: f64) -> String {
    let r = (x * 1000.0).round() / 1000.0;
    let s = format!("{}", r);
    if s.contains('.') || s.contains('e') || s.contains("inf") || s.contains("NaN") {
        s
    } else {
        format!("{}.0", s)
    }
}

enum J {
    S(String),
    I(i128),
    B(bool),
    Raw(String),
}

fn json_obj(fields: &[(&str, J)]) -> String {
    let parts: Vec<String> = fields
        .iter()
        .map(|(k, v)| {
            let val = match v {
                J::S(s) => json_str(s),
                J::I(i) => i.to_string(),
                J::B(b) => b.to_string(),
                J::Raw(r) => r.clone(),
            };
            format!("{}:{}", json_str(k), val)
        })
        .collect();
    format!("{{{}}}", parts.join(","))
}

// ── version ───────────────────────────────────────────────────────────────────
fn cmd_version(argv: &[String]) -> Res<i32> {
    let o = Opts::parse(argv, &[], &["json"], &[])?;
    if let Some(p) = o.pos.first() {
        return usage(format!("unrecognized arguments: {}", p));
    }
    if o.has("json") {
        let list = |xs: &[&str]| {
            format!(
                "[{}]",
                xs.iter().map(|x| json_str(x)).collect::<Vec<_>>().join(",")
            )
        };
        println!(
            "{}",
            json_obj(&[
                ("name", J::S("punctim".into())),
                ("version", J::S(VERSION.into())),
                ("impl", J::S(IMPL.into())),
                ("media", J::Raw(list(&MEDIA))),
                ("families", J::Raw(list(&FAMILIES))),
            ])
        );
    } else {
        println!("punctim {} ({})", VERSION, IMPL);
    }
    Ok(EXIT_OK)
}

// ── encode ────────────────────────────────────────────────────────────────────
fn cmd_encode(argv: &[String]) -> Res<i32> {
    let o = Opts::parse(
        argv,
        &["type", "seq", "src", "dst", "payload", "text", "ts"],
        &[],
        &[],
    )?;
    if let Some(p) = o.pos.first() {
        return usage(format!("unrecognized arguments: {}", p));
    }
    let req = |k: &str| -> Res<&str> {
        match o.get(k) {
            Some(v) => Ok(v),
            None => usage(format!("the following arguments are required: --{}", k)),
        }
    };
    let (ts_, seq_, src_, dst_) = (req("type")?, req("seq")?, req("src")?, req("dst")?);
    let payload = match (o.get("payload"), o.get("text")) {
        (Some(_), Some(_)) => return usage("argument --text: not allowed with argument --payload"),
        (None, None) => return usage("one of the arguments --payload --text is required"),
        (Some(p), None) => {
            let p = p.trim();
            if p.len() != 8 || !p.bytes().all(|c| c.is_ascii_hexdigit()) {
                return usage("--payload wants exactly 8 hex digits (4 bytes)");
            }
            let b = m::from_hex(p).expect("validated hex");
            [b[0], b[1], b[2], b[3]]
        }
        (None, Some(t)) => {
            let b = t.as_bytes();
            if b.len() > 4 {
                return usage("--text is at most 4 UTF-8 bytes (zero-padded)");
            }
            let mut p = [0u8; 4];
            p[..b.len()].copy_from_slice(b);
            p
        }
    };
    let t = num_arg(ts_, "--type", 0, 15)? as u8;
    let seq = num_arg(seq_, "--seq", 0, 0xFFFF)? as u16;
    let src = num_arg(src_, "--src", 0, 0xFFFF)? as u16;
    let dst = num_arg(dst_, "--dst", 0, 0xFFFF)? as u16;
    let ts = (num_arg(o.get("ts").unwrap_or("0"), "--ts", 0, u64::MAX as i128)? & 0xFF_FFFF) as u32;
    let f = encode_raw(t, seq, src, dst, payload, ts);
    println!("{}", m::to_hex(&f));
    Ok(EXIT_OK)
}

/// `wirelab_core.encode`: any type nibble 0..15 (FrameType only models 0..3).
fn encode_raw(t: u8, seq: u16, src: u16, dst: u16, payload: [u8; 4], ts: u32) -> Frame17 {
    let mut b = [0u8; 17];
    b[0] = 0xD3;
    b[1] = (1 << 4) | (t & 0x0F);
    b[2..4].copy_from_slice(&seq.to_be_bytes());
    b[4..6].copy_from_slice(&src.to_be_bytes());
    b[6..8].copy_from_slice(&dst.to_be_bytes());
    b[8..12].copy_from_slice(&payload);
    b[12..15].copy_from_slice(&ts.to_be_bytes()[1..]);
    let crc = crc16_ccitt(&b[..15]);
    b[15..17].copy_from_slice(&crc.to_be_bytes());
    b
}

// ── decode ────────────────────────────────────────────────────────────────────
struct Rec {
    hex: String,
    valid: bool,
    error: Option<String>,
    fields: Option<(u16, [u8; 17])>, // (syndrome, word)
}

fn type_name(t: u8) -> String {
    match t {
        0 => "FData".into(),
        1 => "FAck".into(),
        2 => "FBeacon".into(),
        3 => "FCtrl".into(),
        _ => format!("0x{:X}", t),
    }
}

/// `python/punctim.py:decode_record` — wirelab_core.decode() fields + hex/valid/syndrome
/// (+ error when invalid); fields are read raw from a 17-byte word even if the gate fails.
fn decode_record(text: &str) -> Rec {
    let s = text.trim_matches(|c| matches!(c, ' ' | '\t' | '\r' | '\u{b}' | '\u{c}'));
    let bytes = match m::from_hex(s) {
        Some(b) => b,
        None => {
            return Rec {
                hex: s.to_string(),
                valid: false,
                error: Some("not hex".into()),
                fields: None,
            }
        }
    };
    if bytes.len() != 17 {
        return Rec {
            hex: m::to_hex(&bytes),
            valid: false,
            error: Some(format!("length {} != 17", bytes.len())),
            fields: None,
        };
    }
    let mut w = [0u8; 17];
    w.copy_from_slice(&bytes);
    let syn = crc16_ccitt(&w[..15]) ^ u16::from_be_bytes([w[15], w[16]]);
    let error = if w[0] != 0xD3 {
        Some("bad sync byte")
    } else if w[1] >> 4 != 1 {
        Some("bad version nibble")
    } else if syn != 0 {
        Some("CRC mismatch")
    } else {
        None
    };
    Rec {
        hex: m::to_hex(&w),
        valid: error.is_none(),
        error: error.map(String::from),
        fields: Some((syn, w)),
    }
}

fn rec_json(r: &Rec) -> String {
    let mut f: Vec<(&str, J)> = vec![("hex", J::S(r.hex.clone())), ("valid", J::B(r.valid))];
    if let Some((syn, w)) = r.fields {
        let be = |a: usize| i128::from(u16::from_be_bytes([w[a], w[a + 1]]));
        f.push(("syndrome", J::I(i128::from(syn))));
        f.push(("frame_type", J::I(i128::from(w[1] & 0x0F))));
        f.push(("frame_type_name", J::S(type_name(w[1] & 0x0F))));
        f.push(("seq", J::I(be(2))));
        f.push(("src", J::I(be(4))));
        f.push(("dst", J::I(be(6))));
        f.push(("payload", J::S(m::to_hex(&w[8..12]))));
        f.push((
            "ts_us",
            J::I(i128::from(u32::from_be_bytes([0, w[12], w[13], w[14]]))),
        ));
        f.push(("crc", J::I(be(15))));
    }
    if let Some(e) = &r.error {
        f.push(("error", J::S(e.clone())));
    }
    json_obj(&f)
}

fn rec_human(r: &Rec) -> String {
    let (syn, w) = match r.fields {
        None => return format!("invalid ({}) {}", r.error.as_deref().unwrap_or(""), r.hex),
        Some(x) => x,
    };
    let head = if r.valid {
        "valid".to_string()
    } else {
        format!("invalid ({})", r.error.as_deref().unwrap_or(""))
    };
    let be = |a: usize| u16::from_be_bytes([w[a], w[a + 1]]);
    format!(
        "{} type={} ({}) seq={} src={} dst={} payload={} ts_us={} crc=0x{:04x} syndrome=0x{:04x}",
        head,
        w[1] & 0x0F,
        type_name(w[1] & 0x0F),
        be(2),
        be(4),
        be(6),
        m::to_hex(&w[8..12]),
        u32::from_be_bytes([0, w[12], w[13], w[14]]),
        be(15),
        syn
    )
}

fn cmd_decode(argv: &[String]) -> Res<i32> {
    let o = Opts::parse(argv, &[], &["stdin", "json"], &[])?;
    if o.pos.len() > 1 {
        return usage(format!("unrecognized arguments: {}", o.pos[1..].join(" ")));
    }
    let from_stdin = o.has("stdin");
    if from_stdin == !o.pos.is_empty() {
        return usage("decode wants exactly one of HEX or --stdin");
    }
    let items: Vec<String> = if from_stdin {
        let mut raw = Vec::new();
        io::stdin().lock().read_to_end(&mut raw)?;
        raw.split(|&c| c == b'\n')
            .map(|l| l.iter().map(|&b| b as char).collect::<String>()) // latin-1
            .map(|s| {
                s.trim_matches(|c| matches!(c, ' ' | '\t' | '\r' | '\u{b}' | '\u{c}' | '\n'))
                    .to_string()
            })
            .filter(|s| !s.is_empty() && !s.starts_with('#'))
            .collect()
    } else {
        vec![o.pos[0].clone()]
    };
    let mut rc = EXIT_OK;
    let out = io::stdout();
    let mut out = out.lock();
    for it in &items {
        let r = decode_record(it);
        if !r.valid {
            rc = EXIT_INVALID;
        }
        writeln!(
            out,
            "{}",
            if o.has("json") {
                rec_json(&r)
            } else {
                rec_human(&r)
            }
        )?;
    }
    Ok(rc)
}

// ══ io: medium URIs ══════════════════════════════════════════════════════════
const SCHEMES: [(&str, &[&str]); 12] = [
    ("file", &["path", "mode", "append", "follow", "in", "out"]),
    ("stdio", &[]),
    ("hex", &["path", "mode", "append", "follow"]),
    (
        "udp",
        &[
            "dialect",
            "bind",
            "peer",
            "pair",
            "flush_ms",
            "ts",
            "seq_start",
        ],
    ),
    (
        "l2eth",
        &["if", "ethertype", "dst", "mtu", "impl", "id", "flush_ms"],
    ),
    ("loop", &["id"]),
    (
        "hydra",
        &[
            "in",
            "out",
            "profile",
            "fec",
            "interleave",
            "base_freq",
            "tone_spacing",
            "baud",
            "n_tones",
            "impl",
            "tx",
            "rx",
        ],
    ),
    ("afsk", &["in", "out", "profile", "fec"]),
    ("audio", &["in", "out", "profile", "fec"]),
    ("sdr", &["in", "out", "mod"]),
    (
        "janus",
        &["in", "out", "pset", "fs", "pset_file", "tx", "rx"],
    ),
    (
        "mc",
        &["rcon", "pass_file", "pass_env", "fifo", "log", "bot", "egress", "ns", "poll_hz"],
    ),
];

struct Uri {
    scheme: String,
    kw: BTreeMap<String, String>,
}

impl Uri {
    fn g(&self, k: &str) -> Option<&str> {
        self.kw.get(k).map(|s| s.as_str())
    }
    fn g_nonempty(&self, k: &str) -> Option<&str> {
        self.g(k).filter(|s| !s.is_empty())
    }
}

/// `SCHEME[:k=v,...]` (the `python/dcf/medium.py:parse_uri` grammar): scheme is
/// case-insensitive; keys are validated per scheme (+ `name`); values verbatim; a later
/// duplicate wins; `|` inside a value separates multiple values.
fn parse_uri(spec: &str) -> Res<Uri> {
    if spec.trim().is_empty() {
        return usage("empty medium URI");
    }
    let (scheme, rest) = match spec.find(':') {
        Some(k) => (&spec[..k], &spec[k + 1..]),
        None => (spec, ""),
    };
    let scheme = scheme.trim().to_ascii_lowercase();
    let allowed = match SCHEMES.iter().find(|(s, _)| *s == scheme) {
        Some((_, keys)) => *keys,
        None => {
            let names: Vec<&str> = SCHEMES.iter().map(|(s, _)| *s).collect();
            return usage(format!(
                "unknown medium '{}' (one of: {})",
                scheme,
                names.join(", ")
            ));
        }
    };
    let mut kw = BTreeMap::new();
    for item in rest.split(',') {
        if item.is_empty() {
            continue;
        }
        let (k, v) = match item.find('=') {
            Some(p) => (item[..p].trim(), &item[p + 1..]),
            None => return usage(format!("{}: bad item '{}' (want key=value)", scheme, item)),
        };
        if k != "name" && !allowed.contains(&k) {
            let mut keys: Vec<&str> = allowed.to_vec();
            keys.push("name");
            keys.sort_unstable();
            return usage(format!(
                "{}: unknown key '{}' (keys: {})",
                scheme,
                k,
                keys.join(", ")
            ));
        }
        kw.insert(k.to_string(), v.to_string());
    }
    Ok(Uri { scheme, kw })
}

fn multi(v: &str) -> Vec<&str> {
    v.split('|').filter(|s| !s.is_empty()).collect()
}

fn uri_bool(v: &str, key: &str) -> Res<bool> {
    match v.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" | "" => Ok(false),
        _ => usage(format!("{}='{}': want 0|1", key, v)),
    }
}

fn uri_int(v: &str, key: &str) -> Res<i128> {
    match parse_int_hex_or_dec(v) {
        Some(x) => Ok(x),
        None => usage(format!(
            "{}='{}': want an integer (decimal or 0x hex)",
            key, v
        )),
    }
}

fn uri_num(v: &str, key: &str) -> Res<f64> {
    match v.trim().parse::<f64>() {
        Ok(x) => Ok(x),
        Err(_) => usage(format!("{}='{}': want a number", key, v)),
    }
}

fn choice<'a>(v: &'a str, key: &str, choices: &[&str]) -> Res<&'a str> {
    if choices.contains(&v) {
        Ok(v)
    } else {
        usage(format!("{}='{}': want {}", key, v, choices.join("|")))
    }
}

fn hostport(s: &str, key: &str) -> Res<(String, u16)> {
    let (host, port) = match s.rfind(':') {
        Some(k) if k > 0 => (&s[..k], &s[k + 1..]),
        _ => return usage(format!("{}='{}': want host:port", key, s)),
    };
    let p = uri_int(port, key)?;
    if !(0..=65535).contains(&p) {
        return usage(format!("{}='{}': port out of range", key, s));
    }
    Ok((
        host.trim_start_matches('[')
            .trim_end_matches(']')
            .to_string(),
        p as u16,
    ))
}

fn resolve(h: &(String, u16)) -> io::Result<SocketAddr> {
    (h.0.as_str(), h.1)
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                format!("cannot resolve {}:{}", h.0, h.1),
            )
        })
}

/// `finite(spec)`: stdio, and hex/file without follow.
fn is_finite(u: &Uri) -> Res<bool> {
    Ok(match u.scheme.as_str() {
        "stdio" => true,
        "hex" | "file" => !uri_bool(u.g("follow").unwrap_or("0"), "follow")?,
        _ => false,
    })
}

// ── the HydraModem tool PHY (frame_tx / frame_rx) ────────────────────────────
struct HydraTool {
    tx: PathBuf,
    rx: PathBuf,
    fec: String,
    prof: Vec<String>,
}

fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for d in std::env::split_paths(&path) {
        let p = d.join(name);
        if is_exec(&p) {
            return Some(p);
        }
    }
    None
}

#[cfg(unix)]
fn is_exec(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    p.metadata()
        .map(|md| md.is_file() && md.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}
#[cfg(not(unix))]
fn is_exec(p: &Path) -> bool {
    p.is_file()
}

/// Run a command with a timeout, capturing stdout/stderr. `Ok(None)` on timeout.
fn run_timeout(cmd: &mut Command, timeout: Duration) -> io::Result<Option<std::process::Output>> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let t0 = Instant::now();
    loop {
        if child.try_wait()?.is_some() {
            return child.wait_with_output().map(Some);
        }
        if t0.elapsed() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Ok(None);
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

/// The optional flags a frame_tx/frame_rx build understands (probed from its usage
/// text, exactly like `dcf.transport.hydra_tool_caps`).
fn tool_caps(tool: &Path) -> HashSet<&'static str> {
    let usage_text = match run_timeout(&mut Command::new(tool), Duration::from_secs(10)) {
        Ok(Some(o)) => format!(
            "{}{}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        ),
        _ => String::new(),
    };
    ["--profile", "--interleave", "--preamble"]
        .into_iter()
        .filter(|f| usage_text.contains(f))
        .collect()
}

/// Python `str(v)` after `int(v) if float(v).is_integer()`.
fn py_num(v: f64) -> String {
    if v.is_finite() && v.fract() == 0.0 && v.abs() < 1e18 {
        format!("{}", v as i64)
    } else {
        format!("{}", v)
    }
}

/// Validate the hydra: URI and build the tool invocation (mirrors `dcf.medium.make_transport`
/// + `dcf.transport.HydraTransport`): usage errors first, then tool discovery (exit 3).
fn hydra_tool(u: &Uri) -> Res<HydraTool> {
    let fec = choice(
        u.g("fec").unwrap_or("conv"),
        "fec",
        &["none", "rep3", "conv"],
    )?
    .to_string();
    let profile = choice(
        u.g("profile").unwrap_or("default"),
        "profile",
        &["default", "aux"],
    )?
    .to_string();
    let interleave = match u.g("interleave") {
        None => None,
        Some(v) => Some(uri_bool(v, "interleave")?),
    };
    let mut tone: Vec<(&str, String)> = Vec::new();
    for (k, flag) in [
        ("base_freq", "--base-freq"),
        ("tone_spacing", "--tone-spacing"),
        ("baud", "--baud"),
    ] {
        if let Some(v) = u.g(k) {
            tone.push((flag, py_num(uri_num(v, k)?)));
        }
    }
    if let Some(v) = u.g("n_tones") {
        tone.push(("--n-tones", uri_int(v, "n_tones")?.to_string()));
    }
    let imp = choice(u.g("impl").unwrap_or("tool"), "impl", &["tool", "cffi"])?;
    if imp == "cffi" {
        return Err(Fail::Unsupported(
            "hydra:impl=cffi is the Python in-process binding; use impl=tool".into(),
        ));
    }
    let find = |key: &str, env: &str, bin: &str| -> Option<PathBuf> {
        u.g_nonempty(key)
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os(env)
                    .filter(|s| !s.is_empty())
                    .map(PathBuf::from)
            })
            .or_else(|| which(bin))
    };
    let (tx, rx) = match (
        find("tx", "HYDRA_TX", "frame_tx"),
        find("rx", "HYDRA_RX", "frame_rx"),
    ) {
        (Some(t), Some(r)) => (t, r),
        _ => {
            return Err(Fail::Unsupported(
                "HydraModem tools not found: build hydramodem/dcf-tools (build.sh) and put \
                 frame_tx/frame_rx on PATH or in $HYDRA_TX/$HYDRA_RX (or tx=/rx=)"
                    .into(),
            ))
        }
    };
    let caps: HashSet<&str> = tool_caps(&tx)
        .intersection(&tool_caps(&rx))
        .copied()
        .collect();
    let mut prof: Vec<String> = Vec::new();
    if profile == "aux" {
        if caps.contains("--profile") {
            prof.extend(["--profile".to_string(), "aux".to_string()]);
        } else {
            for s in [
                "--base-freq",
                "1200",
                "--tone-spacing",
                "1200",
                "--baud",
                "1200",
            ] {
                prof.push(s.to_string());
            }
            if caps.contains("--preamble") {
                prof.extend(["--preamble".to_string(), "16".to_string()]);
            }
        }
    }
    if interleave == Some(false) {
        if !caps.contains("--interleave") {
            return Err(Fail::Unsupported(
                "hydra: interleave=0 needs frame_tx/frame_rx with --interleave (rebuild hydramodem/dcf-tools)".into(),
            ));
        }
        prof.extend(["--interleave".to_string(), "0".to_string()]);
    }
    for (flag, v) in tone {
        prof.push(flag.to_string());
        prof.push(v);
    }
    Ok(HydraTool {
        tx,
        rx,
        fec: format!("--{}", fec),
        prof,
    })
}

impl HydraTool {
    fn encode_file(&self, frame: &Frame17, path: &Path) -> io::Result<()> {
        let r = run_timeout(
            Command::new(&self.tx)
                .arg(m::to_hex(frame))
                .arg(path)
                .arg(&self.fec)
                .args(&self.prof),
            Duration::from_secs(60),
        )?;
        match r {
            Some(o) if o.status.success() => Ok(()),
            Some(o) => Err(io::Error::other(format!("frame_tx failed ({})", o.status))),
            None => Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "frame_tx timed out",
            )),
        }
    }

    fn decode_file(&self, path: &Path) -> Option<Frame17> {
        let o = run_timeout(
            Command::new(&self.rx)
                .arg(path)
                .arg(&self.fec)
                .args(&self.prof),
            Duration::from_secs(60),
        )
        .ok()??;
        if !o.status.success() {
            return None;
        }
        let s = String::from_utf8_lossy(&o.stdout);
        let b = m::from_hex(s.trim())?;
        b.try_into().ok()
    }
}

fn mkdirs(u: &Uri) -> io::Result<()> {
    for k in ["in", "out"] {
        if let Some(d) = u.g_nonempty(k) {
            std::fs::create_dir_all(d)?;
        }
    }
    Ok(())
}

// ── readers ───────────────────────────────────────────────────────────────────
const CHUNK: usize = 65536;
const DIR_POLL: Duration = Duration::from_millis(100);
const FILE_POLL: Duration = Duration::from_millis(100);
const UDP_WAIT: Duration = Duration::from_millis(50);

enum Src {
    /// finite .dcf stream (file without follow, or stdin)
    Stream {
        rd: Box<dyn Read>,
        sc: m::StreamScanner,
    },
    /// finite hex lines (file without follow, or stdin)
    Hex { rd: Box<dyn BufRead> },
    /// file:follow=1 — tail a .dcf spool
    FileFollow {
        path: PathBuf,
        pos: u64,
        sc: m::StreamScanner,
        next: Instant,
    },
    /// hex:follow=1 with a path — tail whole lines
    HexFollow {
        path: PathBuf,
        pos: u64,
        carry: Vec<u8>,
        next: Instant,
    },
    /// hex:follow=1 on stdin — line by line until EOF
    HexStdin { rd: Box<dyn BufRead> },
    Udp {
        sock: UdpSocket,
        proto: bool,
        blocking: Option<Duration>,
    },
    Hydra {
        dir: PathBuf,
        seen: HashSet<String>,
        tool: HydraTool,
        next: Instant,
    },
}

struct Reader {
    src: Src,
    finite: bool,
    skipped_bytes: u64,
    bad_lines: u64,
    invalid: u64,
    eof: bool,
}

fn unsupported_media(u: &Uri, dir: &str) -> Res<()> {
    match u.scheme.as_str() {
        "mc" => {
            return Err(Fail::Unsupported(
                "mc: a Minecraft world's register is a Python-only medium (punctim mc); use python/punctim.py"
                    .to_string(),
            ));
        }
        "afsk" | "audio" | "sdr" | "janus" => {
            if u.g_nonempty(dir).is_none() {
                return usage(format!(
                    "{} as {} needs {}=<dir>",
                    u.scheme,
                    if dir == "in" { "input" } else { "output" },
                    dir
                ));
            }
            Err(Fail::Unsupported(format!(
                "{}: is a Python-runtime medium (numpy modem / janus-c); use python/punctim.py",
                u.scheme
            )))
        }
        "loop" | "l2eth" => Err(Fail::Unsupported(format!(
            "{}: is an in-process / raw-socket medium of the Python runtime; not in the Rust build",
            u.scheme
        ))),
        _ => Ok(()),
    }
}

fn open_reader(spec: &str) -> Res<Reader> {
    let u = parse_uri(spec)?;
    let finite = is_finite(&u)?;
    unsupported_media(&u, "in")?;
    let now = Instant::now();
    let src = match u.scheme.as_str() {
        "stdio" => Src::Stream {
            rd: Box::new(io::stdin()),
            sc: m::StreamScanner::new(),
        },
        "file" if finite => {
            let path = match u.g_nonempty("path").or_else(|| u.g_nonempty("in")) {
                Some(p) => p,
                None => return usage("file: needs path="),
            };
            Src::Stream {
                rd: Box::new(File::open(path)?),
                sc: m::StreamScanner::new(),
            }
        }
        "hex" if finite => match u.g_nonempty("path") {
            Some(p) => Src::Hex {
                rd: Box::new(BufReader::new(File::open(p)?)),
            },
            None => Src::Hex {
                rd: Box::new(BufReader::new(io::stdin())),
            },
        },
        "file" => {
            let mode = choice(u.g("mode").unwrap_or("r"), "mode", &["r", "w", "rw"])?;
            let path = u.g_nonempty("in").or(if mode.contains('r') {
                u.g_nonempty("path")
            } else {
                None
            });
            match path {
                Some(p) => Src::FileFollow {
                    path: p.into(),
                    pos: 0,
                    sc: m::StreamScanner::new(),
                    next: now,
                },
                None => return usage("file: needs path= (or in=/out=)"),
            }
        }
        "hex" => {
            choice(u.g("mode").unwrap_or("r"), "mode", &["r", "w", "rw"])?;
            match u.g_nonempty("path") {
                Some(p) => Src::HexFollow {
                    path: p.into(),
                    pos: 0,
                    carry: Vec::new(),
                    next: now,
                },
                None => Src::HexStdin {
                    rd: Box::new(BufReader::new(io::stdin())),
                },
            }
        }
        "udp" => {
            let bind = match u.g("bind") {
                Some(b) => hostport(b, "bind")?,
                None => return usage("udp as input needs bind=host:port"),
            };
            let proto = choice(
                u.g("dialect").unwrap_or("proto"),
                "dialect",
                &["proto", "bare"],
            )? == "proto";
            // validate the output-side keys too (the Python factory parses them all)
            udp_common(&u)?;
            let sock = UdpSocket::bind(resolve(&bind)?)?;
            Src::Udp {
                sock,
                proto,
                blocking: None,
            }
        }
        "hydra" => {
            if u.g_nonempty("in").is_none() {
                return usage("hydra as input needs in=<dir>");
            }
            let tool = hydra_tool(&u)?;
            mkdirs(&u)?;
            Src::Hydra {
                dir: u.g("in").unwrap().into(),
                seen: HashSet::new(),
                tool,
                next: now,
            }
        }
        s => {
            return Err(Fail::Unsupported(format!(
                "{}: not supported by this build",
                s
            )))
        }
    };
    Ok(Reader {
        src,
        finite,
        skipped_bytes: 0,
        bad_lines: 0,
        invalid: 0,
        eof: false,
    })
}

impl Reader {
    /// Finite media: read the next chunk (stream) or line (hex); `false` at EOF.
    fn read_finite(&mut self, out: &mut Vec<Frame17>) -> io::Result<bool> {
        match &mut self.src {
            Src::Stream { rd, sc } => {
                let mut buf = vec![0u8; CHUNK];
                let n = loop {
                    match rd.read(&mut buf) {
                        Ok(n) => break n,
                        Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                        Err(e) => return Err(e),
                    }
                };
                if n == 0 {
                    sc.flush();
                    return Ok(false);
                }
                out.extend(sc.feed(&buf[..n]));
                self.skipped_bytes = sc.skipped_bytes() as u64;
                Ok(true)
            }
            Src::Hex { rd } => {
                let mut line = Vec::new();
                if rd.read_until(b'\n', &mut line)? == 0 {
                    return Ok(false);
                }
                let (fr, bad) = m::hex_decode_bytes(&line);
                self.bad_lines += bad as u64;
                out.extend(fr);
                Ok(true)
            }
            _ => Ok(false),
        }
    }

    /// Infinite media: collect what arrives within `wait` (zero = non-blocking).
    fn poll(&mut self, wait: Duration, out: &mut Vec<Frame17>) -> io::Result<()> {
        let now = Instant::now();
        match &mut self.src {
            Src::Udp {
                sock,
                proto,
                blocking,
            } => {
                let mut buf = vec![0u8; 65536];
                let mut first = !wait.is_zero();
                for _ in 0..1024 {
                    let want = if first { Some(wait) } else { None };
                    if *blocking != want {
                        match want {
                            Some(w) => {
                                sock.set_nonblocking(false)?;
                                sock.set_read_timeout(Some(w))?;
                            }
                            None => sock.set_nonblocking(true)?,
                        }
                        *blocking = want;
                    }
                    first = false;
                    let n = match sock.recv_from(&mut buf) {
                        Ok((n, _)) => n,
                        Err(e)
                            if matches!(
                                e.kind(),
                                io::ErrorKind::WouldBlock
                                    | io::ErrorKind::TimedOut
                                    | io::ErrorKind::Interrupted
                            ) =>
                        {
                            break
                        }
                        // ICMP port-unreachable echoes etc. on some stacks: not fatal
                        Err(e) if e.kind() == io::ErrorKind::ConnectionRefused => continue,
                        Err(e) => return Err(e),
                    };
                    let dg = &buf[..n];
                    if *proto {
                        match m::proto_decode(dg) {
                            Err(_) => self.invalid += 1,
                            Ok((t, _, _, _)) if t != m::MSG_FRAME => {} // adapter envelope
                            Ok(_) => match m::proto_frame_decode(dg) {
                                Some(f) => out.push(f),
                                None => self.invalid += 1,
                            },
                        }
                    } else {
                        let fr = m::bare_decode(dg);
                        if fr.is_empty() {
                            self.invalid += 1;
                        }
                        out.extend(fr);
                    }
                }
            }
            Src::FileFollow {
                path,
                pos,
                sc,
                next,
            } => {
                if now < *next {
                    sleep_until(*next, wait);
                    return Ok(());
                }
                *next = now + FILE_POLL;
                if let Ok(mut f) = File::open(&path) {
                    f.seek(SeekFrom::Start(*pos))?;
                    let mut data = Vec::new();
                    f.read_to_end(&mut data)?;
                    *pos += data.len() as u64;
                    out.extend(sc.feed(&data));
                    self.skipped_bytes = sc.skipped_bytes() as u64;
                }
            }
            Src::HexFollow {
                path,
                pos,
                carry,
                next,
            } => {
                if now < *next {
                    sleep_until(*next, wait);
                    return Ok(());
                }
                *next = now + FILE_POLL;
                if let Ok(mut f) = File::open(&path) {
                    f.seek(SeekFrom::Start(*pos))?;
                    let mut data = Vec::new();
                    f.read_to_end(&mut data)?;
                    *pos += data.len() as u64;
                    carry.extend_from_slice(&data);
                    if let Some(cut) = carry.iter().rposition(|&c| c == b'\n') {
                        let (fr, bad) = m::hex_decode_bytes(&carry[..=cut]);
                        self.bad_lines += bad as u64;
                        out.extend(fr);
                        carry.drain(..=cut);
                    }
                }
            }
            Src::HexStdin { rd } => {
                let mut line = Vec::new();
                if rd.read_until(b'\n', &mut line)? == 0 {
                    self.eof = true;
                } else {
                    let (fr, bad) = m::hex_decode_bytes(&line);
                    self.bad_lines += bad as u64;
                    out.extend(fr);
                }
            }
            Src::Hydra {
                dir,
                seen,
                tool,
                next,
            } => {
                if now < *next {
                    sleep_until(*next, wait);
                    return Ok(());
                }
                *next = now + DIR_POLL;
                let mut files: Vec<String> = match std::fs::read_dir(&dir) {
                    Ok(rd) => rd
                        .filter_map(|e| e.ok())
                        .filter_map(|e| e.file_name().into_string().ok())
                        .filter(|f| f.ends_with(".wav") && !f.starts_with('.'))
                        .collect(),
                    Err(_) => Vec::new(),
                };
                files.sort();
                for f in files {
                    if !seen.insert(f.clone()) {
                        continue;
                    }
                    if let Some(fr) = tool.decode_file(&dir.join(&f)) {
                        out.push(fr);
                    }
                    if sig::stopped() {
                        break;
                    }
                }
            }
            Src::Stream { .. } | Src::Hex { .. } => {}
        }
        Ok(())
    }
}

fn sleep_until(next: Instant, wait: Duration) {
    if wait.is_zero() {
        return;
    }
    let d = next.saturating_duration_since(Instant::now()).min(wait);
    if !d.is_zero() {
        std::thread::sleep(d);
    }
}

// ── writers ───────────────────────────────────────────────────────────────────
enum Sink {
    Stream(Box<dyn Write>),
    Hex(Box<dyn Write>),
    Udp {
        sock: UdpSocket,
        peers: Vec<SocketAddr>,
        proto: bool,
        pair: bool,
        flush: Duration,
        ts_now: bool,
        seq: u32,
        pairer: m::BarePairer,
        held_at: Option<Instant>,
    },
    Hydra {
        dir: PathBuf,
        name: String,
        n: u64,
        tool: HydraTool,
    },
}

struct UdpOpts {
    proto: bool,
    pair: bool,
    flush_ms: i128,
    ts_now: bool,
    seq_start: u32,
}

fn udp_common(u: &Uri) -> Res<UdpOpts> {
    let proto = choice(
        u.g("dialect").unwrap_or("proto"),
        "dialect",
        &["proto", "bare"],
    )? == "proto";
    let pair = uri_bool(u.g("pair").unwrap_or("1"), "pair")?;
    let flush_ms = uri_int(u.g("flush_ms").unwrap_or("20"), "flush_ms")?;
    let ts_now = choice(u.g("ts").unwrap_or("0"), "ts", &["0", "now"])? == "now";
    let seq_start = (uri_int(u.g("seq_start").unwrap_or("1"), "seq_start")? & 0xFFFF_FFFF) as u32;
    for p in multi(u.g("peer").unwrap_or("")) {
        hostport(p.rsplit('@').next().unwrap_or(p), "peer")?;
    }
    if let Some(b) = u.g("bind") {
        hostport(b, "bind")?;
    }
    Ok(UdpOpts {
        proto,
        pair,
        flush_ms,
        ts_now,
        seq_start,
    })
}

fn open_writer(spec: &str) -> Res<Sink> {
    let u = parse_uri(spec)?;
    unsupported_media(&u, "out")?;
    match u.scheme.as_str() {
        "stdio" => Ok(Sink::Stream(Box::new(BufWriter::new(io::stdout())))),
        "file" | "hex" => {
            let path = u.g_nonempty("path").or(if u.scheme == "file" {
                u.g_nonempty("out")
            } else {
                None
            });
            if u.scheme == "file" && path.is_none() {
                return usage("file: needs path=");
            }
            let append = uri_bool(u.g("append").unwrap_or("0"), "append")?;
            let w: Box<dyn Write> = match path {
                Some(p) => {
                    let f = if append {
                        OpenOptions::new().append(true).create(true).open(p)?
                    } else {
                        File::create(p)?
                    };
                    Box::new(BufWriter::new(f))
                }
                None => Box::new(BufWriter::new(io::stdout())),
            };
            Ok(if u.scheme == "file" {
                Sink::Stream(w)
            } else {
                Sink::Hex(w)
            })
        }
        "udp" => {
            if u.g_nonempty("peer").is_none() {
                return usage("udp as output needs peer=host:port");
            }
            let o = udp_common(&u)?;
            let bind = hostport(u.g("bind").unwrap_or("0.0.0.0:0"), "bind")?;
            let mut peers = Vec::new();
            for p in multi(u.g("peer").unwrap_or("")) {
                peers.push(resolve(&hostport(
                    p.rsplit('@').next().unwrap_or(p),
                    "peer",
                )?)?);
            }
            let sock = UdpSocket::bind(resolve(&bind)?)?;
            Ok(Sink::Udp {
                sock,
                peers,
                proto: o.proto,
                pair: o.pair,
                flush: Duration::from_millis(o.flush_ms.clamp(0, u64::MAX as i128) as u64),
                ts_now: o.ts_now,
                seq: o.seq_start,
                pairer: m::BarePairer::new(),
                held_at: None,
            })
        }
        "hydra" => {
            if u.g_nonempty("out").is_none() {
                return usage("hydra as output needs out=<dir>");
            }
            let tool = hydra_tool(&u)?;
            mkdirs(&u)?;
            let name = u.g("name").unwrap_or("hydra").to_string();
            Ok(Sink::Hydra {
                dir: u.g("out").unwrap().into(),
                name,
                n: 0,
                tool,
            })
        }
        s => Err(Fail::Unsupported(format!(
            "{}: not supported by this build",
            s
        ))),
    }
}

fn now_micros() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as u64)
        .unwrap_or(0)
}

impl Sink {
    fn send_all(sock: &UdpSocket, peers: &[SocketAddr], dg: &[u8]) -> io::Result<()> {
        for p in peers {
            sock.send_to(dg, p)?;
        }
        Ok(())
    }

    fn write(&mut self, f: &Frame17) -> io::Result<()> {
        match self {
            Sink::Stream(w) => w.write_all(f),
            Sink::Hex(w) => {
                let mut line = m::to_hex(f).into_bytes();
                line.push(b'\n');
                w.write_all(&line)
            }
            Sink::Udp {
                sock,
                peers,
                proto,
                pair,
                ts_now,
                seq,
                pairer,
                held_at,
                ..
            } => {
                if *proto {
                    let ts = if *ts_now { now_micros() } else { 0 };
                    let dg = m::proto_frame_encode(f, *seq, ts);
                    *seq = seq.wrapping_add(1);
                    return Self::send_all(sock, peers, &dg);
                }
                if !*pair {
                    return Self::send_all(sock, peers, f);
                }
                for dg in pairer.push(f) {
                    Self::send_all(sock, peers, &dg)?;
                }
                *held_at = if pairer.has_pending() {
                    Some(held_at.unwrap_or_else(Instant::now))
                } else {
                    None
                };
                Ok(())
            }
            Sink::Hydra { dir, name, n, tool } => {
                *n += 1;
                let tmp = dir.join(format!(".{}-{}.wav.tmp", name, n));
                let fin = dir.join(format!("{}-{:08}.wav", name, n));
                tool.encode_file(f, &tmp)?;
                std::fs::rename(&tmp, &fin)
            }
        }
    }

    fn has_pending(&self) -> bool {
        matches!(
            self,
            Sink::Udp {
                held_at: Some(_),
                ..
            }
        )
    }

    /// Idle hook: release a lone bare frame after `flush_ms`; push buffered bytes out.
    fn poll(&mut self) -> io::Result<()> {
        match self {
            Sink::Udp {
                sock,
                peers,
                flush,
                pairer,
                held_at,
                ..
            } => {
                if let Some(t) = *held_at {
                    if t.elapsed() >= *flush {
                        for dg in pairer.flush() {
                            Self::send_all(sock, peers, &dg)?;
                        }
                        *held_at = None;
                    }
                }
                Ok(())
            }
            Sink::Stream(w) | Sink::Hex(w) => w.flush(),
            Sink::Hydra { .. } => Ok(()),
        }
    }

    fn close(&mut self) -> io::Result<()> {
        match self {
            Sink::Udp {
                sock,
                peers,
                pairer,
                held_at,
                ..
            } => {
                for dg in pairer.flush() {
                    Self::send_all(sock, peers, &dg)?;
                }
                *held_at = None;
                Ok(())
            }
            Sink::Stream(w) | Sink::Hex(w) => w.flush(),
            Sink::Hydra { .. } => Ok(()),
        }
    }
}

// ── the pipeline ──────────────────────────────────────────────────────────────
#[derive(Default)]
struct Stats {
    frames_in: u64,
    frames_out: u64,
    invalid_frames: u64,
    dropped: u64,
}

struct IoArgs {
    inp: String,
    out: String,
    count: Option<u64>,
    seconds: Option<f64>,
    expect: Option<u64>,
    validate: bool,
    queue: usize,
}

/// The single-threaded ordered pipeline reader -> frame gate -> writer
/// (`python/dcf/medium.py:run_io`). Returns the stats JSON line.
fn run_io(a: &IoArgs) -> Res<(Stats, String)> {
    let t0 = Instant::now();
    let mut rd = open_reader(&a.inp)?;
    let mut wr = open_writer(&a.out)?;
    let mut st = Stats::default();
    let limit = a.count.or(if rd.finite { None } else { a.expect });
    let deadline = a.seconds.map(|s| {
        if s <= 0.0 {
            t0
        } else {
            t0 + Duration::from_secs_f64(s.min(1e9))
        }
    });
    let done = |st: &Stats| limit.map_or(false, |l| st.frames_out >= l);
    let expired = || deadline.map_or(false, |d| Instant::now() >= d) || sig::stopped();

    let handle = |f: &Frame17, st: &mut Stats, wr: &mut Sink| -> io::Result<()> {
        st.frames_in += 1;
        if a.validate && !m::gate(f) {
            st.invalid_frames += 1;
            return Ok(());
        }
        wr.write(f)?;
        st.frames_out += 1;
        Ok(())
    };

    let result: io::Result<()> = (|| {
        let mut batch = Vec::new();
        if rd.finite {
            'outer: while !done(&st) {
                batch.clear();
                if !rd.read_finite(&mut batch)? {
                    break;
                }
                for f in &batch {
                    handle(f, &mut st, &mut wr)?;
                    if done(&st) || expired() {
                        break 'outer;
                    }
                }
                if expired() {
                    break;
                }
            }
        } else {
            let mut q: VecDeque<Frame17> = VecDeque::new();
            let cap = a.queue.max(1);
            while !done(&st) && !rd.eof {
                let wait = if !q.is_empty() {
                    Duration::ZERO
                } else if wr.has_pending() {
                    Duration::from_millis(5)
                } else {
                    UDP_WAIT
                };
                batch.clear();
                rd.poll(wait, &mut batch)?;
                for f in batch.drain(..) {
                    q.push_back(f);
                    if q.len() > cap {
                        q.pop_front();
                        st.dropped += 1;
                    }
                }
                match q.pop_front() {
                    Some(f) => handle(&f, &mut st, &mut wr)?,
                    None => {
                        wr.poll()?;
                        if expired() {
                            break;
                        }
                    }
                }
                if expired() {
                    break;
                }
            }
            // frames already received are not dropped
            while !done(&st) {
                match q.pop_front() {
                    Some(f) => handle(&f, &mut st, &mut wr)?,
                    None => break,
                }
            }
        }
        Ok(())
    })();
    let closed = wr.close();
    result?;
    closed?;
    let secs = t0.elapsed().as_secs_f64();
    let line = json_obj(&[
        ("in", J::S(a.inp.clone())),
        ("out", J::S(a.out.clone())),
        ("frames_in", J::I(st.frames_in.into())),
        ("frames_out", J::I(st.frames_out.into())),
        (
            "invalid_frames",
            J::I((st.invalid_frames + rd.invalid).into()),
        ),
        ("skipped_bytes", J::I(rd.skipped_bytes.into())),
        ("bad_lines", J::I(rd.bad_lines.into())),
        ("dropped", J::I(st.dropped.into())),
        ("seconds", J::Raw(json_secs(secs))),
    ]);
    Ok((st, line))
}

fn cmd_io(argv: &[String]) -> Res<i32> {
    let o = Opts::parse(
        argv,
        &["in", "out", "count", "seconds", "expect", "queue"],
        &["no-validate", "stats"],
        &[],
    )?;
    if let Some(p) = o.pos.first() {
        return usage(format!("unrecognized arguments: {}", p));
    }
    let (inp, out) = match (o.get("in"), o.get("out")) {
        (Some(i), Some(w)) => (i.to_string(), w.to_string()),
        (None, _) => return usage("the following arguments are required: --in"),
        (_, None) => return usage("the following arguments are required: --out"),
    };
    let seconds = match o.get("seconds") {
        None => None,
        Some(s) => match s.trim().parse::<f64>() {
            Ok(v) if !v.is_nan() => Some(v),
            _ => return usage(format!("argument --seconds: invalid float value: '{}'", s)),
        },
    };
    let queue = match nonneg_arg(&o, "queue")? {
        None => 256,
        Some(0) => return usage("argument --queue: must be >= 1"),
        Some(q) => q.min(1 << 24) as usize,
    };
    let args = IoArgs {
        inp,
        out,
        count: nonneg_arg(&o, "count")?,
        seconds,
        expect: nonneg_arg(&o, "expect")?,
        validate: !o.has("no-validate"),
        queue,
    };
    let (st, line) = match run_io(&args) {
        Ok(x) => x,
        Err(Fail::Usage(msg)) => {
            eprintln!("punctim io: {}", msg);
            return Ok(EXIT_USAGE);
        }
        Err(Fail::Unsupported(msg)) => {
            eprintln!("punctim io: medium unsupported: {}", msg);
            return Ok(EXIT_UNSUPPORTED);
        }
        Err(Fail::Io(e)) => {
            if e.kind() != io::ErrorKind::BrokenPipe {
                eprintln!("punctim io: I/O error: {}", e);
            }
            return Ok(EXIT_IO);
        }
    };
    if o.has("stats") {
        eprintln!("{}", line);
    }
    if let Some(n) = args.expect {
        if st.frames_out != n {
            eprintln!("punctim io: expected {} frames, wrote {}", n, st.frames_out);
            return Ok(EXIT_EXPECT);
        }
    }
    Ok(EXIT_OK)
}

// ══ certify ═══════════════════════════════════════════════════════════════════
fn find_vectors(explicit: Option<&str>) -> Result<PathBuf, String> {
    let file_of = |base: &str| -> PathBuf {
        if base.ends_with(".json") {
            PathBuf::from(base)
        } else {
            Path::new(base).join("medium_vectors.json")
        }
    };
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Some(e) = explicit {
        cands.push(file_of(e));
    } else {
        if let Some(env) = std::env::var_os("PUNCTIM_VECTORS").filter(|s| !s.is_empty()) {
            cands.push(file_of(&env.to_string_lossy()));
        }
        let mut roots: Vec<PathBuf> = Vec::new();
        if let Ok(exe) = std::env::current_exe() {
            roots.extend(exe.ancestors().skip(1).take(8).map(Path::to_path_buf));
        }
        roots.push(Path::new(env!("CARGO_MANIFEST_DIR")).join(".."));
        if let Ok(cwd) = std::env::current_dir() {
            roots.extend(cwd.ancestors().take(8).map(Path::to_path_buf));
        }
        for r in &roots {
            cands.push(r.join("Documentation").join("medium_vectors.json"));
        }
        for r in &roots {
            cands.push(r.join("python").join("MCP").join("medium_vectors.json"));
        }
    }
    cands
        .into_iter()
        .find(|c| c.is_file())
        .map(|c| c.canonicalize().unwrap_or(c))
        .ok_or_else(|| {
            "medium_vectors.json not found (use --vectors DIR or $PUNCTIM_VECTORS)".to_string()
        })
}

type CertResult = Result<(), String>;

fn js<'a>(v: &'a Value, k: &str) -> Result<&'a str, String> {
    v.get(k)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string '{}'", k))
}
fn ju(v: &Value, k: &str) -> Result<u64, String> {
    v.get(k)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("missing integer '{}'", k))
}
fn jcases(fam: &Value) -> Result<&Vec<Value>, String> {
    fam.get("cases")
        .and_then(Value::as_array)
        .ok_or_else(|| "missing cases".to_string())
}
fn jbytes(v: &Value, k: &str) -> Result<Vec<u8>, String> {
    m::from_hex(js(v, k)?).ok_or_else(|| format!("bad hex in '{}'", k))
}
fn jframes(v: &Value, k: &str) -> Result<Vec<Frame17>, String> {
    v.get(k)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("missing list '{}'", k))?
        .iter()
        .map(|x| {
            x.as_str()
                .and_then(m::from_hex)
                .and_then(|b| <Frame17>::try_from(b).ok())
                .ok_or_else(|| format!("bad frame in '{}'", k))
        })
        .collect()
}
fn cname(c: &Value) -> String {
    c.get("name")
        .and_then(Value::as_str)
        .unwrap_or("?")
        .to_string()
}

fn cert_stream(fam: &Value) -> CertResult {
    for c in jcases(fam)? {
        let (buf, want) = (jbytes(c, "input")?, jframes(c, "frames")?);
        let (sk, tail) = (
            ju(c, "skipped_bytes")? as usize,
            ju(c, "tail_bytes")? as usize,
        );
        if m::stream_decode(&buf) != (want.clone(), sk, tail) {
            return Err(format!("{}: stream_decode mismatch", cname(c)));
        }
        for step in [1usize, 7, 16, 17, 64] {
            let mut sc = m::StreamScanner::new();
            let mut fr = Vec::new();
            for ch in buf.chunks(step) {
                fr.extend(sc.feed(ch));
            }
            if fr != want || sc.skipped_bytes() != sk || sc.flush().len() != tail {
                return Err(format!(
                    "{}: StreamScanner mismatch at chunk {}",
                    cname(c),
                    step
                ));
            }
        }
        if m::stream_decode(&m::stream_encode(&want)) != (want.clone(), 0, 0) {
            return Err(format!("{}: stream_encode round trip", cname(c)));
        }
    }
    Ok(())
}

fn cert_hex(fam: &Value) -> CertResult {
    for c in jcases(fam)? {
        if m::hex_encode(&jframes(c, "frames")?) != js(c, "text")? {
            return Err(format!("{}: hex_encode mismatch", cname(c)));
        }
        let (fr, bad) = m::hex_decode(js(c, "decode_input")?);
        if fr != jframes(c, "decoded")? || bad as u64 != ju(c, "bad_lines")? {
            return Err(format!("{}: hex_decode mismatch", cname(c)));
        }
    }
    Ok(())
}

fn cert_proto(fam: &Value) -> CertResult {
    let types = fam
        .get("types")
        .and_then(Value::as_object)
        .ok_or("missing types")?;
    let reg_ok = types.len() == m::MSG_TYPES.len()
        && m::MSG_TYPES
            .iter()
            .all(|(n, id)| types.get(*n).and_then(Value::as_u64) == Some(u64::from(*id)));
    if ju(fam, "header_len")? != m::PROTO_HEADER_LEN as u64
        || ju(fam, "msg_frame")? != u64::from(m::MSG_FRAME)
        || !reg_ok
    {
        return Err("type registry / header mismatch".into());
    }
    for c in jcases(fam)? {
        let (pl, dg) = (jbytes(c, "payload")?, jbytes(c, "datagram")?);
        let (t, seq, ts) = (ju(c, "type")? as u8, ju(c, "seq")? as u32, ju(c, "ts")?);
        if u64::from_str_radix(js(c, "ts_hex")?, 16).ok() != Some(ts) {
            return Err(format!("{}: ts_hex != ts", cname(c)));
        }
        if m::proto_encode(t, seq, ts, &pl) != dg {
            return Err(format!("{}: proto_encode mismatch", cname(c)));
        }
        if m::proto_decode(&dg) != Ok((t, seq, ts, &pl[..])) {
            return Err(format!("{}: proto_decode mismatch", cname(c)));
        }
        let fr = m::proto_frame_decode(&dg);
        let accept = c
            .get("accept_as_frame")
            .and_then(Value::as_bool)
            .ok_or("missing accept_as_frame")?;
        if fr.is_some() != accept {
            return Err(format!("{}: accept_as_frame mismatch", cname(c)));
        }
        if let Some(f) = fr {
            if f[..] != pl[..] || m::proto_frame_encode(&f, seq, ts)[..] != dg[..] {
                return Err(format!("{}: proto_frame round trip", cname(c)));
            }
        }
    }
    Ok(())
}

fn cert_bare(fam: &Value) -> CertResult {
    for c in jcases(fam)? {
        let fr = jframes(c, "frames")?;
        let dgs: Vec<Vec<u8>> = c
            .get("datagrams")
            .and_then(Value::as_array)
            .ok_or("missing datagrams")?
            .iter()
            .map(|x| x.as_str().and_then(m::from_hex).ok_or("bad datagram hex"))
            .collect::<Result<_, _>>()?;
        if m::bare_encode(&fr, true) != dgs {
            return Err(format!("{}: bare_encode mismatch", cname(c)));
        }
        if dgs
            .iter()
            .flat_map(|d| m::bare_decode(d))
            .collect::<Vec<_>>()
            != fr
        {
            return Err(format!("{}: bare_decode mismatch", cname(c)));
        }
    }
    Ok(())
}

fn cert_l2(fam: &Value) -> CertResult {
    if jbytes(fam, "filler")? != m::L2_FILLER.to_vec() || ju(fam, "hdr")? != m::L2_HDR as u64 {
        return Err("filler / header mismatch".into());
    }
    for c in jcases(fam)? {
        let (fr, pl) = (jframes(c, "frames")?, jbytes(c, "payload")?);
        if m::l2_batch(&fr).ok() != Some(pl.clone()) {
            return Err(format!("{}: l2_batch mismatch", cname(c)));
        }
        if m::l2_unbatch(&pl).ok() != Some(fr) {
            return Err(format!("{}: l2_unbatch mismatch", cname(c)));
        }
    }
    Ok(())
}

fn cert_hydra(fam: &Value) -> CertResult {
    let profs = fam
        .get("profiles")
        .and_then(Value::as_object)
        .ok_or("missing profiles")?;
    for (name, u) in profs {
        let p = m::HydraProfile::named(name).ok_or_else(|| format!("profile {} mismatch", name))?;
        let f = |k: &str| u.get(k).and_then(Value::as_f64);
        let ok = f("sample_rate") == Some(p.sample_rate)
            && f("baud") == Some(p.baud)
            && f("n_tones") == Some(f64::from(p.n_tones))
            && f("base_freq") == Some(p.base_freq)
            && f("tone_spacing") == Some(p.tone_spacing)
            && f("preamble_syms") == Some(f64::from(p.preamble_syms))
            && f("sync_word") == Some(f64::from(p.sync_word))
            && f("fec_mode") == Some(f64::from(p.fec as u8))
            && f("interleave") == Some(f64::from(u8::from(p.interleave)))
            && f("tx_gain") == Some(p.tx_gain)
            && u.as_object().map_or(0, |o| o.len()) == 10;
        if !ok {
            return Err(format!("profile {} mismatch", name));
        }
    }
    for c in jcases(fam)? {
        let mut p = m::HydraProfile::named(js(c, "profile")?)
            .ok_or_else(|| format!("{}: unknown profile", cname(c)))?;
        p.fec = m::HydraFec::from_name(js(c, "fec")?)
            .ok_or_else(|| format!("{}: unknown fec", cname(c)))?;
        p.interleave = ju(c, "interleave")? != 0;
        p.n_tones = ju(c, "n_tones")? as u32;
        p.init().map_err(|e| format!("{}: {}", cname(c), e))?;
        if (
            p.coded_bits as u64,
            p.interleave_stride as u64,
            p.total_syms as u64,
        ) != (
            ju(c, "coded_bits")?,
            ju(c, "interleave_stride")?,
            ju(c, "total_syms")?,
        ) {
            return Err(format!("{}: derived profile fields mismatch", cname(c)));
        }
        let fr: Frame17 = jbytes(c, "frame")?
            .try_into()
            .map_err(|_| "bad frame".to_string())?;
        let syms = js(c, "symbols")?;
        if m::hydra_symbols_encode(&p, &fr).ok().as_deref() != Some(syms) {
            return Err(format!("{}: symbols mismatch", cname(c)));
        }
        if m::hydra_symbols_decode(&p, syms) != Some(fr) {
            return Err(format!("{}: decode mismatch", cname(c)));
        }
    }
    Ok(())
}

fn cert_afsk(fam: &Value) -> CertResult {
    let profs = fam
        .get("profiles")
        .and_then(Value::as_object)
        .ok_or("missing profiles")?;
    if profs.len() != m::AFSK_PROFILES.len() {
        return Err("profiles mismatch".into());
    }
    for p in m::AFSK_PROFILES {
        let u = profs.get(p.name).ok_or("profiles mismatch")?;
        let f = |k: &str| u.get(k).and_then(Value::as_f64);
        if f("mark") != Some(p.mark)
            || f("space") != Some(p.space)
            || f("baud") != Some(f64::from(p.baud))
            || f("preamble_bits") != Some(p.preamble_bits as f64)
        {
            return Err("profiles mismatch".into());
        }
    }
    for c in jcases(fam)? {
        let fr: Frame17 = jbytes(c, "frame")?
            .try_into()
            .map_err(|_| "bad frame".to_string())?;
        let prof = js(c, "profile")?;
        let fec = c.get("fec").and_then(Value::as_bool).ok_or("missing fec")?;
        let b = m::afsk_bits_encode(&fr, prof, fec).map_err(|e| format!("{}: {}", cname(c), e))?;
        if b != js(c, "bits")? || b.len() as u64 != ju(c, "n_bits")? {
            return Err(format!("{}: bits mismatch", cname(c)));
        }
        if m::afsk_bits_decode(&b, prof, fec) != Some(fr) {
            return Err(format!("{}: decode mismatch", cname(c)));
        }
    }
    Ok(())
}

fn cert_anchors(v: &Value) -> CertResult {
    let a = v.get("anchors").ok_or("missing anchors")?;
    let checks = [
        (
            u64::from(crc16_ccitt(b"123456789")),
            ju(a, "crc_123456789")?,
        ),
        (u64::from(crc16_ccitt(&[0u8; 15])), ju(a, "crc_zero15")?),
        (17, ju(a, "frame_len")?),
        (m::PROTO_HEADER_LEN as u64, ju(a, "proto_header_len")?),
        (u64::from(m::MSG_FRAME), ju(a, "msg_frame")?),
        (32, ju(a, "super_len")?),
        (m::L2_HDR as u64, ju(a, "l2_hdr")?),
        (u64::from(m::HYDRA_SYNC_WORD), ju(a, "hydra_sync_word")?),
        (u64::from(m::AFSK_SYNC), ju(a, "afsk_sync")?),
        (
            u64::from(m::crc8_afsk(b"123456789")),
            ju(a, "afsk_crc8_123456789")?,
        ),
    ];
    if checks.iter().any(|(x, y)| x != y) || js(a, "l2_filler")? != m::to_hex(&m::L2_FILLER) {
        return Err("anchor mismatch".into());
    }
    for b in v
        .get("basis")
        .and_then(Value::as_array)
        .map(|x| x.as_slice())
        .unwrap_or(&[])
    {
        let p = jbytes(b, "payload")?;
        if p.len() != 4 {
            return Err("basis payload".into());
        }
        let f = encode_raw(
            ju(b, "type")? as u8,
            ju(b, "seq")? as u16,
            ju(b, "src")? as u16,
            ju(b, "dst")? as u16,
            [p[0], p[1], p[2], p[3]],
            ju(b, "ts")? as u32 & 0xFF_FFFF,
        );
        if m::to_hex(&f) != js(b, "hex")? {
            return Err(format!("basis {} mismatch", ju(b, "id").unwrap_or(0)));
        }
    }
    Ok(())
}

fn cmd_certify(argv: &[String]) -> Res<i32> {
    let o = Opts::parse(argv, &["vectors"], &[], &["family"])?;
    if let Some(p) = o.pos.first() {
        return usage(format!("unrecognized arguments: {}", p));
    }
    let path = match find_vectors(o.get("vectors")) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("punctim certify: {}", e);
            return Ok(EXIT_IO);
        }
    };
    let vec: Value = match std::fs::read_to_string(&path)
        .map_err(|e| e.to_string())
        .and_then(|s| serde_json::from_str(&s).map_err(|e| e.to_string()))
    {
        Ok(v) => v,
        Err(e) => {
            eprintln!("punctim certify: {}: {}", path.display(), e);
            return Ok(EXIT_IO);
        }
    };
    let fams: Vec<String> = match o.vals.get("family") {
        Some(v) => v.clone(),
        None => FAMILIES.iter().map(|s| s.to_string()).collect(),
    };
    for f in &fams {
        if !FAMILIES.contains(&f.as_str()) {
            eprintln!(
                "punctim certify: unknown family '{}' (one of: {})",
                f,
                FAMILIES.join(", ")
            );
            return Ok(EXIT_USAGE);
        }
    }
    println!("vectors: {}", path.display());
    let nbasis = vec
        .get("basis")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    let mut failed = 0;
    let mut total = 0;
    match cert_anchors(&vec) {
        Ok(()) => println!("PASS anchors ({} basis frames)", nbasis),
        Err(e) => {
            failed += 1;
            println!("FAIL anchors: {}", e);
        }
    }
    for name in &fams {
        let fam = vec.get("families").and_then(|f| f.get(name.as_str()));
        let (n, r) = match fam {
            None => (0, Err("family missing from vectors".to_string())),
            Some(fam) => {
                let n = fam
                    .get("cases")
                    .and_then(Value::as_array)
                    .map_or(0, Vec::len);
                let r = match name.as_str() {
                    "stream" => cert_stream(fam),
                    "hex" => cert_hex(fam),
                    "udp_proto" => cert_proto(fam),
                    "udp_bare" => cert_bare(fam),
                    "l2eth" => cert_l2(fam),
                    "hydra_symbols" => cert_hydra(fam),
                    _ => cert_afsk(fam),
                };
                (n, r)
            }
        };
        total += n;
        match r {
            Ok(()) => println!("PASS {} ({} cases)", name, n),
            Err(e) => {
                failed += 1;
                println!("FAIL {}: {}", name, e);
            }
        }
    }
    if failed > 0 {
        println!(
            "CERTIFICATION FAILED ({} famil{})",
            failed,
            if failed == 1 { "y" } else { "ies" }
        );
        return Ok(EXIT_CERT);
    }
    println!("ALL MEDIUM VECTORS PASS ({} cases, impl {})", total, IMPL);
    Ok(EXIT_OK)
}

// ── main ──────────────────────────────────────────────────────────────────────
fn main() {
    sig::install();
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let cmd = argv.first().map(String::as_str).unwrap_or("");
    let rest = if argv.is_empty() {
        &argv[..]
    } else {
        &argv[1..]
    };
    let r = match cmd {
        "version" => cmd_version(rest),
        "io" => cmd_io(rest),
        "encode" => cmd_encode(rest),
        "decode" => cmd_decode(rest),
        "certify" => cmd_certify(rest),
        "-h" | "--help" | "help" => {
            print!("{}", HELP);
            Ok(EXIT_OK)
        }
        "" => {
            eprint!("{}", HELP);
            eprintln!("punctim: error: the following arguments are required: command");
            Ok(EXIT_USAGE)
        }
        "sim" => {
            eprintln!("punctim sim: Python only (python3 python/punctim.py sim ...)");
            Ok(EXIT_USAGE)
        }
        other => {
            eprintln!("punctim: error: invalid choice: '{}' (choose from version, io, encode, decode, certify)", other);
            Ok(EXIT_USAGE)
        }
    };
    let code = match r {
        Ok(c) => c,
        Err(Fail::Usage(msg)) => {
            eprintln!("punctim {}: {}", cmd, msg);
            EXIT_USAGE
        }
        Err(Fail::Unsupported(msg)) => {
            eprintln!("punctim {}: medium unsupported: {}", cmd, msg);
            EXIT_UNSUPPORTED
        }
        Err(Fail::Io(e)) => {
            if e.kind() != io::ErrorKind::BrokenPipe {
                eprintln!("punctim {}: I/O error: {}", cmd, e);
            }
            EXIT_IO
        }
    };
    let _ = io::stdout().flush();
    std::process::exit(code);
}
