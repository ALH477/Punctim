# HydraPack — Universal Serialization for Punctim

**Version 0.1** · DeMoD LLC · LGPL-3.0 · This document is normative.
**Companion documents:**
- [`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) (17-byte DeModFrame)
- [`DCF_PIPE_SPEC.md`](DCF_PIPE_SPEC.md) (bulk lossless transfer)
- [`DCF_AUDIO_SPEC.md`](DCF_AUDIO_SPEC.md) (worked example of a quantum adapter)
- [`SUPERPACK_SPEC.md`](SUPERPACK_SPEC.md) (paired-frame container)

> **Scope.** HydraPack is the single serialization layer that sits above both
> of Punctim's data planes. It produces either a sequence of 4-byte quanta
> (for the wire quantum / adapter path) or a contiguous byte buffer (for the
> DCF-Pipe data plane). The choice is driven by size and schema policy.
> HydraPack never invents a new wire format; it only decides *how* application
> values are turned into the representations already defined by the quantum
> and by Pipe.

---

## 1. Design Goals

1. **One schema system** for every domain (audio parameters, game state,
   telemetry, control, bulk objects, etc.).
2. **Plane-aware emission**
   - Small / real-time values → Quantum path (dense 4-byte payloads).
   - Large / bulk values → Pipe path (contiguous bytes + session metadata).
3. **Maximum information density** on the quantum path (the scarce resource
   is the 4-byte payload).
4. **Byte-determinism** across all certified language implementations.
5. **Certification by finite golden vectors** (same contract as the wire
   quantum, SuperPack, and Pipe control messages).
6. **Zero or near-zero dynamic allocation** on the quantum hot path.
7. **Preservation of existing invariants**
   - The 17-byte DeModFrame remains the only certified wire quantum.
   - Pipe control messages continue to ride as ordinary frame payloads.
   - The Pipe data plane remains a dumb, high-throughput datagram lane.

---

## 2. Two Emission Planes

```
Application value
        │
        ▼
┌──────────────────────────────────────────┐
│              HydraPack                   │
│  Schema + packing rules + size policy    │
└─────────────┬───────────────┬────────────┘
              │               │
              ▼               ▼
     Quantum Path        Pipe Path
   (≤ size threshold)   (> size threshold)
              │               │
              ▼               ▼
   ordered list of       contiguous byte
   4-byte payloads       buffer + metadata
   (ready for adapter    (handed to Pipe
    framing / SuperPack)  OPEN + CHUNK)
```

### 2.1 Quantum Path

- Output: ordered sequence of exactly 4-byte payloads.
- Framing into DeModFrame sequences (descriptor + data quanta) is the
  responsibility of a thin adapter layer (the same pattern used by
  DCF-Audio).
- Preferred for messages that fit in <= 120 bytes after packing.
- Aggressive bit-packing, fixed layouts, and delta encoding are expected.

### 2.2 Pipe Path

- Output: a single contiguous, deterministic byte buffer together with
  metadata (`schema_id`, schema version, whole-object FNV-1a checksum).
- The buffer is handed to a DCF-Pipe sender. All chunking, sequencing,
  FEC, credit, and ARQ are performed by Pipe.
- Preferred for any object that would be inefficient or impossible to
  carry as a short burst of 4-byte quanta.

### 2.3 Size Decision

A schema (or a call-site override) supplies a threshold `T`
(default recommendation: 120 bytes).

```
if packed_size <= T → Quantum Path
else               → Pipe Path
```

The decision is pure and deterministic given the schema and the value.

---

## 3. Schema Model (Language-Agnostic)

A **schema** is an ordered collection of fields with explicit widths and
packing rules. Schemas are identified by a 16-bit integer `schema_id`
and a 4-bit `version` so they can be referenced from quantum descriptors,
Pipe `OPEN` messages, and call sites.

### 3.1 Core Field Types (v0.1)

| Type     | Description                                      | Notes |
|----------|--------------------------------------------------|-------|
| `uN`     | Unsigned integer of exactly N bits               | N ∈ {1…64} |
| `iN`     | Signed integer (two's complement) of N bits      | N ∈ {1…64} |
| `bool`   | 1-bit boolean                                    | |
| `enum`   | Small integer discriminant                       | Explicit width |
| `bits`   | Opaque bitfield of fixed width                   | |
| `struct` | Nested ordered collection of fields              | Sub-fields inline |

**Deferred to v0.2 (additive, vectors won't move):**
`fixed[N]`, `bytes[N]`, `blob` (length-prefixed variable byte sequence),
`optional` (presence bit + payload). These are rare on the quantum path
and easy to add without touching existing bit-pack vectors.

All multi-byte integers are **big-endian** unless a schema explicitly
declares otherwise (matching the rest of the DCF wire).

### 3.2 Packing Rules

- Fields are packed in declaration order.
- Bitfields and small integers may share bytes (bit-packing is mandatory
  for density on the quantum path).
- The bit stream is **big-endian (MSB-first)**: the first field occupies
  the high bits of byte 0, the next field occupies the next lower bits,
  and so on.
- Padding bits (if required for alignment to a byte boundary) are zero
  and must be ignored on decode.
- The total packed size of a value under a schema is a pure function of
  the schema and the value (no hidden state). In v0.1 (fixed-width types
  only) it is a pure function of the schema alone.

### 3.3 Delta / Predictive Mode (Optional, Reserved)

A schema may declare that certain fields are transmitted as deltas
relative to a previously agreed base value (last-seen, predicted, or
explicitly negotiated). Delta mode is especially valuable on the
quantum path for high-frequency state (positions, parameters, sensors).

When delta mode is active the serializer and deserializer share a
per-peer prediction state that lives *outside* HydraPack itself.
HydraPack only encodes the numeric difference according to the schema's
declared rules. (v0.1 reserves the descriptor flag bit for this;
implementation is deferred.)

### 3.4 Schema Versioning

- `schema_id` identifies the logical schema.
- A separate 4-bit `version` allows additive evolution (16 slots).
- Receivers that do not understand a version must reject the object
  (or fall back according to a documented policy).

---

## 4. Quantum Path Encoding

### 4.1 Single-Quantum Messages (Preferred)

When the packed representation of a value fits in <= 4 bytes, the entire
4-byte payload IS the packed value (zero-padded to 4 bytes). No extra
header is required; the schema is implied by context (the call site or
adapter arrangement).

### 4.2 Multi-Quantum Messages

When a value requires more than 4 bytes:

```
Quantum 0 (descriptor — 4 bytes, byte-aligned, big-endian)
  B0  schema_id_hi
  B1  schema_id_lo
  B2  (schema_version << 4) | flags   (4-bit version nibble, 4-bit opaque flags)
  B3  payload_byte_len                (the packed-data byte length, 0..255)

Quantum 1 … N
  pure packed data (last quantum zero-padded to 4 bytes)
```

The number of data quanta = `ceil(payload_byte_len / 4)`. The total
number of quanta = `1 + ceil(payload_byte_len / 4)`.

This descriptor generalizes the pattern used by DCF-Audio
(`[payload_len, frag_total, codec_id, flags]`), DCF-Text
(`[len_hi, len_lo, flags, 0]`), and DCF-Game — each is a 4-byte
byte-aligned descriptor that names the payload and its interpretation.

### 4.3 SuperPack Affinity

When emitting a multi-quantum message, implementations should prefer
an even number of quanta whenever the cost is negligible, so that
SuperPack can collapse pairs into 32-byte datagrams.

### 4.4 Reassembly

1. Collect the descriptor and all data quanta in order (the adapter layer
   handles ordering and completeness via the surrounding DeModFrame's
   `seq`/`frag_idx` fields).
2. Concatenate the data quanta and truncate to `payload_byte_len`.
3. Interpret the resulting byte string according to the schema named
   by `(schema_id, version)` in the descriptor.

Missing fragments cause the whole logical message to be dropped
(identical policy to DCF-Audio/Text/Game — the adapter layer detects
this via the frame sequence fields, not HydraPack).

For single-quantum messages, the caller provides the schema by context;
HydraPack unpacks the 4-byte payload directly.

---

## 5. Pipe Path Encoding

### 5.1 Output

```
bytes          : contiguous packed representation of the value
schema_id      : 16-bit identifier (carried in OpenPipe)
schema_version : 4-bit version of the schema used (carried in OpenPipe)
checksum       : FNV-1a 32-bit over the entire byte buffer
                 (init = 0x811C9DC5, prime = 0x01000193)
```

The byte buffer contains **no** HydraPack-specific framing. All
framing, sequencing, FEC, and reliability are performed by DCF-Pipe.

### 5.2 OpenPipe — the OPEN extension

```
  14-byte OPEN (from DCF-Pipe, unchanged and certified):
    [0]=0 [1]=ver [2:4]=session [4:8]=total_len [8:10]=chunk_size [10:14]=checksum

  3-byte schema extension (appended by HydraPack):
    [14]=schema_id_hi
    [15]=schema_id_lo
    [16]=(schema_version << 4) | flags   (4-bit version, 4-bit flags)

  Total = 17 bytes
```

A plain-Pipe receiver sees a valid 14-byte OPEN and ignores the trailing
3 bytes (additive, fail-safe). A HydraPack receiver reads them to select
the correct deserializer after the transfer completes and the FNV-1a
check passes. The Pipe `pipe_vectors.json` certificate is untouched.

### 5.3 Determinism

Given the same schema and the same abstract value, every certified
implementation must produce identical bytes. The FNV-1a checksum is the
same table-free hash used by Pipe (anchors: `FNV("") = 0x811C9DC5`,
`FNV("hello") = 0x4F9F2CAB`).

---

## 6. Relationship to Existing Components

| Component              | Relationship to HydraPack                                      |
|------------------------|----------------------------------------------------------------|
| DeModFrame             | HydraPack never alters it. Quantum-path output becomes the     |
|                        | 4-byte payload of one or more frames.                          |
| DCF-Audio / Game / …   | Domain adapters become thin consumers of HydraPack quanta.     |
| SuperPack              | Orthogonal; operates on already-formed frames.                 |
| DCF-Pipe control       | OPEN / CREDIT / SACK / … remain ordinary frame payloads; they  |
|                        | may themselves be expressed with HydraPack primitives.         |
| DCF-Pipe data plane    | Receives the contiguous buffer produced by the Pipe path.      |
| DCF-FEC                | Applied by Pipe to each chunk; invisible to HydraPack.         |

---

## 7. Certification Contract

HydraPack is certified by finite golden-vector sets, exactly as the
wire quantum, SuperPack, and Pipe control messages are certified.

Two independent vector families are required:

1. **Quantum vectors** — for every schema under test, a set of
   (value → list of 4-byte payloads) pairs that every implementation
   must match exactly. Includes single-quantum, multi-quantum, and
   round-trip (unpack → re-pack → byte-identical) cases.
2. **Pipe vectors** — for every schema under test, a set of
   (value → byte buffer + checksum) pairs that every implementation
   must match exactly. Includes OpenPipe pack/unpack round-trips.

Passing the vector sets is defined to be equivalent to agreement on
the entire relevant input space for those schemas (the same formal
stance taken by the 246-vector wire certificate).

Reference implementations in C, Rust, and Python are expected;
additional languages follow the same vectors.

```sh
python3 python/MCP/gen_hydrapack_vectors.py /tmp/hp.json        # regen + verify laws
cd codec && cargo test --test certify_hydrapack                  # Rust
gcc -std=c11 -I codec C_SDK/tests/test_hydrapack_certify.c -o /tmp/hp && /tmp/hp  # C
```

| Artifact | Role |
|----------|------|
| `Documentation/hydrapack_vectors.json` (= `python/MCP/`) | quantum + pipe + openpipe vectors |
| `codec/hydrapack_vectors.gen.h` | same vectors as a C header (dependency-free C test) |
| `python/MCP/hydrapack_core.py` | Python reference (canonical) |
| `python/MCP/gen_hydrapack_vectors.py` | executable laws + vector generator |
| `codec/demod_hydrapack.h` | C header-only reference |
| `codec/src/hydrapack.rs` | Rust reference |

---

## 8. Non-Goals

- HydraPack does **not** provide encryption, authentication, or
  confidentiality. Those remain the responsibility of lower layers
  (WireGuard, etc.) or of higher application policy.
- HydraPack does **not** invent a new wire format or a new frame type.
- HydraPack does **not** replace DCF-Pipe's flow control, FEC, or
  ARQ semantics.
- HydraPack does **not** attempt to be a general-purpose
  self-describing format (CBOR / MessagePack style) on the quantum
  path; density is preferred over self-description for high-rate
  traffic.
- Schema negotiation and dynamic schema distribution are out of
  scope for version 0.1 (schemas are assumed to be known a priori
  or distributed by an external mechanism).
- `blob`, `optional`, and `fixed[N]` field types are deferred to v0.2.

---

## 9. Versioning of This Specification

- This document is versioned independently of the wire quantum.
- Breaking changes to packing rules or to the plane-selection
  contract require a new major version of HydraPack and new
  golden-vector sets.
- Additive schema evolution (new fields with defined defaults) is
  encouraged and does not require a HydraPack major version bump.

---

## 10. Summary of Responsibilities

| Layer                    | Responsibility                                      |
|--------------------------|-----------------------------------------------------|
| Application / Domain     | Produce abstract values; choose schemas             |
| **HydraPack**            | Schema → packed quanta **or** packed byte buffer    |
| Quantum Adapter          | Quanta → DeModFrame sequence (descriptor + data)    |
| SuperPack (optional)     | Pair frames when beneficial                         |
| DCF-Pipe                 | Session, credit, chunking, FEC, ARQ, integrity      |
| Transport                | UDP / acoustic / etc.                               |

HydraPack is the single point at which an abstract value becomes
either a short burst of 4-byte quanta or a bulk byte stream.
Everything below it is already defined by the existing Punctim
specifications.

---

*DeMoD LLC — Cut the bullshit, cut the price. Innovation without the overhead.*