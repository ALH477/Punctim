// SPDX-License-Identifier: LGPL-3.0-only
//!
//! DCF-SPA — single-packet port authorization for Punctim.
//!
//! Authentication only: the token proves a device's identity (via HMAC-SHA256
//! or Ed25519 over a plaintext, nonce'd, timestamped header) so the authorizer
//! can open a mesh port for that source address. It performs **no** cryptography
//! for data confidentiality and exchanges **no** session keys — that boundary is
//! what keeps it EAR99 (see `Documentation/DCF_SPA_SPEC.md` §3). Do not add
//! confidentiality or key exchange here.
//!
//! This module is the reference authorizer's core: the token wire format (§5),
//! constant-time verification, and the freshness + replay lifecycle (§6). The
//! firewall side effect is abstracted behind [`Granter`] so the decision logic
//! is testable without root or nftables.

use std::collections::HashMap;
use std::net::Ipv4Addr;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use ed25519_dalek::{Signature, VerifyingKey};
use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Token magic byte, `'S'`.
pub const MAGIC: u8 = 0x53;
/// Token version.
pub const VERSION: u8 = 0x01;
/// Header length in bytes; the tag covers exactly these bytes.
pub const HDR_LEN: usize = 31;
/// Total length of an HMAC-SHA256 token (header + 32-byte tag).
pub const LEN_HMAC: usize = HDR_LEN + 32; // 63
/// Total length of an Ed25519 token (header + 64-byte signature).
pub const LEN_ED25519: usize = HDR_LEN + 64; // 95

/// A per-device credential the authorizer holds.
#[derive(Clone)]
pub enum Cred {
    /// Per-device pre-shared HMAC key (symmetric; the store is sensitive).
    Hmac([u8; 32]),
    /// Per-device Ed25519 public key (the authorizer holds no signing key).
    Ed25519(VerifyingKey),
}

/// The parsed, unauthenticated token header (§5).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub device_id: u16,
    pub timestamp_ms: u64,
    pub nonce: [u8; 16],
    pub port: u16,
    pub flags: u8,
}

impl Header {
    /// Serialize the 31-byte header (big-endian), the exact bytes the tag covers.
    pub fn to_bytes(&self) -> [u8; HDR_LEN] {
        let mut b = [0u8; HDR_LEN];
        b[0] = MAGIC;
        b[1] = VERSION;
        b[2..4].copy_from_slice(&self.device_id.to_be_bytes());
        b[4..12].copy_from_slice(&self.timestamp_ms.to_be_bytes());
        b[12..28].copy_from_slice(&self.nonce);
        b[28..30].copy_from_slice(&self.port.to_be_bytes());
        b[30] = self.flags;
        b
    }

    /// Parse a header from the front of `buf`. Returns `None` on any structural
    /// mismatch — the caller drops silently, per §6.
    pub fn parse(buf: &[u8]) -> Option<Header> {
        if buf.len() < HDR_LEN || buf[0] != MAGIC || buf[1] != VERSION {
            return None;
        }
        Some(Header {
            device_id: u16::from_be_bytes([buf[2], buf[3]]),
            timestamp_ms: u64::from_be_bytes(buf[4..12].try_into().ok()?),
            nonce: buf[12..28].try_into().ok()?,
            port: u16::from_be_bytes([buf[28], buf[29]]),
            flags: buf[30],
        })
    }
}

/// Verify the token's tag over its header, constant-time. The token length must
/// match the credential's mode exactly.
pub fn verify_tag(cred: &Cred, buf: &[u8]) -> bool {
    if buf.len() < HDR_LEN {
        return false;
    }
    let header = &buf[..HDR_LEN];
    match cred {
        Cred::Hmac(key) => {
            if buf.len() != LEN_HMAC {
                return false;
            }
            let mut mac = match HmacSha256::new_from_slice(key) {
                Ok(m) => m,
                Err(_) => return false,
            };
            mac.update(header);
            mac.verify_slice(&buf[HDR_LEN..LEN_HMAC]).is_ok() // constant-time
        }
        Cred::Ed25519(vk) => {
            if buf.len() != LEN_ED25519 {
                return false;
            }
            let sig_bytes: [u8; 64] = match buf[HDR_LEN..LEN_ED25519].try_into() {
                Ok(b) => b,
                Err(_) => return false,
            };
            let sig = Signature::from_bytes(&sig_bytes);
            vk.verify_strict(header, &sig).is_ok()
        }
    }
}

