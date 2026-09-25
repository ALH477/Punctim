# DeMoD 17-Byte Communication Framework (DCF)

**Minecraft Redstone Reference Implementation**

Version 3.1.0 | License: LGPL-3.0 | Minecraft 1.21.x

> **Demonstration only — NOT a DCF wire-quantum implementation.** This datapack's
> 17-byte frame (`"RDCF"` magic, version `0x4601`, a 1-byte checksum `0x01`) has **no
> `0xD3` sync byte, no version nibble and no CRC-16/CCITT-FALSE**, so it is
> **non-conforming** to [`Documentation/WIRE_QUANTUM_SPEC.md`](../Documentation/WIRE_QUANTUM_SPEC.md)
> and fails the frame gate (`punctim decode 5254444346010000000000000000000001` →
> `invalid (bad sync byte)`, exit 5). It is not certified against
> `golden_vectors.json` and is not a binding. "Reference implementation" and
> "normative" below (and in [`protocol.md`](protocol.md)) describe this redstone demo's
> own rules, not DCF's.

---

## Overview

The DCF is a deterministic, time-framed analog communication protocol with physical validation. This repository contains a Python datapack generator that creates a complete Minecraft redstone implementation demonstrating:

- **Analog signal encoding** via comparator signal strength
- **Physical authentication** using subtract-mode comparison
- **Hardware fault injection** where mismatches destroy the circuit
- **Jitter buffering** for timing normalization

This is a reference implementation—intentionally explicit to be readable as a real protocol specification.

---

## Quick Start

```bash
# Generate the datapack
python generate_dcf_datapack.py

# Copy to your Minecraft world
cp -r dcf_protocol_v3 ~/.minecraft/saves/YOUR_WORLD/datapacks/

# In Minecraft:
/reload
/tp @s 0 100 0
/fill -50 90 -50 50 200 300 air
/function dcf:install
```

---

## Protocol Specification

### Frame Structure

| Field | Bytes | Description |
|-------|-------|-------------|
| Magic | 4 | `0x52544443` ("RDCF") |
| Version | 2 | `0x4601` (v1.70) |
| Reserved | 10 | Padding (zeros) |
| Checksum | 1 | `0x01` |

**Total: 17 bytes (fixed)**

### Encoding

Each byte is split into two nibbles (high first), yielding 34 symbols per frame.

```
Byte 0x52 → Nibble 5, Nibble 2
Byte 0x54 → Nibble 5, Nibble 4
...
```

### Protocol Header (Hex)

```
5254444346010000000000000000000001
```

### Nibble Sequence

```
[5, 2, 5, 4, 4, 4, 4, 3, 4, 6, 0, 1,
 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
 0, 0, 0, 0, 0, 0, 0, 0,
 0, 1]
```

---

## Analog Signal Model

### Comparator Mechanics

Minecraft comparators output signal strength 0-15 based on container fill level:

```
signal = 0                              (if empty)
signal = floor(14 × items / capacity)   (if items > 0)
```

For barrels: `capacity = 9 slots × 64 items = 576`

### Signal-to-Items Table

| Signal | Items Required | Formula |
|--------|----------------|---------|
| 0 | 0 | Empty barrel |
| 1 | 1 | Any item triggers |
| 2 | 42 | ceil((2-1) × 576 / 14) |
| 3 | 83 | ceil((3-1) × 576 / 14) |
| 4 | 124 | ceil((4-1) × 576 / 14) |
| 5 | 165 | ceil((5-1) × 576 / 14) |
| 6 | 206 | ceil((6-1) × 576 / 14) |
| 7 | 247 | ceil((7-1) × 576 / 14) |
| 8 | 288 | ceil((8-1) × 576 / 14) |
| 9 | 330 | ceil((9-1) × 576 / 14) |
| 10 | 371 | ceil((10-1) × 576 / 14) |
| 11 | 412 | ceil((11-1) × 576 / 14) |
| 12 | 453 | ceil((12-1) × 576 / 14) |
| 13 | 494 | ceil((13-1) × 576 / 14) |
| 14 | 535 | ceil((14-1) × 576 / 14) |

