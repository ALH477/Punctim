# DCF wire exposure: the plaintext mesh and how to deploy it safely

DCF / Punctim is **encryption-free by design**. The core wire — the 17-byte
`DeModFrame` and the `ProtoMessage` UDP envelope that carries it — contains no
confidentiality, integrity-against-tampering, or authentication mechanism. This is a
deliberate, load-bearing decision for EAR/ITAR export compliance
(`Documentation/Specs/export_compliance.markdown`): a protocol with no cryptography
is not export-controlled as cryptographic software. **Do not add encryption to the
core wire path** — doing so changes the export posture of the entire project.

This document states, plainly, what that costs you and how to deploy DCF without
getting hurt.

## What an on-path observer sees

Because the wire is plaintext, **any party on the network path** — a malicious or
compromised router, a host with a span/mirror port, anyone on the same broadcast
segment, a hostile relay — can read and reconstruct, with **zero credentials**:

- **Membership**: which node ids exist, and their addresses.
- **Topology + latency**: the DCF-Mesh `REPORT` control message carries each node's
  peer list with per-peer status and RTT — i.e. the live mesh graph and its timing.
- **Roles / control**: the `ROLE` message reveals who the master is and every node's
  assigned role. An active attacker can also *forge* `REPORT`/`ROLE` (there is no
  authentication) to steer election or partition the mesh.
- **Message contents**: all application payloads — text, positions, game/audio
  adapter frames — are in the clear.

This is not a bug in any binding; it is the wire. It applies **equally to every
language node** (Go, C, Rust, Python, …) because they all speak the same plaintext
envelope. There is nothing to misconfigure and nothing to turn on — an eavesdropper
just reads it.

### See it for yourself

A passive wiretap is included (Python, stdlib-only):

```sh
# Terminal 1 — a real mesh node (master)
python3 python/dcf_node.py start --mode master --node-id 0 --bind 0.0.0.0:7000 \
    --peer 1@localhost:7001

# Terminal 2 — an auto node that reaches the master *through* the wiretap on :7100
python3 python/dcf_node.py start --mode auto --node-id 1 --bind 0.0.0.0:7001 \
    --master 0 --peer 0@localhost:7100

# Terminal 3 — the eavesdropper: capture + forward, decoding everything it sees
python3 python/dcf_node.py wiretap --bind 0.0.0.0:7100 --forward localhost:7000
```

Terminal 3 prints the decoded REPORT topology, the ROLE assignments, and any
application text — none of which it has any right to. A self-contained version is
`python/examples/wiretap_demo.py`, and the assertion-backed proof (leak **and**
mitigation) is `python/tests/test_eavesdrop_leak.py`.

## How to deploy it safely

Confidentiality and authentication are **the operator's responsibility, supplied
outside DCF**, at the transport layer beneath the UDP socket:

- **WireGuard (recommended).** Run every node's DCF traffic inside a WireGuard tunnel
  (or an equivalent operator-managed VPN / overlay). WireGuard provides modern
  authenticated encryption, peer authentication, and replay protection; DCF then runs
  over the encrypted interface and the on-path observer above sees only WireGuard
  packets. This keeps all cryptography — and its export classification — entirely
  outside this project.
- **Operator-supplied, legally-bound crypto.** If you cannot use WireGuard, wrap the
  datagrams in your own encryption layer that is contractually/legally bound to your
  deployment and its export obligations — again, *beneath* the DCF socket, as a
  transport wrapper, never inside the codec or `ProtoMessage`.

The test's `external_wrap` (a one-byte XOR in `python/dcf/wiretap.py`) is **only a
stand-in** to demonstrate the principle — once the bytes on the wire are no longer a
DCF `ProtoMessage`, the wiretap decodes nothing. It is not cryptography and must never
be mistaken for the real mitigation, which is WireGuard or an equivalent.

## Key material never rides the wire (DCF-QKD)

One module now handles key material: the ETSI GS QKD 014 bridge
([`DCF_QKD_SPEC.md`](DCF_QKD_SPEC.md)). It does **not** change the rule above — it
observes it. The bridge holds delivered keys in process memory and hands them to the
calling application; what crosses the DCF wire is the **`key_ID`** only, a non-secret
UUID that is useless without an authenticated session to a KME that still holds the
key. **Key material MUST NOT be placed in a `DeModFrame` payload**, and an automated
test (`python/tests/test_qkd_bridge.py::TestExportInvariant`) taps a live exchange and
fails if any window of a delivered key appears on the wire.

The exposure the beacon *does* add is traffic analysis: an on-path observer learns
which peers are keying, how often, and when, and can correlate that with data-plane
activity. That is the same class of leak this document already describes for
membership and topology, and it has the same mitigation — the tunnel below.

## The one rule

Keep the DCF wire plaintext; put the crypto in the tunnel under it. That preserves the
export posture that motivates DCF's design while giving real deployments the
confidentiality and authentication the bare wire deliberately omits.
