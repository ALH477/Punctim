# DCF-QKD — ETSI GS QKD 014 Key-ID Beacon over the DeModFrame Wire

**Version 1** · DeMoD LLC · This document is normative. Companion to
[`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) and a sibling of
[`DCF_TEXT_SPEC.md`](DCF_TEXT_SPEC.md) and [`DCF_AUDIO_SPEC.md`](DCF_AUDIO_SPEC.md).

> **Nothing in this adapter is quantum.** DCF's *wire quantum* is quantum as in
> **quanta** — the indivisible 17-byte `DeModFrame`, the sense in which physicists
> speak of a quantum of energy. It is unrelated to quantum mechanics, and this
> adapter carries no quantum information: a `key_ID` is an opaque 128-bit
> identifier minted by external KME hardware. Keys come from that hardware (from
> `os.urandom` in the mock fixtures). There are no photons in this repository.
>
> **Read [Export classification](#export-classification) before changing what this
> module touches.** It handles delivered key material in process memory, which is a
> different posture from the rest of the tree, and the rule that keeps it safe —
> *key material never reaches a `DeModFrame`* — is normative, not advisory.

DCF-QKD carries the **`key_ID`** of an ETSI GS QKD 014 key-delivery exchange across
the HydraMesh mesh. It introduces **no new wire format**: one `key_ID` is an
**adapter** over the 17-byte `DeModFrame` quantum, serialised into exactly four
ordinary `CTRL` (type 3) frames. The framing is **content-agnostic and
byte-deterministic across C, Rust, and Python**, pinned by a finite certificate
exactly like the wire quantum itself. **The 246-vector wire certificate is
untouched.**

## Why this adapter exists

ETSI GS QKD 014 — the REST key-delivery API that real QKD hardware speaks (IDQ,
Toshiba, AIT and Huawei devices were orchestrated together over it in Madrid's
MadQCI production network) — **deliberately leaves the transport of `key_ID` from
the master SAE to the slave SAE out of scope.** Every deployment has to invent its
own carrier. That gap is the single place a 17-byte deterministic frame beats
another REST call, and the arithmetic is exact:

```
key_ID = UUID = 128 bits = 16 bytes = exactly 4 × 4-byte DeModFrame payloads
```

Conform everywhere the standard *is* specified; extend only at the layer it leaves
open. A parallel key-delivery scheme would make HydraMesh the one node that nothing
else can talk to.

| Layer | Spec | This repo |
|-------|------|-----------|
| key delivery to applications | ETSI GS QKD 014 | `python/dcf/qkd/etsi014.py`, `mock_kme.py` |
| **`key_ID` transport** | ***explicitly out of scope*** | **this adapter** ← the contribution |
| SDN control / agent-controller | ETSI GS QKD 015 | not yet |
| key management, control | ITU-T Y.3803 / Y.3804 | not yet |
| classical channel | RFC 9340 | `DeModFrame` over any DCF transport |

Entangled photons cannot carry information without exactly this kind of classical
side channel — that is the no-communication theorem, and it is why this layer is
necessary rather than a consolation prize.

## Layered model

| Layer | What | Certified? |
|-------|------|-----------|
| **L1** identifier | One `key_ID` (a UUID, always 16 B). Opaque to everything below. | n/a (opaque, non-secret) |
| **L2** framing | `packetize` / reassemble: 16 identifier bytes ⇄ four `DeModFrame` `CTRL` frames. | **Yes** — `qkd_vectors.json` |
| **L3** delivery | Reassembly with dup-suppression, out-of-order tolerance, bounded in-flight set. | Runtime (unit-tested) |
| **L4** key delivery | ETSI 014 `status` / `enc_keys` / `dec_keys` against a KME over mTLS. | External standard |

The key invariant: **the `key_ID` is opaque to L2 and always 16 bytes**, so the
framing vectors are invariant to its content and this adapter needs no descriptor.

## L2 framing (normative)

One `key_ID` → **exactly four** frames. Every frame is a fully valid `DeModFrame`
(version = 1, type = `CTRL` = 3) and still passes the 246-vector wire certificate.

```
 seq (u16)      = epoch[15:2] (14 bits, 0..16383) | frag_idx[1:0] (2 bits, 0..3)
 frag_idx 0..3  data : payload = key_id_bytes[idx*4 .. +4]   (no padding, ever)
 frag_total     = 4 (constant)
 timestamp_us   = 24-bit send time, identical across a beacon's frames
 src_id         = local SAE / node id ; dst_id = rendezvous channel (0xFFFF = broadcast)
```

Bounds (from the 2-bit fragment field):

| Quantity | Value |
|----------|-------|
| Fragments per beacon | **4, always** |
| `key_ID` size | **16 bytes, always** |
| Max `epoch` | 16383 (14-bit; wraps modulo 16384) |
| Frames per beacon | 4 |

### Why there is no descriptor fragment

Every other fragmenting adapter in this tree (text, game, sstv, audio, snake) burns
`frag_idx 0` on a descriptor carrying the true byte length, because their payloads
vary in size and the final fragment is zero-padded. A `key_ID` is **always** 16
bytes, so length and `frag_total` are known a priori, 16 divides evenly by 4, and
there is nothing to carry in band. **All four fragments are data.** This is a
normative design property, not an omission: an implementation that emits a
descriptor is non-conforming and will fail the vectors.

There are consequently **no descriptor flags**. A beacon is a bare statement of
fact — *this key_ID is now available to you* — and needs no delivery-behaviour
hints. A future extension must not steal fragment bits for flags; it should use a
separate `dst` channel or an epoch subrange.

### The epoch field

The 14-bit `epoch` distinguishes concurrent in-flight beacons between the same
peers, so two `key_ID`s can never interleave their fragments. It is a local
sequence number, not a key index: it carries no meaning to the KME and is free to
wrap. Senders SHOULD increment it per beacon.

### Reassembly

Beacons are keyed by **`(src_id, dst_id, epoch)`**, so two SAEs may beacon the same
epoch concurrently without cross-contamination. Reassembly is **order-independent**
— a receiver accepts fragments in any order and completes as soon as all four
indices for one key are present. Duplicates are ignored. A frame that fails sync,
version or CRC is **dropped and logged, never fatal**: lossy transports are
expected.

Incomplete beacons are **bounded**. Once more than `max_pending` (default 64) are
in flight, the oldest is evicted and reported lost; otherwise a lossy link leaks
memory indefinitely. `finalize()` reports every still-incomplete beacon as lost, in
ascending `(src, dst, epoch)`.

## Relationship to the other CTRL adapters

`CTRL` (type 3) is shared with DCF-Audio, DCF-Cue, DCF-Snake and DCF-Pipe control.
As everywhere else in the tree there is **no in-band adapter tag**: the adapters
differ only by their `seq` split, which is not self-describing, and are separated by
**deployment, not by the bytes** — a node runs exactly one reassembler per `dst`
channel.

| Adapter | Type | `seq` split (id : frag) |
|---------|------|-------------------------|
| DCF-Audio | CTRL(3) | 11 : 5 |
| DCF-Cue (monitor) | CTRL(3) | 9 : 7 |
| DCF-Snake (record) | CTRL(3) | 5 : 11 |
| **DCF-QKD (key-ID beacon)** | **CTRL(3)** | **14 : 2** |

The 14:2 split is unique among them. **Never multiplex a key-ID beacon and another
CTRL adapter on the same `dst`.**

## Addressing & channel rendezvous

`dst_id = 0xFFFF` is broadcast; otherwise `dst_id = channel_id(name) =
crc16_ccitt(name)`, the same frequency-channel rendezvous hash the rest of the repo
uses. A receiver accepts a frame iff `dst == my_channel || dst == 0xFFFF`, and
multiplexes correspondents by `src_id`. Anchor: `channel_id("123456789") = 0x29B1`.

## Certification

| Artifact | Role |
|----------|------|
| `python/MCP/qkdlab_core.py` | canonical reference (Python) |
| `python/MCP/gen_qkd_vectors.py` | executable laws + vector generator |
| `Documentation/qkd_vectors.json` | the certificate (5 framing + 7 reassembly cases) |
| `python/MCP/qkd_vectors.json` | byte-identical copy, CI-diffed |
| `codec/qkd_vectors.gen.h` | dependency-free C vector dump |

```sh
python3 python/MCP/qkdlab_core.py                              # -> "qkdlab_core selftest: CERTIFIED"
python3 python/MCP/gen_qkd_vectors.py /tmp/qkd_vectors.json    # -> "ALL QKD LAWS HOLD"
diff /tmp/qkd_vectors.json  Documentation/qkd_vectors.json     # must be empty
diff /tmp/qkd_vectors.json  python/MCP/qkd_vectors.json        # must be empty
diff /tmp/qkd_vectors.gen.h codec/qkd_vectors.gen.h            # must be empty
cd codec && cargo test --test certify_qkd                      # Rust
gcc -std=c11 -I codec C_SDK/tests/test_qkd_certify.c -lm -o /tmp/qc && /tmp/qc   # C
cd python && python3 -m unittest tests.test_qkd_beacon tests.test_qkd_bridge -v
```

The reassembly vectors cover `in_order`, `reordered`, `frag_drop_lost`,
`duplicate`, `interleaved_epochs`, `two_srcs_same_epoch` and
`foreign_dst_ignored`. CI runs the Python/C/Rust certs and diffs regenerated vs.
committed vectors (`.github/workflows/wire-certify.yml`, job `certify-qkd`); the
bridge runtime is exercised by the separate `qkd-bridge` job.

Eviction of an over-capacity in-flight beacon is a **runtime** bound, exercised by
the Python and Rust unit tests but deliberately **not** covered by the vectors (no
vector stream exceeds the slot table). The C reference evicts the oldest beacon
silently rather than reporting it lost; that divergence is documented in
`codec/demod_qkd.h` and is outside the certified surface.

**Anchor.** `exampleKeyIdBeacon`: `key_ID = 574bace1-4c27-49a1-babd-663fdb624d00`,
`epoch = 10`, `ts_us = 66051`, `src = 0x00A1`, `dst = channel_id("qkd") = 0xD856`
→ exactly 4 frames, first frame `d313002800a1d856574bace1010203a8d3`.

## Reference implementations

| Lang | File | Entry points |
|------|------|--------------|
| C | `codec/demod_qkd.h` | `dcf_qkd_packetize` / `dcf_qkd_reasm_push`, `dcf_qkd_channel_id` |
| Rust | `codec/src/qkd.rs` | `packetize` / `KeyIdReassembler`, `channel_id` |
| Python | `python/MCP/qkdlab_core.py` | `packetize` / `KeyIdReassembler`, `channel_id` |

`python/MCP/qkdlab_core.py` is the canonical source of truth (the generator
`gen_qkd_vectors.py` emits the laws + vectors from it).

## Consumers — the ETSI 014 bridge

`python/dcf/qkd/` is the runtime that makes the beacon useful. It is **stdlib
only**: no pip, no venv, no network needed to test.

| Module | Role |
|--------|------|
| `beacon.py` | key-ID beacon over any `dcf.transport.Transport` |
| `etsi014.py` | ETSI GS QKD 014 client — `status` / `enc_keys` / `dec_keys`, optional mTLS |
| `mock_kme.py` | mock KME pair over one shared keystore (the simulated QKD link) |
| `sae.py` | `MasterSae` / `SlaveSae` — the two halves of an exchange |
| `demo.py` | end-to-end harness + negative tests, hardware-free over loopback |

`dcf.qkd` ships in the wheel and in the Nix `dcf-python` package
(`python/pyproject.toml`), and exposes a `dcf-qkd-demo` console script plus a
`nix run .#qkd-demo` app. The package itself is stdlib-only; it composes with
`dcf.transport`, which pulls in numpy (the SDK's one declared runtime dependency).

The full flow:

```
master SAE --enc_keys--> KME-A                          (ETSI GS QKD 014)
master SAE --key-ID beacon: 4 × DeModFrame--> slave SAE  (DCF: the out-of-scope bit)
slave  SAE --dec_keys--> KME-B                          (ETSI GS QKD 014)
=> both ends hold identical key material
```

Roles are **per key, not per node**: the SAE that calls `enc_keys` is the master for
those keys; a bidirectional peer plays both roles depending on traffic direction.

```sh
python3 python/dcf/qkd/demo.py --count 3            # self-contained, loopback
python3 python/dcf/qkd/demo.py --json               # machine-readable, for CI
nix run .#qkd-demo -- --count 3                     # same, hermetic
python/dcf/qkd/testdata/tls_check.sh /tmp/qkdcerts  # manual mTLS smoke test (needs openssl)

# against QuKayDee or vendor hardware — same code path, no changes
python3 python/dcf/qkd/demo.py --external \
    --kme-a https://kme-a.example:443 --kme-b https://kme-b.example:443 \
    --cert sae.crt --key sae.key --ca ca.crt
```

Because the beacon rides an ordinary `Transport`, a `key_ID` can cross **any** DCF
medium — UDP, HydraModem acoustic, JANUS underwater — unchanged. Four 17-byte
frames is 68 bytes, roughly 7 seconds on the ~8–12 B/s acoustic link.

## Theorem

L2 framing is a fixed bit-placement adapter over the certified `DeModFrame`.
Matching the framing + reassembly vectors pins the C and Rust implementations to
the Python reference on the entire input space. The `key_ID` is opaque to L2 and
always 16 bytes, so the beacon needs no descriptor and the vectors are invariant to
`key_ID` content — adding transports or key-delivery back-ends never re-opens the
certificate, and **the 246-vector wire certificate is untouched**.

## Export classification

This section is the reason the feature can live in-project. It is written so it can
be lifted directly into a self-classification record (Appendix A).

**This is not the DCF-SPA argument.** DCF-SPA decontrols via the ECCN 5A002 Note (g)
*authentication* carve-out, and [`DCF_SPA_SPEC.md`](DCF_SPA_SPEC.md) §3 states the
limit of that carve-out in terms: *"Key management **for authentication keys** is
fine; key exchange **to bootstrap data-plane encryption** is controlled."* That
carve-out does **not** reach a key-delivery bridge. DCF-QKD needs its own argument,
and it is a different one.

**The argument.**

1. **This module implements no cryptographic algorithm.** No cipher, no key
   agreement, no key derivation, no MAC. It makes REST calls and moves 128-bit
   identifiers. There is nothing here that renders any user data unintelligible.
2. **Key generation and key exchange are performed by external KME hardware**, not
   by DCF. The bridge is a *client* of equipment the operator already owns and has
   already classified. DCF neither produces nor negotiates key material.
3. **The wire carries only the `key_ID`** — a UUID, a non-secret identifier that is
   useless without an authenticated session to a KME that still holds the key.
4. **The transport security to the KME is platform TLS** (stdlib `ssl`) against an
   operator-supplied endpoint, which is exactly the posture
   [`Specs/export_compliance.markdown`](Specs/export_compliance.markdown) already
   records for DCF: *"optional user-added TLS … not integrated into the core."*

**The new posture, stated plainly rather than buried.** Unlike every other module
in this tree, the bridge process **holds delivered key material in memory** — that
is what `dec_keys` returns. DCF has until now been able to say it never touches
keys; that is no longer true of this module. Nothing is done *with* those bytes
here — they are handed to the calling application — but the change is real and it
is the thing to put in front of counsel.

**The normative rule.**

> **Key material MUST NOT be placed in a `DeModFrame` payload.** The beacon carries
> the `key_ID` and nothing else. Key bytes never enter the codec, never enter the
> wire, and never enter a vector file.

This is enforced, not merely asserted: `python/tests/test_qkd_bridge.py`
(`TestExportInvariant`) taps the wire during a live exchange and fails if any
delivered key — or any 4-byte window of one, i.e. a single frame payload — appears
in the captured bytes. `python/dcf/qkd/demo.py` runs the same check as its final
assertion.

**The one line you must not cross.** If a delivered key is ever wired into a
cipher at the DCF layer — the tempting version: use the QKD key to encrypt mesh
traffic — the posture collapses, and not just for this module. DCF's
encryption-free design is what keeps the *entire project* outside 5D002/5A002
(`Specs/export_compliance.markdown`,
[`DCF_SECURITY_EXPOSURE.md`](DCF_SECURITY_EXPOSURE.md)). Deliver the key to the
application and stop. Confidentiality stays where that document already puts it:
WireGuard beneath the socket, operator-supplied.

**Non-goals — explicitly forbidden without a separate, export-reviewed module.**

1. Encrypting any DCF traffic with a delivered key.
2. Post-quantum or classical cipher suites of any kind in this package.
3. Key derivation, key wrapping, or key agreement in DCF.
4. Putting key material — whole, partial, or transformed — on the wire.
5. Entanglement-distribution or quantum-repeater features. That is research-stage
   physics requiring hardware this project does not and will not have.

**Standard caveats.** This is a self-classification rationale, not legal advice
from counsel. **It warrants a firmer counsel review than DCF-SPA's**, because the
authentication decontrol that covers SPA does not apply here and because the module
handles key material at all. EAR99 items still cannot go to embargoed/sanctioned
destinations or denied parties. If the fleet ships to sensitive destinations —
which, for QKD integrations, is a live possibility — confirm the classification with
export counsel before shipping.

**Regulatory references**

- ECCN 5A002 / 5D002 and Notes — 15 CFR Part 774, Supplement No. 1, Category 5 — Part 2.
- Definition of "cryptography for data confidentiality" — 15 CFR Part 772.
- ETSI GS QKD 014 — *Quantum Key Distribution (QKD); Protocol and data format of
  REST-based key delivery API*.

## Security posture

Like the rest of DCF, the beacon wire is **plaintext** — the protocol is
deliberately encryption-free for export compliance. The `key_ID`s it carries are
non-secret, but they are a **traffic-analysis signal**: an on-path observer learns
which peers are keying, how often, and when, and can correlate that with data-plane
activity. An active on-path attacker can also inject or suppress beacons, causing a
slave SAE to request keys that were never minted for it (the KME rejects those:
`dec_keys` binds each `key_ID` to its master SAE and enforces single delivery) or to
miss keys entirely (a denial of service, not a disclosure).

Deploy behind WireGuard or operator-supplied, export-compliant crypto **beneath**
the UDP socket; never add encryption to the codec. See
[`DCF_SECURITY_EXPOSURE.md`](DCF_SECURITY_EXPOSURE.md).

## Where this could go next

- **ETSI GS QKD 015 SDN agent.** MadQCI put a per-node agent under a central
  controller; that is the natural next layer up and where multi-vendor orchestration
  actually happens.
- **Timing/sync signalling.** Below key delivery, the QKD transmitter/receiver sync
  layer is largely vendor-proprietary and *not* standardised the way ETSI 014 is.
  That is a genuine gap where a deterministic low-latency frame has room — but it
  cannot be validated without photonic hardware, so do not build speculatively
  against an imagined interface.
- **QuKayDee.** A cloud-hosted QKD network simulator built for exactly this: testing
  classical integration against the key-delivery interface without hardware.
  `demo.py --external` already points there unmodified.

## Appendix A — self-classification memo (fill-in)

> **Item:** HydraMesh DCF-QKD key-ID beacon adapter and ETSI GS QKD 014 bridge
> (`python/dcf/qkd/`, `codec/demod_qkd.h`, `codec/src/qkd.rs`,
> `python/MCP/qkdlab_core.py`), version ____.
>
> **Function:** Transports a 128-bit key identifier (`key_ID`, a UUID) between two
> Secure Application Entities as four 17-byte plaintext frames, and acts as an HTTP
> client to external Key Management Entities implementing ETSI GS QKD 014
> (`status` / `enc_keys` / `dec_keys`). The component performs identifier transport
> and API client functions only.
>
> **Cryptography for data confidentiality:** None. The component implements no
> cipher, key-agreement, key-derivation or MAC algorithm, and renders no user data
> unintelligible. Transport security to the KME is platform TLS against an
> operator-supplied endpoint. Key generation and key exchange are performed entirely
> by external KME hardware.
>
> **Key material handling:** The component receives key material from external KME
> hardware over that operator-supplied TLS channel and holds it in process memory
> for delivery to the calling application. Key material is never transmitted on the
> DCF wire, never enters the DCF codec, and is not used by the component for any
> cryptographic operation. This constraint is enforced by an automated test
> (`python/tests/test_qkd_bridge.py::TestExportInvariant`).
>
> **Classification rationale:** The item implements no cryptographic functionality
> and therefore falls outside ECCN 5A002 / 5D002 (15 CFR Part 774, Supp. 1, Cat. 5 —
> Part 2). Key material handled on behalf of external, separately classified
> equipment is data processed by the item, not cryptographic functionality performed
> by it. No other Category 5 — Part 2 or CCL entry applies.
> **Determination: EAR99.**
>
> **Reviewed by:** ____________  **Date:** __________  **Counsel confirmation:** ______
>
> *This memo records a self-classification. The authentication decontrol relied upon
> by DCF-SPA does not apply to this item; counsel confirmation is recommended rather
> than optional. EAR99 items remain subject to embargo, sanctioned-destination, and
> denied-party restrictions.*

---

*[DeMoD LLC](https://DeMoD.ltd) — Cut the bullshit, cut the price. Innovation without the overhead.*
