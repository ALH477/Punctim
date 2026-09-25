## 3A. Formal Protocol Specification (Normative)

> **Scope note.** "Normative" here applies only to this Minecraft redstone demo
> (D17BCP). It is **not** the DCF wire quantum: the frame below has no `0xD3` sync,
> no version nibble and no CRC-16, so it is non-conforming to
> [`WIRE_QUANTUM_SPEC.md`](../Documentation/WIRE_QUANTUM_SPEC.md) and uncertified.
> See [`README.md`](README.md).

### Status of This Specification

This section defines the **DeMoD 17-Byte Communication Protocol (D17BCP)**.
It is a **normative description** of the protocol semantics.
The Minecraft redstone build is a **reference implementation**, not the protocol itself.

---

## 3A.1 Definitions

* **Symbol**: A single transmitted unit representing a 4-bit nibble.
* **Frame**: A complete transmission of 34 symbols.
* **Frame Window**: The fixed time interval allocated to exactly one symbol.
* **Signal Strength (S)**: Integer value in range `0 ≤ S ≤ 14`.
* **Valid Frame**: A frame whose symbols exactly match the reference header.

---

## 3A.2 Frame Structure

### Frame Length

* **Fixed**
* Exactly **34 symbols**

### Symbol Order

* Big-endian nibble order:

  * High nibble first
  * Low nibble second

### Payload

* Fixed header value:

```
0x5254444346010000000000000000000001
```

### Expanded Symbol Sequence

```
Index:  0  1  2  3  4  5  6  7  8  9 10 11 ...
Value:  5, 2, 5, 4, 4, 4, 4, 3, 4, 6, 0, 1, ...
```

---

## 3A.3 Symbol Encoding

Each symbol is encoded as an **analog amplitude value**.

### Signal Domain

| Property | Value       |
| -------- | ----------- |
| Minimum  | 0           |
| Maximum  | 14          |
| Reserved | 15 (unused) |

### Encoding Rule

```
SymbolValue == SignalStrength
```

There is a **direct 1:1 mapping** between nibble value and transmitted amplitude.

No compression, scaling, or modulation beyond amplitude selection is used.

---

## 3A.4 Timing Model

### Clocking

* Global, synchronous clock
* **Fixed cycle**
* No asynchronous transitions

### Frame Window

Each symbol occupies exactly one **Frame Window**.

**Normative properties**:

* Symbols MUST NOT overlap
* Only one symbol MAY be valid per window
* Gaps between windows are permitted but ignored

### Reference Implementation Timing

| Property         | Value        |
| ---------------- | ------------ |
| Clock period     | 3 game ticks |
| Symbol high time | 2 ticks      |
| Symbol low time  | 1 tick       |

Other implementations MAY use different timing, provided the window is fixed and deterministic.

---

## 3A.5 Transmission Rules

1. Symbols MUST be transmitted sequentially.
2. No symbol MAY be skipped.
3. No symbol MAY be repeated.
4. A frame is invalid if:

   * A symbol amplitude differs
   * A symbol arrives outside its window
   * Extra symbols are present

There is **no partial-frame validity**.

---

## 3A.6 Authentication Model

Authentication is **deterministic and exact**.

### Acceptance Criteria

A frame is **accepted** if and only if:

```
∀ i ∈ [0..33], ReceivedSymbol[i] == ReferenceSymbol[i]
```

### Rejection Semantics

* Any mismatch causes **immediate frame rejection**
* Rejection is **terminal** for that frame
* No recovery or correction is attempted

Authentication failure MUST prevent:

* Payload execution
* Downstream signaling
* State mutation

---

## 3A.7 Buffering Semantics

The protocol is **logically unbuffered**.

However, implementations MAY include buffering layers provided that:

* Symbol order is preserved
* Symbol values are unchanged
* Frame boundaries remain intact

The reference implementation includes a FIFO buffer solely to compensate for environmental jitter.

---

## 3A.8 Error Handling

### Defined Errors

| Error           | Condition                   |
| --------------- | --------------------------- |
| Amplitude Error | Symbol value mismatch       |
| Timing Error    | Symbol outside frame window |
| Length Error    | Symbol count ≠ 34           |

### Error Response

* Frame is discarded
* No partial acceptance
* No retry mechanism defined by protocol

Recovery is **out of scope**.

---

## 3A.9 Security Considerations

This protocol provides:

✔ Deterministic validation
✔ Tamper detection

It does **not** provide:

✘ Confidentiality
✘ Cryptographic integrity
✘ Replay protection
✘ Resistance to active adversaries

Any security claims are **out of scope**.

---

## 3A.10 Extensibility

The protocol is intentionally rigid.

Possible future extensions (not defined here):

* Variable-length frames
* Framing markers
* Payload multiplexing
* Checksum symbols

Such extensions MUST NOT reinterpret existing symbols.

---

## 3A.11 Reference Implementation Mapping

| Protocol Concept | Redstone Mechanism              |
| ---------------- | ------------------------------- |
| Symbol           | Comparator signal strength      |
| Frame window     | Clocked repeater cycle          |
| Serializer       | Unary advance TX chain          |
| Authentication   | Subtractor comparator + breaker |
| Rejection        | Physical bus severing           |
| Buffer           | Hopper/dropper FIFO             |

This mapping is **illustrative**, not normative.

---

## 3A.12 Compliance

An implementation is **D17BCP-compliant** if it:

1. Transmits exactly 34 symbols
2. Uses amplitudes 0–14 only
3. Preserves symbol order
4. Rejects on first mismatch
5. Prevents payload execution on failure

---

### End of Protocol Specification