**Signal 15 is not achievable** with comparator output—maximum is 14 (full container).

### Formula Derivation

Forward: `signal = floor(1 + (items / capacity) × 14)` for items > 0

Inverse: `items = ceil((signal - 1) × capacity / 14)` for signal >= 2

Special case: Signal 1 only requires 1 item (any non-empty container outputs 1).

---

## System Architecture

```
┌─────────┐    ┌─────────────┐    ┌─────────────┐    ┌────────┐    ┌─────────┐
│  Clock  │───▶│  TX Layer   │───▶│ Auth Layer  │───▶│ Buffer │───▶│ Decoder │
│ (BORE)  │    │ (34 stages) │    │ (subtract)  │    │ (FIFO) │    │         │
└─────────┘    └─────────────┘    └─────────────┘    └────────┘    └─────────┘
```

### Layer Descriptions

| Layer | Function | Z Range |
|-------|----------|---------|
| Clock | 4-tick oscillator | 0-10 |
| TX | Barrel→comparator signal generation | 10-112 |
| Auth | Subtract comparison + fault injection | 130-232 |
| Buffer | Hopper FIFO timing normalization | 230-245 |
| Decoder | Threshold detection + payload | 245-260 |

---

## Layer Details

### 1. BORE Clock

The Basic Oscillator for Redstone Events generates a stable pulse train:

```
Lever → Repeater (2t) → Wire → Repeater (2t) → Observer → Output
                ↑                                    │
                └────────────── Feedback ────────────┘
```

**Period:** 4 game ticks (200ms at 20 TPS)

### 2. TX Layer

Each of 34 stages encodes one nibble:

```
┌────────┐
│ Barrel │──▶ Comparator ──▶ Repeater ──▶ Target (bus)
└────────┘    (compare)       (2 ticks)
     ↑
   Items = signal_to_items(nibble)
```

The barrel fill determines comparator output. Repeaters isolate stages and maintain signal integrity.

### 3. Auth Layer

Physical validation using subtraction:

```
TX Signal ──▶ Comparator ──▶ Torch ──▶ Piston ──▶ Output
              (subtract)     (invert)   │
Reference ───────┘                      ▼
Barrel                              [Gravel]
```

**How it works:**

1. Subtract comparator: `output = max(0, TX_signal - reference_signal)`
2. If TX matches reference: output = 0 → torch ON → piston retracted
3. If mismatch: output > 0 → torch OFF → piston extends → gravel breaks bus

**This is hardware enforcement**—wrong signals physically destroy the circuit.

### 4. Jitter Buffer

Absorbs timing variations using hopper FIFO:

```
Observer ──▶ Dropper ──▶ [Hopper Chain] ──▶ Comparator ──▶ Dropper
 (edge)      (input)      (8 hoppers)       (detect)      (output)
                               │                              ↑
                               └──────── Items Flow ──────────┘
```

Items are inserted on signal edges and released at a fixed rate, decoupling TX timing from decoder timing.

### 5. Decoder

Threshold detection extracts protocol signals:

| Signal | Meaning | Action |
|--------|---------|--------|
| ≥14 | RESET | Clear state |
| 10 | PAYLOAD | Deliver reward |
| ≥1 | HEARTBEAT | Light indicator |

Wire decay is used for threshold filtering—4 blocks of wire reduces signal by 4.

---

## World Setup

### Prerequisites

- Minecraft Java Edition 1.21.x
- Creative mode
- Superflat world recommended

### Recommended Superflat Preset

```
minecraft:bedrock,2*minecraft:dirt,minecraft:grass_block;minecraft:plains;village
```

### Game Rules

```
/gamemode creative
/gamerule randomTickSpeed 0
/gamerule doDaylightCycle false
/gamerule doMobSpawning false
/gamerule commandBlockOutput false
```