/// The abstracted firewall grant. Production installs an nftables allow-set
/// element; tests record the grants in memory.
pub trait Granter {
    /// Permit `ip` to reach the mesh port for the configured TTL.
    fn grant(&mut self, ip: Ipv4Addr, port: u16);
}

/// Why a token was rejected (all rejections are silent on the wire; this is for
/// the authorizer's own logging/metrics only).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reject {
    Malformed,
    UnknownDevice,
    BadTag,
    Stale,
    Replay,
    PolicyDenied,
}

/// The outcome of processing one datagram.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Granted { device_id: u16, port: u16 },
    Rejected(Reject),
}

/// Authorizer policy + credential state. Owns the nonce cache.
pub struct Authorizer {
    creds: HashMap<u16, Cred>,
    /// Optional per-device port allow-list (empty vec = any port).
    policy: HashMap<u16, Vec<u16>>,
    default_mesh_port: u16,
    window_ms: u64,
    seen: HashMap<(u16, [u8; 16]), Instant>,
    last_sweep: Instant,
}

impl Authorizer {
    pub fn new(default_mesh_port: u16, window_ms: u64) -> Self {
        Authorizer {
            creds: HashMap::new(),
            policy: HashMap::new(),
            default_mesh_port,
            window_ms,
            seen: HashMap::new(),
            last_sweep: Instant::now(),
        }
    }

    pub fn add_cred(&mut self, device_id: u16, cred: Cred) {
        self.creds.insert(device_id, cred);
    }

    pub fn set_policy(&mut self, device_id: u16, ports: Vec<u16>) {
        self.policy.insert(device_id, ports);
    }

    fn now_ms() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    /// Lazily drop nonce-cache entries older than `2 * window_ms`.
    fn sweep(&mut self) {
        let ttl = Duration::from_millis(2 * self.window_ms);
        if self.last_sweep.elapsed() > Duration::from_millis(self.window_ms + 1000) {
            self.seen.retain(|_, t| t.elapsed() < ttl);
            self.last_sweep = Instant::now();
        }
    }

