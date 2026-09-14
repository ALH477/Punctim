# DCF-SPA: single-packet port authorization for Punctim

**0.x — pre-release, design-complete, reference implementation below**
**Developed by DeMoD LLC** · **Contact:** alh477@demod.ltd
**License:** LGPL-3.0 (library), consistent with the Punctim core.

> **Scope, honestly.** DCF-SPA is a **secondary-channel authenticator** that opens
> Punctim data ports on demand for devices on a shared network. It authenticates
> and gates — it does **not** encrypt the data plane and provides **no
> confidentiality**. That boundary is deliberate: it is what keeps the feature outside
> ECCN 5A002 and lets it ship *inside* the repo without disturbing the encryption-free
> EAR posture that motivates DCF's design (see [`DCF_SECURITY_EXPOSURE.md`](DCF_SECURITY_EXPOSURE.md)).
> If you need confidentiality, it stays where that document already puts it — WireGuard
> beneath the socket, operator-supplied. **Read [§3 Export classification](#3-export-classification)
> before changing the cryptographic surface.**

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Design goals and non-goals](#2-design-goals-and-non-goals)
3. [Export classification](#3-export-classification)
4. [Threat model](#4-threat-model)
5. [The SPA token (wire format)](#5-the-spa-token-wire-format)
6. [Authorization lifecycle](#6-authorization-lifecycle)
7. [Firewall integration (nftables)](#7-firewall-integration-nftables)
8. [Reference authorizer (Rust)](#8-reference-authorizer-rust)
9. [Reference client / knock sender](#9-reference-client--knock-sender)
10. [NixOS module (ArchibaldOS / Oligarchy)](#10-nixos-module-archibaldos--oligarchy)
11. [Key management and provisioning](#11-key-management-and-provisioning)
12. [Configuration reference](#12-configuration-reference)
13. [Testing and validation](#13-testing-and-validation)
14. [Operational rules](#14-operational-rules)
15. [Appendix A — self-classification memo (fill-in)](#appendix-a--self-classification-memo-fill-in)

---

## 1. What it does

Punctim data ports default to **DROP**. A device that wants to join sends a single
authenticated UDP datagram — the **SPA token** — on a dedicated knock channel. A small
**authorizer** daemon verifies the token's signature, checks it is fresh (not a replay),
and installs a **time-limited allow rule** permitting that source address to reach the
mesh port. Established sessions are held open by connection tracking after the window
closes; only *new* peers must re-knock.

This is Single Packet Authorization (SPA), the modern successor to port knocking. It is
chosen over classic knocking because a knock *sequence* is a replayable shared secret and
costs several round trips; an SPA token is one packet, cryptographically authenticated,
and carries a nonce + timestamp so an on-path observer cannot replay it.

```
   device                        authorizer                     kernel firewall
     |                                |                                |
     |  1. SPA token (1 UDP dgram) -->|                                |
     |                                | 2. verify sig                  |
     |                                | 3. check timestamp window      |
     |                                | 4. check nonce cache           |
     |                                | 5. add saddr to allow set ---->| timeout=TTL
     |                                |                                |
     |  6. mesh traffic to :MESH_PORT ------------------------------->| accept (saddr in set)
     |                                |                                | conntrack: established
```

## 2. Design goals and non-goals

**Goals**

- Data ports invisible to a fleet scan until a valid token arrives.
- One-packet, zero-round-trip authorization on the shared segment.
- Non-replayable and unforgeable without the device credential.
- Authentication only — identity, integrity, freshness. Nothing else.
- Declarative deployment on ArchibaldOS / Oligarchy (NixOS module).
- Zero change to the DCF wire certificate — the token is a side channel, not a frame
  format change. It reuses DCF idioms (`device_id` ≙ `src_id`) but is its own datagram.

**Non-goals (do not add these here)**

- **Confidentiality of user traffic.** Out of scope, by design and by export posture.
- **Session key exchange to bootstrap an encrypted tunnel.** Explicitly forbidden — it
  drags the feature back under 5A002 (see §3).
- **Per-packet authentication of the data plane.** SPA authorizes an *address* to reach
  a port; it does not sign every subsequent packet. If you need cryptographic per-peer
  identity for the whole session, that is the beneath-socket WireGuard tunnel's job
  (§4, residual risks).

## 3. Export classification

This section is the reason the feature can live in-project. It is written so it can be
lifted directly into a self-classification record (Appendix A).

**The control.** ECCN 5A002 (15 CFR Part 774, Supp. 1, Category 5 — Part 2) controls
information-security items that use cryptography to perform functions **other than**
authentication, digital signature, or execution of copy-protected software (and the key
management associated with those). The relevant carve-outs:

- **Note (g) to 5A002** excludes equipment where the cryptographic functionality is used
  **only for authentication, digital signature, or execution of copy-protected
  software**.
- **BIS Technical Note on Authentication** defines *authentication* as verifying the
  identity of a user, process, or device — typically as a prerequisite to allowing
  access to resources in an information system — including verifying the origin or
  content of a message, with no encryption of files or text except as needed to protect
  passwords/PINs. Data integrity and anti-replay ride along in the same decontrol.

**Why DCF-SPA fits.** The token authenticates a device identity (`device_id`) as a
prerequisite to granting access to a resource (the mesh port). Its cryptography is a
**MAC (HMAC-SHA256)** or a **digital signature (Ed25519)** over a plaintext token; it
verifies origin, integrity, and freshness. It performs **no `cryptography for data
confidentiality`** in the ECCN sense — it never renders any user data unintelligible.
That places it within the authentication decontrol and outside 5A002, landing it at
**EAR99**, the same classification as the rest of Punctim.

**The one line you must not cross.** If the authenticated channel is ever made to
provide confidentiality — most tempting version: the token hands the peer a session key
to bring up an *encrypted* mesh session — the exclusion is lost. Encryption "beyond what
is needed for the auth handshake" is controlled, and key management is only decontrolled
when it supports the decontrolled functions. Key management **for authentication keys**
is fine; key exchange **to bootstrap data-plane encryption** is controlled. So:
authenticate, open the port, stop. Confidentiality remains WireGuard beneath the socket,
operator-supplied, per `DCF_SECURITY_EXPOSURE.md`.

**Standard caveats.** This is a self-classification rationale, not legal advice from
counsel. EAR99 items still cannot go to embargoed/sanctioned destinations or denied
parties. Publicly available source code has its own path under the EAR, but because this
feature is authentication-only it is not 5D002 to begin with. If the fleet ships to
sensitive destinations, confirm the self-classification with export counsel.

**Regulatory references**

- ECCN 5A002 and Notes — 15 CFR Part 774, Supplement No. 1, Category 5 — Part 2.
- BIS, *Technical Note: Authentication and other uses of encryption that are not
  controlled* — https://www.bis.gov/ (Encryption controls / Cat. 5 Part 2).
- Definition of "cryptography for data confidentiality" — 15 CFR Part 772.

## 4. Threat model

Assume the classic shared-segment adversary: on-path, can sniff every packet, can inject
and spoof, can scan the fleet.

| Attack | Defense |
|--------|---------|
| **Scan for open mesh ports** | Ports are DROP until authorized; a scan sees nothing. The knock listener never replies, so the knock port reads as filtered/closed. |
| **Sniff the token** | The token carries no secret. The HMAC key / Ed25519 private key never travels; the signature and nonce are not reusable. Sniffing yields nothing actionable. |
| **Replay a sniffed token** | Rejected: nonce cache + timestamp window. A token is valid once, within a few seconds. |
| **Forge a token** | Requires the per-device key (HMAC) or private key (Ed25519). Infeasible without compromising the device. |
| **DoS the authorizer with junk** | Verification is cheap and constant-time; invalid tokens are dropped silently with no state allocated until *after* signature + freshness pass (nonce is only recorded on success). Rate-limit the knock port (§7). |

**Residual risk — source-IP spoofing.** SPA authorizes a *source address* to reach a
port. On a shared L2, an attacker can spoof the IP of an already-authorized peer and ride
its allow rule for the rule's lifetime. Mitigations, in increasing strength:

1. Keep the allow-rule TTL short (default 30 s) — the window to ride is small.
2. Bind the allow rule to `(ip saddr, ether saddr)` on the local segment (nftables can
   match `ether saddr`), so a spoofed IP from a different MAC is not matched.
3. **Run WireGuard beneath the socket anyway.** WG authenticates every packet by peer
   key regardless of IP, so a spoofed address reaches a WG endpoint that rejects it.
   DCF-SPA then serves as a stealth/attack-surface-reduction layer in front of WG, not
   the sole gate. This is the recommended posture for hostile networks and keeps the
   confidentiality/identity crypto outside the project.

DCF-SPA reduces attack surface and hides the fleet; it is defense-in-depth, not a
replacement for a per-packet-authenticated transport where the segment is untrusted.

## 5. The SPA token (wire format)

One UDP datagram, fixed layout, big-endian. Two modes select the tag; the header is
identical. The tag is computed over the entire 31-byte header.

```
offset  size  field         notes
------  ----  ------------  ---------------------------------------------------------
  0      1    magic         0x53 ('S')
  1      1    version       0x01
  2      2    device_id     u16, identity; selects the key/pubkey. (≙ DCF src_id)
  4      8    timestamp_ms  u64, sender wall-clock, ms since Unix epoch
 12     16    nonce         random, CSPRNG
 28      2    port          u16, requested mesh port; 0 = authorizer default
 30      1    flags         bit0..: reserved (0). Do NOT repurpose for crypto modes
                            that add confidentiality.
------  ----  ------------  header = 31 bytes; tag covers bytes [0..31)
 31     32    tag (HMAC)    HMAC-SHA256(key_device, header)         [mode = hmac]
   -or-
 31     64    tag (Ed25519) Ed25519.sign(sk_device, header)         [mode = ed25519]
------
total: 63 bytes (hmac) | 95 bytes (ed25519). Well under any MTU.
```

**Mode choice**

- **HMAC-SHA256 + per-device pre-shared key** — symmetric, smallest, simplest to
  provision for a closed fleet. The authorizer holds every device's key, so protect the
  key store.
- **Ed25519 signature + per-device keypair** — the authorizer holds only *public* keys;
  compromising it leaks no signing capability, and you get non-repudiation. Preferred
  for anything past a handful of nodes. **Default recommendation.**

Both are pure authentication under §3.

**Anti-replay parameters**

- `window_ms` (default 30 000): accept only if `|now − timestamp_ms| ≤ window_ms`.
- Nonce cache: remember `(device_id, nonce)` for `2 × window_ms`; reject any repeat.
- Requires loose clock agreement across the fleet within `window_ms`. Use NTP, or the
  DCF-Snake `BEACON(2)` grandmaster media clock if the deployment already runs one, as
  the shared time reference.

## 6. Authorization lifecycle

```
receive datagram on KNOCK_PORT
  ├─ length ∉ {63, 95}                         → drop (silent)
  ├─ magic ≠ 0x53 or version ≠ 0x01            → drop (silent)
  ├─ lookup key/pubkey by device_id            → miss: drop (silent)
  ├─ verify tag over header[0..31)             → fail: drop (silent)     # constant-time
  ├─ |now − timestamp_ms| > window_ms          → drop (stale/future)
  ├─ (device_id, nonce) in cache               → drop (replay)
  ├─ record (device_id, nonce) expiry = now + 2·window_ms
  ├─ port := field.port or default_mesh_port
  ├─ policy: device_id allowed for port?       → deny: drop + log
  └─ nft add element allow_set { saddr timeout TTL }   + log grant
```

Silence on every failure is intentional: the knock channel must never emit a
distinguishable response, so a scanner cannot tell a valid device from noise, nor even
that an authorizer is listening.

## 7. Firewall integration (nftables)

Modern approach: a named set with a per-element `timeout`. The authorizer adds an
address; the kernel reaps it. No rule churn, atomic updates, maps cleanly onto declarative
NixOS config.

```nft
# /etc/nftables/punctim-spa.nft  (or generated by the NixOS module in §10)
define MESH_PORT  = 7100
define KNOCK_PORT = 62201

table inet punctim_spa {
    set allowed_peers {
        type ipv4_addr
        flags timeout
        # elements added at runtime: { 192.0.2.5 timeout 30s }
    }

    chain input {
        type filter hook input priority filter; policy drop;

        ct state established,related accept
        ct state invalid drop
        iif "lo" accept
        ip protocol icmp icmp type echo-request limit rate 5/second accept

        # Knock channel: rate-limited so junk can't flood the authorizer.
        # The daemon binds here and never replies, so the port reads as filtered.
        udp dport $KNOCK_PORT limit rate 20/second accept

        # Mesh data ports: only for peers the authorizer has vouched for.
        ip saddr @allowed_peers udp dport $MESH_PORT accept
        ip saddr @allowed_peers tcp dport $MESH_PORT accept

        # Everything else falls through to policy drop.
    }
}
```

Runtime grant (what the daemon runs, or does via netlink):

```sh
nft add element inet punctim_spa allowed_peers { 192.0.2.5 timeout 30s }
```

For the stronger IP+MAC binding from §4 on a local segment, use a `type ipv4_addr .
ether_addr` set and match `ip saddr . ether saddr @allowed_peers`.

**Stealthier variant.** Instead of `accept`ing `$KNOCK_PORT` and binding a UDP socket,
capture knock packets with libpcap/AF_PACKET and leave the port under `policy drop`. The
port then never accepts at all, yet the daemon still sees the token. The reference daemon
below uses the plain UDP-listener form for clarity; swap the receive path for pcap if you
want the port fully dark.

The daemon needs `CAP_NET_ADMIN` to edit the set (granted narrowly in §10).

## 8. Reference authorizer (Rust)

Reference-grade, single file. Crates: `ed25519-dalek = "2"`, `hmac = "0.12"`,
`sha2 = "0.10"`, plus std. Verification is constant-time (`verify_slice` / `verify_strict`).
Nonce cache is a `HashMap` swept lazily; swap for a bounded LRU under high load.

```rust
use std::collections::HashMap;
use std::net::{UdpSocket, Ipv4Addr, SocketAddr};
use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use ed25519_dalek::{Signature, VerifyingKey, Verifier};
use hmac::{Hmac, Mac};
use sha2::Sha256;
type HmacSha256 = Hmac<Sha256>;

const MAGIC: u8 = 0x53;
const VERSION: u8 = 0x01;
const HDR_LEN: usize = 31;
const LEN_HMAC: usize = HDR_LEN + 32;      // 63
const LEN_ED25519: usize = HDR_LEN + 64;   // 95

#[derive(Clone)]
enum Cred {
    Hmac([u8; 32]),          // per-device pre-shared key
    Ed25519(VerifyingKey),   // per-device public key
}

struct Config {
    knock_addr: SocketAddr,      // e.g. 0.0.0.0:62201
    default_mesh_port: u16,      // e.g. 7100
    window_ms: u64,              // e.g. 30_000
    grant_ttl_secs: u64,         // e.g. 30
    creds: HashMap<u16, Cred>,   // device_id -> credential
    nft_table: String,           // "punctim_spa"
    nft_set: String,             // "allowed_peers"
    // policy: device_id -> allowed ports (empty = any). Extend as needed.
    policy: HashMap<u16, Vec<u16>>,
}

struct Header {
    device_id: u16,
    timestamp_ms: u64,
    nonce: [u8; 16],
    port: u16,
}

fn parse_header(buf: &[u8]) -> Option<Header> {
    if buf.len() < HDR_LEN || buf[0] != MAGIC || buf[1] != VERSION {
        return None;
    }
    let device_id = u16::from_be_bytes([buf[2], buf[3]]);
    let timestamp_ms = u64::from_be_bytes(buf[4..12].try_into().ok()?);
    let mut nonce = [0u8; 16];
    nonce.copy_from_slice(&buf[12..28]);
    let port = u16::from_be_bytes([buf[28], buf[29]]);
    Some(Header { device_id, timestamp_ms, nonce, port })
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
}

fn verify_tag(cred: &Cred, buf: &[u8]) -> bool {
    let header = &buf[..HDR_LEN];
    match cred {
        Cred::Hmac(key) => {
            if buf.len() != LEN_HMAC { return false; }
            let mut mac = match HmacSha256::new_from_slice(key) { Ok(m) => m, Err(_) => return false };
            mac.update(header);
            mac.verify_slice(&buf[HDR_LEN..LEN_HMAC]).is_ok()   // constant-time
        }
        Cred::Ed25519(vk) => {
            if buf.len() != LEN_ED25519 { return false; }
            let sig_bytes: [u8; 64] = match buf[HDR_LEN..LEN_ED25519].try_into() { Ok(b) => b, Err(_) => return false };
            let sig = Signature::from_bytes(&sig_bytes);
            vk.verify_strict(header, &sig).is_ok()
        }
    }
}

fn grant(cfg: &Config, ip: Ipv4Addr) -> std::io::Result<()> {
    // Shell-out form for clarity; production: use nftables netlink (nftnl / rustables).
    let elem = format!("{{ {} timeout {}s }}", ip, cfg.grant_ttl_secs);
    let status = Command::new("nft")
        .args(["add", "element", "inet", &cfg.nft_table, &cfg.nft_set, &elem])
        .status()?;
    if !status.success() {
        eprintln!("nft add element failed for {ip}");
    }
    Ok(())
}

fn main() -> std::io::Result<()> {
    let cfg = load_config();            // read creds dir + settings; see §11/§12
    let sock = UdpSocket::bind(cfg.knock_addr)?;
    let mut seen: HashMap<(u16, [u8; 16]), Instant> = HashMap::new();
    let mut last_sweep = Instant::now();
    let mut buf = [0u8; 128];

    loop {
        let (n, peer) = match sock.recv_from(&mut buf) { Ok(x) => x, Err(_) => continue };
        let src = match peer { SocketAddr::V4(a) => *a.ip(), _ => continue };
        let pkt = &buf[..n];

        // lazy sweep of the nonce cache
        if last_sweep.elapsed() > Duration::from_secs(cfg.window_ms / 1000 + 1) {
            let ttl = Duration::from_millis(2 * cfg.window_ms);
            seen.retain(|_, t| t.elapsed() < ttl);
            last_sweep = Instant::now();
        }

        let hdr = match parse_header(pkt) { Some(h) => h, None => continue }; // silent
        let cred = match cfg.creds.get(&hdr.device_id) { Some(c) => c, None => continue };
        if !verify_tag(cred, pkt) { continue; }                              // silent

        let now = now_ms();
        if (now as i64 - hdr.timestamp_ms as i64).unsigned_abs() > cfg.window_ms {
            continue; // stale or future
        }
        let key = (hdr.device_id, hdr.nonce);
        if seen.contains_key(&key) { continue; } // replay

        let port = if hdr.port == 0 { cfg.default_mesh_port } else { hdr.port };
        if let Some(allowed) = cfg.policy.get(&hdr.device_id) {
            if !allowed.is_empty() && !allowed.contains(&port) {
                eprintln!("deny device {} for port {}", hdr.device_id, port);
                continue;
            }
        }

        seen.insert(key, Instant::now());      // record only on full success
        grant(&cfg, src)?;
        println!("grant device={} saddr={} port={} ttl={}s",
                 hdr.device_id, src, port, cfg.grant_ttl_secs);
    }
}

fn load_config() -> Config { unimplemented!("wire to §11/§12: creds dir, settings file") }
```

## 9. Reference client / knock sender

Python, for provisioning tests and CI. Sends one Ed25519 token. Requires
`pynacl` (`libsodium`); for HMAC mode use `hmac`/`hashlib` from stdlib.

```python
#!/usr/bin/env python3
# knock.py — send one DCF-SPA token
import os, socket, struct, sys, time
from nacl.signing import SigningKey   # Ed25519 mode

MAGIC, VERSION = 0x53, 0x01

def build_header(device_id: int, port: int) -> bytes:
    ts_ms = int(time.time() * 1000)
    nonce = os.urandom(16)
    return struct.pack(">BBHQ16sHB", MAGIC, VERSION, device_id, ts_ms, nonce, port, 0)

def token_ed25519(sk_hex: str, device_id: int, port: int) -> bytes:
    sk = SigningKey(bytes.fromhex(sk_hex))
    hdr = build_header(device_id, port)
    sig = sk.sign(hdr).signature            # 64 bytes
    return hdr + sig

def token_hmac(key_hex: str, device_id: int, port: int) -> bytes:
    import hmac, hashlib
    hdr = build_header(device_id, port)
    tag = hmac.new(bytes.fromhex(key_hex), hdr, hashlib.sha256).digest()  # 32 bytes
    return hdr + tag

if __name__ == "__main__":
    host, knock_port = sys.argv[1], int(sys.argv[2])
    device_id = int(sys.argv[3])
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    sk_hex = os.environ["DCF_SPA_KEY"]      # signing key (ed25519) or PSK (hmac)
    token = token_ed25519(sk_hex, device_id, port)   # or token_hmac(...)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(token, (host, knock_port))
    # no reply expected — the authorizer is silent by design
    print(f"sent {len(token)}-byte token: device={device_id} -> {host}:{knock_port}")
```

## 10. NixOS module (ArchibaldOS / Oligarchy)

Declarative: renders the nftables table and runs the authorizer as a hardened systemd
service holding only `CAP_NET_ADMIN`. Drop into your flake and set the options.

```nix
# modules/dcf-spa.nix
{ config, lib, pkgs, ... }:
let
  cfg = config.services.dcf-spa;
  spaPkg = pkgs.callPackage ../pkgs/dcf-spa-authorizer.nix { }; # your Rust build
in {
  options.services.dcf-spa = {
    enable = lib.mkEnableOption "DCF-SPA port authorizer";
    knockPort = lib.mkOption { type = lib.types.port; default = 62201; };
    meshPort  = lib.mkOption { type = lib.types.port; default = 7100; };
    windowMs  = lib.mkOption { type = lib.types.int;  default = 30000; };
    grantTtl  = lib.mkOption { type = lib.types.int;  default = 30; };
    credsDir  = lib.mkOption {
      type = lib.types.path;
      description = "Directory of per-device public keys (ed25519) or PSKs (hmac).";
    };
  };

  config = lib.mkIf cfg.enable {
    networking.nftables.enable = true;
    networking.nftables.tables.punctim_spa = {
      family = "inet";
      content = ''
        set allowed_peers { type ipv4_addr; flags timeout; }
        chain input {
          type filter hook input priority filter; policy drop;
          ct state established,related accept
          ct state invalid drop
          iif "lo" accept
          ip protocol icmp icmp type echo-request limit rate 5/second accept
          udp dport ${toString cfg.knockPort} limit rate 20/second accept
          ip saddr @allowed_peers udp dport ${toString cfg.meshPort} accept
          ip saddr @allowed_peers tcp dport ${toString cfg.meshPort} accept
        }
      '';
    };

    systemd.services.dcf-spa = {
      description = "DCF-SPA single-packet port authorizer";
      after = [ "network.target" "nftables.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        ExecStart = ''${spaPkg}/bin/dcf-spa-authorizer \
          --knock-port ${toString cfg.knockPort} \
          --mesh-port ${toString cfg.meshPort} \
          --window-ms ${toString cfg.windowMs} \
          --grant-ttl ${toString cfg.grantTtl} \
          --creds-dir ${cfg.credsDir} \
          --nft-table punctim_spa --nft-set allowed_peers'';
        DynamicUser = true;
        # Narrowly scoped: the authorizer only needs to edit the nftables set.
        AmbientCapabilities = [ "CAP_NET_ADMIN" ];
        CapabilityBoundingSet = [ "CAP_NET_ADMIN" ];
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_NETLINK" ];
        SystemCallFilter = [ "@system-service" ];
        MemoryDenyWriteExecute = true;
        LockPersonality = true;
      };
    };
  };
}
```

Usage:

```nix
services.dcf-spa = {
  enable = true;
  meshPort = 7100;
  credsDir = "/etc/dcf-spa/peers";   # 000X.pub files, see §11
};
```

## 11. Key management and provisioning

**Ed25519 (recommended).** Each device holds its own signing key; the authorizer holds
only the public keys.

```sh
# on the device, once:
DCF_SPA_KEY=$(openssl genpkey -algorithm ed25519 -outform DER | tail -c 32 | xxd -p -c 32)
# store DCF_SPA_KEY in the device's secret store; export its public key to the authorizer:
#   <device_id>.pub  in credsDir, containing the 32-byte raw public key (hex).
```

Provision by writing `NNNN.pub` (device_id, zero-padded) into `credsDir`. Rotation =
replace the file and reload the service. Revocation = delete the file. Because the
authorizer never holds a signing key, a compromise of the authorizer host cannot forge
tokens.

**HMAC.** Provision `NNNN.key` with a 32-byte random PSK, shared with the device. Simpler,
but the authorizer's key store is now sensitive — encrypt it at rest and restrict it to
the service user. Prefer sops-nix / agenix for the secrets on NixOS rather than
world-readable files.

Whichever mode: the credential is **for authentication only**. Do not reuse a device's
SPA key as a data-plane encryption key — that couples an authentication credential to a
confidentiality function and undermines §3.

## 12. Configuration reference

| Setting | Default | Meaning |
|---------|---------|---------|
| `knock_port` | 62201 | UDP port the authorizer watches for tokens. |
| `mesh_port` | 7100 | Punctim data port gated by the allow set. |
| `window_ms` | 30 000 | Accepted clock skew for token freshness (± this). |
| `grant_ttl_secs` | 30 | Lifetime of an allow-set entry. Long enough to establish; conntrack holds the session after. |
| `creds_dir` | — | Per-device public keys / PSKs. |
| `nft_table` / `nft_set` | `punctim_spa` / `allowed_peers` | Firewall objects the daemon edits. |
| `policy` | any | Optional per-device port allow-lists. |

Tune `grant_ttl_secs` down (to a few seconds) if peers connect immediately after
knocking; the shorter it is, the smaller the IP-spoofing window (§4).

## 13. Testing and validation

**Happy path.** Bring up the module, confirm the mesh port is unreachable, knock, confirm
it opens, confirm it re-closes after the TTL.

```sh
# from a peer:
nc -uzv MESH_HOST 7100   # expect: filtered/closed
DCF_SPA_KEY=<key> python3 knock.py MESH_HOST 62201 5   # device_id 5
nc -uzv MESH_HOST 7100   # expect: open, for grant_ttl seconds
```

**Replay must fail.** Capture a token and resend it; the second send must not open the
port.

```sh
# capture one token on the wire, then replay the exact bytes:
tcpdump -i any -w /tmp/knock.pcap udp port 62201 &   # capture during a real knock
# extract payload, resend verbatim -> authorizer must reject (nonce seen / stale)
```

CI should assert: valid token → allow-set gains the source; identical replay → no
change; tampered header (flip one byte) → no change; wrong device_id → no change; token
older than `window_ms` → no change.

**Stealth.** From an unprovisioned host, scan the fleet; the mesh port and the knock port
must both read as filtered/closed, and the authorizer must log nothing beyond a dropped
packet.

## 14. Operational rules

1. **Authenticate and open — never encrypt the data plane here.** The entire export
   rationale in §3 depends on it. Any change that adds confidentiality or session-key
   exchange must go through export review first.
2. **Confidentiality stays beneath the socket.** WireGuard (or equivalent,
   operator-supplied), per `DCF_SECURITY_EXPOSURE.md`. On hostile segments run WG *and*
   DCF-SPA (§4).
3. **Short TTLs.** Prefer the smallest `grant_ttl_secs` that lets a session establish.
4. **Silent on failure.** Never make the knock channel emit a distinguishable response.
5. **Guard the credential store.** Ed25519 by default so the authorizer holds no signing
   keys; if HMAC, encrypt the PSK store and scope it to the service user.
6. **Keep the DCF wire certificate untouched.** DCF-SPA is a side channel, not a frame
   change; nothing in `Documentation/golden_vectors.json` moves.

---

## Appendix A — self-classification memo (fill-in)

> **Item:** Punctim DCF-SPA port-authorization component (`dcf-spa-authorizer` +
> library), version ____.
>
> **Function:** Verifies a device's identity via a message authentication code
> (HMAC-SHA256) or digital signature (Ed25519) over a plaintext, nonce'd, timestamped
> token, as a prerequisite to installing a time-limited firewall rule permitting the
> device's address to reach a Punctim data port. The component performs authentication,
> data-integrity, and anti-replay functions only.
>
> **Cryptography for data confidentiality:** None. The component renders no user data
> unintelligible and exchanges no keys for data-plane encryption.
>
> **Classification rationale:** The cryptographic functionality is limited to
> authentication and digital signature and is therefore excluded from ECCN 5A002 by
> Note (g) to 5A002 and the BIS Technical Note on Authentication (15 CFR Part 774,
> Supp. 1, Cat. 5 — Part 2). No other Category 5 — Part 2 or CCL entry applies.
> **Determination: EAR99.**
>
> **Reviewed by:** ____________  **Date:** __________  **Counsel confirmation:** ______
>
> *This memo records a self-classification. EAR99 items remain subject to embargo,
> sanctioned-destination, and denied-party restrictions.*

---

*[DeMoD LLC](https://DeMoD.ltd) — Cut the bullshit, cut the price. Innovation without the overhead.*