### Build Area

```
/tp @s 0 100 0
/fill -50 90 -50 50 200 300 air
```

---

## Installation

### Automated (Datapack)

```bash
python generate_dcf_datapack.py
```

Copy the generated `dcf_protocol_v3` folder to your world's datapacks directory:

```
.minecraft/saves/YOUR_WORLD/datapacks/dcf_protocol_v3/
```

In-game:

```
/reload
/function dcf:install
```

### Manual (Reference)

See the generated `.mcfunction` files for exact block placements.

---

## Testing

### Normal Operation

1. Flip the lever at the clock
2. Observe pulses propagating through TX layer
3. Watch auth layer pass all 34 stages
4. Receive diamond at decoder (payload confirmation)
5. "SIGNAL ACTIVE" lamp illuminates

### Tamper Detection

```
/function dcf:test_tamper
```

This removes 1 item from TX stage 0. The next transmission will:

1. Output wrong signal strength at stage 0
2. Auth layer detects mismatch
3. Piston extends, gravel falls
4. Signal bus physically breaks
5. Payload never delivered

### Reset After Tamper

```
/function dcf:test_reset
```

Restores the tampered barrel to correct fill level.

---

## Datapack Functions

| Function | Description |
|----------|-------------|
| `dcf:load` | Initialization (runs on load) |
| `dcf:install` | Full installation |
| `dcf:install_clock` | Clock only |
| `dcf:install_tx` | TX layer only |
| `dcf:install_auth` | Auth layer only |
| `dcf:install_buffer` | Jitter buffer only |
| `dcf:install_decoder` | Decoder only |
| `dcf:install_panel` | Control panel only |
| `dcf:install_forceload` | Chunk loading |
| `dcf:test_tamper` | Inject fault |
| `dcf:test_reset` | Restore barrel |
| `dcf:test_signals` | Verify signal table |

---

## Known Limitations

1. **Fixed frame length:** 17 bytes only
2. **No error correction:** Single bit errors cause rejection
3. **Chunk dependencies:** Requires forceload for SMP
4. **Timing constraints:** Must complete within chunk tick budget
5. **No bidirectional:** TX→RX only, no acknowledgment

---

## Troubleshooting

### "Auth layer immediately breaks"

- Check barrel fill counts match exactly
- Verify comparators are in subtract mode (right-click to toggle)
- Ensure repeater delays are correct

### "No signal propagation"

- Verify clock is running (lamp should blink)
- Check for broken redstone wire
- Ensure chunks are forceloaded

### "Payload not delivered"

- Confirm all 34 auth stages pass
- Check decoder comparator orientations
- Verify wire decay path length

### "Inconsistent behavior"

- Set `randomTickSpeed 0`
- Avoid building across chunk boundaries without forceload
- Check for observer timing issues

---

## Design Philosophy

This implementation prioritizes **clarity over compactness**:

- Each concept maps to distinct blocks
- No compressed/obfuscated redstone
- Comments explain every stage
- Failure modes are visible

It demonstrates that Minecraft redstone can enforce protocol rules **physically**, not just logically. A wrong signal doesn't trigger an error message—it destroys the hardware.

---

## Extending the Protocol

### Custom Payloads

Modify `generate_decoder()` to add threshold detectors:

```python
# Add detector for signal 8
f"setblock ~X ~Y ~Z minecraft:comparator[facing=east,mode=compare]",
f"setblock ~X ~Y ~Z-1 minecraft:barrel[facing=north]{barrel_nbt(8)}",
```

### Variable Frame Length

Adjust `NIBBLE_SEQUENCE` and regenerate. The auth layer automatically scales.

### Multi-Channel

Duplicate TX/Auth layers at different X offsets. Each channel operates independently.

---

## License

LGPL-3.0

You may study, modify, and redistribute this implementation provided changes are documented and the license preserved.

---

## Credits

DeMoD Communications Framework  
DeMoD LLC

**17 bytes. One clock. No forgiveness.**