    /// Process one datagram from `src`, granting via `granter` on full success.
    /// The nonce is recorded **only** on success, so junk allocates no state
    /// (§4 DoS defense). `now_ms_override` lets tests pin the clock.
    pub fn process(
        &mut self,
        pkt: &[u8],
        src: Ipv4Addr,
        granter: &mut dyn Granter,
        now_ms_override: Option<u64>,
    ) -> Outcome {
        self.sweep();

        let hdr = match Header::parse(pkt) {
            Some(h) => h,
            None => return Outcome::Rejected(Reject::Malformed),
        };
        let cred = match self.creds.get(&hdr.device_id) {
            Some(c) => c.clone(),
            None => return Outcome::Rejected(Reject::UnknownDevice),
        };
        if !verify_tag(&cred, pkt) {
            return Outcome::Rejected(Reject::BadTag);
        }

        let now = now_ms_override.unwrap_or_else(Self::now_ms);
        let skew = (now as i64 - hdr.timestamp_ms as i64).unsigned_abs();
        if skew > self.window_ms {
            return Outcome::Rejected(Reject::Stale);
        }

        let key = (hdr.device_id, hdr.nonce);
        if self.seen.contains_key(&key) {
            return Outcome::Rejected(Reject::Replay);
        }

        let port = if hdr.port == 0 {
            self.default_mesh_port
        } else {
            hdr.port
        };
        if let Some(allowed) = self.policy.get(&hdr.device_id) {
            if !allowed.is_empty() && !allowed.contains(&port) {
                return Outcome::Rejected(Reject::PolicyDenied);
            }
        }

        // Record only on full success.
        self.seen.insert(key, Instant::now());
        granter.grant(src, port);
        Outcome::Granted {
            device_id: hdr.device_id,
            port,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Test granter: records every grant.
    #[derive(Default)]
    struct MockGranter {
        grants: Vec<(Ipv4Addr, u16)>,
    }
    impl Granter for MockGranter {
        fn grant(&mut self, ip: Ipv4Addr, port: u16) {
            self.grants.push((ip, port));
        }
    }

    const KEY: [u8; 32] = [7u8; 32];
    const DEV: u16 = 5;
    const NOW: u64 = 1_000_000_000_000;

    fn hmac_token(hdr: &Header, key: &[u8; 32]) -> Vec<u8> {
        let h = hdr.to_bytes();
        let mut mac = HmacSha256::new_from_slice(key).unwrap();
        mac.update(&h);
        let tag = mac.finalize().into_bytes();
        let mut t = h.to_vec();
        t.extend_from_slice(&tag);
        t
    }

    fn base_header() -> Header {
        Header {
            device_id: DEV,
            timestamp_ms: NOW,
            nonce: [0x11; 16],
            port: 7100,
            flags: 0,
        }
    }

    fn auth() -> Authorizer {
        let mut a = Authorizer::new(7100, 30_000);
        a.add_cred(DEV, Cred::Hmac(KEY));
        a
    }

    #[test]
    fn valid_token_grants() {
        let mut a = auth();
        let mut g = MockGranter::default();
        let tok = hmac_token(&base_header(), &KEY);
        let out = a.process(&tok, Ipv4Addr::new(192, 0, 2, 5), &mut g, Some(NOW));
        assert_eq!(out, Outcome::Granted { device_id: DEV, port: 7100 });
        assert_eq!(g.grants, vec![(Ipv4Addr::new(192, 0, 2, 5), 7100)]);
    }

    #[test]
    fn exact_replay_is_rejected() {
        let mut a = auth();
        let mut g = MockGranter::default();
        let tok = hmac_token(&base_header(), &KEY);
        let ip = Ipv4Addr::new(192, 0, 2, 5);
        assert!(matches!(a.process(&tok, ip, &mut g, Some(NOW)), Outcome::Granted { .. }));
        // identical bytes again -> nonce seen -> no second grant
        assert_eq!(a.process(&tok, ip, &mut g, Some(NOW)), Outcome::Rejected(Reject::Replay));
        assert_eq!(g.grants.len(), 1);
    }

    #[test]
    fn single_byte_tamper_is_rejected() {
        let mut a = auth();
        let mut g = MockGranter::default();
        let mut tok = hmac_token(&base_header(), &KEY);
        tok[20] ^= 0x01; // flip a nonce byte, invalidating the tag
        assert_eq!(a.process(&tok, Ipv4Addr::new(192, 0, 2, 5), &mut g, Some(NOW)),
                   Outcome::Rejected(Reject::BadTag));
        assert!(g.grants.is_empty());
    }

    #[test]
    fn wrong_device_is_rejected() {
        let mut a = auth();
        let mut g = MockGranter::default();
        let mut hdr = base_header();
        hdr.device_id = 999; // no credential for this id
        let tok = hmac_token(&hdr, &KEY);
        assert_eq!(a.process(&tok, Ipv4Addr::new(192, 0, 2, 5), &mut g, Some(NOW)),
                   Outcome::Rejected(Reject::UnknownDevice));
        assert!(g.grants.is_empty());
    }

    #[test]
    fn stale_token_is_rejected() {
        let mut a = auth();
        let mut g = MockGranter::default();
        let tok = hmac_token(&base_header(), &KEY);
        // now is 40s past the token stamp; window is 30s
        let out = a.process(&tok, Ipv4Addr::new(192, 0, 2, 5), &mut g, Some(NOW + 40_000));
        assert_eq!(out, Outcome::Rejected(Reject::Stale));
        assert!(g.grants.is_empty());
    }

    #[test]
    fn future_token_beyond_window_is_rejected() {
        let mut a = auth();
        let mut g = MockGranter::default();
        let tok = hmac_token(&base_header(), &KEY);
        let out = a.process(&tok, Ipv4Addr::new(192, 0, 2, 5), &mut g, Some(NOW - 40_000));
        assert_eq!(out, Outcome::Rejected(Reject::Stale));
    }

    #[test]
    fn policy_denies_disallowed_port() {
        let mut a = auth();
        a.set_policy(DEV, vec![9000]); // only 9000 allowed
        let mut g = MockGranter::default();
        let tok = hmac_token(&base_header(), &KEY); // requests 7100
        assert_eq!(a.process(&tok, Ipv4Addr::new(192, 0, 2, 5), &mut g, Some(NOW)),
                   Outcome::Rejected(Reject::PolicyDenied));
    }

    #[test]
    fn header_roundtrips() {
        let h = base_header();
        assert_eq!(Header::parse(&h.to_bytes()), Some(h));
    }

    #[test]
    fn malformed_magic_rejected() {
        let mut a = auth();
        let mut g = MockGranter::default();
        let mut tok = hmac_token(&base_header(), &KEY);
        tok[0] = 0x00;
        assert_eq!(a.process(&tok, Ipv4Addr::new(192, 0, 2, 5), &mut g, Some(NOW)),
                   Outcome::Rejected(Reject::Malformed));
    }
}
