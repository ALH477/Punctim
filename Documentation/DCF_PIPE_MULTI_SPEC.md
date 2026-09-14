# DCF-Pipe Multi-Control

**Version 0.1** · DeMoD LLC · LGPL-3.0 · This document is normative.
**Companion documents:**
- [`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) (17-byte DeModFrame)
- [`DCF_PIPE_SPEC.md`](DCF_PIPE_SPEC.md) (lossless bulk transfer)
- [`HYDRAPACK_SPEC.md`](HYDRAPACK_SPEC.md) (optional serialization layer)

> **Scope.** This specification defines a dense control encoding that allows
> a single 4-byte DeModFrame payload to steer up to three concurrent
> DCF-Pipe sessions simultaneously. It turns the wire quantum into a
> parallel control bus for multiple high-throughput data pipes while
> leaving the existing Pipe data plane and the original single-session
> control messages intact.

---

## 1. Motivation

DCF-Pipe already separates concerns cleanly:

- **Control plane** — small messages carried as ordinary DeModFrame payloads.
- **Data plane** — high-throughput chunked datagrams.

The original control messages (OPEN, CREDIT, SACK, NACK, DONE, ABORT)
are sized for one session. On links where quantum bandwidth is scarce
(especially acoustic / HydraModem profiles), the control plane becomes
the limiting factor when several pipes run concurrently.

Multi-Control addresses this by allowing one 4-byte payload to carry
useful steady-state control information for up to three active pipes
at once. Session setup, teardown, and large control messages continue
to use the existing single-session formats.

---

## 2. Design Goals

1. Fit meaningful progress for multiple pipes into a single 4-byte payload.
2. Preserve the existing DCF-Pipe data plane and reliability model.
3. Keep the DeModFrame wire quantum completely unchanged.
4. Remain fully deterministic and certifiable by golden vectors.
5. Gracefully interoperate with the original single-session control messages.
6. Favour byte-alignment at all costs — every command slot starts at a
   byte boundary.

---

## 3. Core Concepts

### 3.1 Active Set

Each endpoint maintains an **Active Set** of at most three Pipe sessions
that may be controlled by Multi-Control messages.

- Members of the Active Set are addressed by a local 2-bit index (0–3).
- Mapping from local index → full `session_id` is established by
  ordinary single-session control messages (or by a dedicated
  Active-Set management message — deferred to a future version).
- A session that is not in the Active Set cannot be steered by
  Multi-Control; it must use classic single-session control.

**v0.1 scope:** The codec only packs/unpacks the 4-byte message. Active-Set
mapping (local_idx ↔ session_id) is caller-managed runtime state, not
byte-certified.

### 3.2 Control Domains

| Domain                    | Encoding                     | When used                              |
|---------------------------|------------------------------|----------------------------------------|
| Classic single-session    | Existing PIPE control msgs   | OPEN, large NACK, ABORT, DONE, setup   |
| Multi-Control             | This specification           | Steady-state CREDIT / ACK progress     |

The two domains share the same DeModFrame type space (normally CTRL).
Demultiplexing is performed by examining the first byte of the payload
(see §4).

---

## 4. Payload Identification

All Multi-Control payloads begin with a distinguishing pattern so they
cannot be confused with classic Pipe control messages or other adapters.

```
Byte 0, bits 7–6 : 11b   (Multi-Control magic)  →  byte0 >= 0xC0
```

Classic Pipe control messages use first-byte values 0–5 (top 2 bits = `00b`).
Audio CTRL descriptors use a first-byte `payload_len` ≤ 124 (top 2 bits ≤ `01b`).
The high two bits being set (`11b`) is therefore a clean discriminator.

---

## 5. Multi-Control Message Format

Total size: exactly 4 bytes (one DeModFrame quantum payload). Big-endian,
byte-aligned — every field starts at a byte boundary.

### 5.1 Header Byte (Byte 0)

```
Bits 7–6 : 11b          Magic (Multi-Control)
Bits 5–4 : count        Number of commands present (1–3)
Bits 3–0 : flags        Reserved (must be zero in v0.1)
```

Header byte value = `0xC0 | (count << 4) | flags`. For `count` 1/2/3 with
`flags=0`: byte 0 = `0xD0` / `0xE0` / `0xF0`.

### 5.2 Command Slots (Bytes 1–3)

Each command occupies exactly one byte (byte-aligned). The maximum number
of commands is therefore **3** (bytes 1, 2, 3), not 4 — the cost of strict
byte alignment.

```
Command byte layout (8 bits, bits 7-2 used, bits 1-0 zero-padded):

  Bits 7–6 : local_idx     (0–3)  — which Active Set member
  Bits 5–3 : opcode        (see §6)
  Bit  2   : param_lsb     — low bit of parameter (depends on opcode)
  Bits 1–0 : zero pad      — must be zero, rejected on decode if nonzero
```

When `count < 3`, the higher-numbered command bytes MUST be zero.
The cert verifies this; nonzero unused slots are rejected on unpack.

### 5.3 Full 4-Byte Layout

```
 Byte 0         Byte 1 (cmd[0])    Byte 2 (cmd[1])    Byte 3 (cmd[2])
┌────┬────┬────┐┌──┬───┬──┬──┐    ┌──┬───┬──┬──┐    ┌──┬───┬──┬──┐
│ 11 │cnt │flg ││idx│opc│p │00│    │idx│opc│p │00│    │idx│opc│p │00│
└────┴────┴────┘└──┴───┴──┴──┘    └──┴───┴──┴──┘    └──┴───┴──┴──┘
  2b   2b   4b   2b 3b 1b 2b       2b 3b 1b 2b       2b 3b 1b 2b
```

---

## 6. Opcodes

| Opcode | Name          | Meaning                                      | Parameter interpretation          |
|--------|---------------|----------------------------------------------|-----------------------------------|
| 000    | NOP           | No operation (padding / reserved)            | ignored                           |
| 001    | CREDIT_DELTA  | Add `n` to the pipe's credit budget          | 1-bit selects small/large step    |
| 010    | ACK_CUMUL     | Cumulative ACK up to a recent base           | 1-bit selects which of two bases  |
| 011    | ACK_SELECTIVE | Selective ACK of one additional chunk        | 1-bit selects which of two        |
| 100    | NACK_ONE      | NACK a single missing chunk                  | 1-bit selects which of two        |
| 101    | DONE_HINT     | Receiver believes transfer is complete       | ignored (full DONE still required)|
| 110    | ABORT_HINT    | Request orderly abort                        | ignored (full ABORT still required)|
| 111    | reserved      | Must NOT be sent in v0.1 (rejected)          | —                                 |

### 6.1 Parameter Expansion (v0.1)

Because only one parameter bit is available per command in the byte-aligned
layout, the two endpoints maintain a small amount of shared context
per active pipe:

- Last credit value granted
- Two most recent cumulative ACK points
- Two most recently observed missing chunk sequence numbers

The single parameter bit selects among these contextual values.
This keeps the wire encoding tiny while still allowing useful progress.

**v0.1 scope:** The codec packs/unpacks the 1-bit `param_lsb` raw. Resolving
it against per-pipe context is a runtime layer, not byte-certified (like
HydraPack's delta context or audio's jitter buffer).

---

## 7. Interaction with Classic Control Messages

- **OPEN** — always uses the classic 14-byte format. After a successful
  OPEN, the receiver (or a negotiated rule) may place the new session
  into the Active Set.
- **Large NACK lists, full SACK bitmaps, ABORT with reason, final DONE** —
  continue to use classic single-session messages.
- Multi-Control is intended only for the high-frequency steady-state
  operations (credit top-ups and lightweight ACKs).

An implementation may freely interleave classic messages and
Multi-Control messages on the same CTRL channel.

---

## 8. Loss and Reliability Considerations

- A lost Multi-Control quantum affects only the commands it carried.
  Because the underlying Pipe reliability model is already ARQ + FEC
  on the data plane, the consequence is temporary under-crediting or
  delayed ACK progress, not data loss.
- Endpoints must not assume that a Multi-Control command has been
  received until a later observable effect (or an explicit classic
  ACK) confirms it.
- The Active Set mapping itself should be soft state and recoverable
  from classic messages.

---

## 9. Certification

Multi-Control is certified by a finite set of golden vectors that
cover three families:

1. **Main** — count sweep (1–3), all 7 legal opcodes, param LSB sweep,
   local_idx sweep, mixed-opcode clusters. Pack → compare bytes;
   unpack round-trip.
2. **Reject** — illegal buffers (reserved opcode, bad count, bad magic,
   nonzero flags, nonzero pad bits, nonzero unused slots) that must
   raise on unpack.
3. **Discriminator** — `is_multicontrol` over Multi-Control vectors,
   classic Pipe messages, and audio descriptor bytes.

Reference codecs in C, Rust, and Python match the vectors exactly.
The same formal stance used by the wire quantum and by DCF-Pipe
control messages applies.

```sh
python3 python/MCP/gen_pipemulti_vectors.py /tmp/mc.json         # regen + verify laws
cd codec && cargo test --test certify_pipemulti                  # Rust
gcc -std=c11 -I codec C_SDK/tests/test_pipemulti_certify.c -o /tmp/mc && /tmp/mc  # C
```

| Artifact | Role |
|----------|------|
| `Documentation/pipemulti_vectors.json` (= `python/MCP/`) | main + reject + discriminator vectors |
| `codec/pipemulti_vectors.gen.h` | same vectors as a C header (dependency-free C test) |
| `python/MCP/pipemulti_core.py` | Python reference (canonical) |
| `python/MCP/gen_pipemulti_vectors.py` | executable laws + vector generator |
| `codec/demod_pipemulti.h` | C header-only reference |
| `codec/src/pipemulti.rs` | Rust reference |

The 246-vector wire certificate and `pipe_vectors.json` are both untouched.

---

## 10. Non-Goals

- Multi-Control does not carry data-plane payload.
- Multi-Control does not replace the classic control messages for
  session lifetime events.
- Multi-Control does not attempt to encode arbitrary credit values
  or arbitrary chunk sequence numbers in v0.1 (context + 1-bit
  selection is used instead).
- Active-Set management (ADD/REMOVE messages) is deferred to a future
  version; v0.1 manages the mapping out-of-band.
- Dynamic resizing of the Active Set beyond three members is out of
  scope (the cost of byte alignment).
- Encryption or authentication of control remains the responsibility
  of lower layers.

---

## 11. Versioning

- The magic bits `11b` and the opcode assignments defined here
  constitute version 0.1.
- Future versions may reclaim header flag bits, increase parameter
  width, or add new opcodes while remaining distinguishable by the
  magic field.

---

## 12. Summary

DCF-Pipe Multi-Control turns the scarce 4-byte quantum payload into
a parallel control bus capable of steering up to three concurrent
lossless pipes. It leaves the high-throughput data plane, the
original control messages, and the DeModFrame invariant untouched.
The encoding is byte-aligned, fixed-slot, and golden-vector friendly
so that it can be implemented and certified with the same discipline
already applied to the rest of Punctim.

---

*End of DCF-Pipe Multi-Control Specification*