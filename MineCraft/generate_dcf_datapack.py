#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""
DeMoD 17-Byte Communication Framework (DCF)
Minecraft Redstone DEMO — Datapack Generator

DEMONSTRATION ONLY — NOT a DCF wire-quantum implementation. The 17-byte pattern below
("RDCF" magic, version 0x4601, 1-byte checksum 0x01) has no 0xD3 sync, no version nibble
and no CRC-16/CCITT-FALSE, so it is non-conforming to Documentation/WIRE_QUANTUM_SPEC.md,
uncertified, and has no input path (TX and Auth are the same baked constant). The
conforming register — datapack generator, `punctim mc` sidecar, Paper plugin, Fabric mod,
Bedrock bridge — is minecraft/ (Documentation/DCF_MINECRAFT_SPEC.md).

Version: 3.1.0
License: LGPL-3.0-only
Author: DeMoD LLC

This generator creates a complete Minecraft datapack implementing the DCF protocol
using redstone mechanics for physical signal validation.

PROTOCOL SUMMARY:
- 17 bytes encoded as 34 nibbles (high nibble first)
- Each nibble → comparator signal strength (0-15)
- TX layer transmits, Auth layer validates via subtraction
- Jitter buffer normalizes timing, Decoder extracts payload

CRITICAL FORMULAS:
- Barrel capacity: 9 slots × 64 items = 576 items
- Signal strength: floor(14 × items / 576) for items > 0, else 0
- Items needed: ceil(signal × 576 / 14) for signal > 1, else 1 for signal 1
"""

import os
from math import floor, ceil
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

DATAPACK_NAME = "dcf_protocol_v3"
PACK_FORMAT = 48  # Minecraft 1.21.x
PACK_DESCRIPTION = "DCF v3.1 redstone DEMO (non-conforming RDCF pattern; see minecraft/ for the real register)"

# World Origin (datapack installs relative to execution position)
# Recommended: Run from a flat area at Y=4 with ~300 blocks Z clearance

# Tower Dimensions
TOWER_WIDTH = 17
TOWER_DEPTH = 17
TOWER_HEIGHT = 260

# Protocol: 17 bytes = 34 nibbles
# Header: 0x5254444346010000000000000000000001
# "RDCF" + version + padding + checksum
PROTOCOL_HEADER = bytes.fromhex("5254444346010000000000000000000001")

def bytes_to_nibbles(data: bytes) -> list[int]:
    """Convert bytes to nibble sequence (high nibble first)."""
    nibbles = []
    for byte in data:
        nibbles.append((byte >> 4) & 0x0F)  # High nibble
        nibbles.append(byte & 0x0F)          # Low nibble
    return nibbles

NIBBLE_SEQUENCE = bytes_to_nibbles(PROTOCOL_HEADER)

# Timing Constants (game ticks)
CLOCK_PERIOD = 4        # Ticks per clock cycle
STAGE_DELAY = 2         # Inter-stage propagation
BUFFER_DEPTH = 8        # Jitter buffer item capacity

# Layout Constants
STAGE_HEIGHT = 6        # Vertical spacing per nibble stage
TX_START_Z = 10         # TX layer Z origin
AUTH_START_Z = 130      # Auth layer Z origin
BUFFER_Z = 230          # Jitter buffer Z
DECODER_Z = 245         # Decoder Z

# ═══════════════════════════════════════════════════════════════════════════════
# COMPARATOR SIGNAL MATH
# ═══════════════════════════════════════════════════════════════════════════════

BARREL_SLOTS = 9
SLOT_CAPACITY = 64
BARREL_CAPACITY = BARREL_SLOTS * SLOT_CAPACITY  # 576

def signal_to_items(signal: int) -> int:
    """
    Calculate minimum items needed for exact comparator signal strength.
    
    Minecraft formula: signal = floor(1 + (items / capacity) × 14) if items > 0
                       signal = 0 if items = 0
    
    Inverse: items = ceil((signal - 1) × capacity / 14) for signal >= 2
    Special: signal 1 requires only 1 item (any non-empty container)
    """
    if signal < 0 or signal > 14:
        raise ValueError(f"Signal strength must be 0-14, got {signal}")
    if signal == 0:
        return 0
    if signal == 1:
        return 1  # Any non-empty container gives signal 1
    # For signal >= 2: items = ceil((signal - 1) × capacity / 14)
    return int(ceil((signal - 1) * BARREL_CAPACITY / 14))

def items_to_signal(items: int) -> int:
    """Verify: calculate signal from item count using Minecraft's formula."""
    if items <= 0:
        return 0
    # signal = floor(1 + (items / capacity) × 14)
    return min(14, floor(1 + (items / BARREL_CAPACITY) * 14))

def validate_signal_table():
    """Generate and validate the signal-to-items mapping."""
    print("Signal Strength → Item Count Mapping:")
    print("=" * 40)
    for sig in range(15):  # 0-14 only (15 not achievable)
        items = signal_to_items(sig)
        verify = items_to_signal(items)
        status = "✓" if verify == sig else f"✗ (got {verify})"
        print(f"  Signal {sig:2d} → {items:3d} items {status}")
    print("  Signal 15 → N/A (not achievable with comparator)")
    print()

# ═══════════════════════════════════════════════════════════════════════════════
# NBT GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def barrel_nbt(signal: int, item_id: str = "minecraft:stone") -> str:
    """
    Generate NBT for a barrel with exact item count for target signal.
    
    Returns empty string for signal 0 (empty barrel).
    """
    items_needed = signal_to_items(signal)
    if items_needed == 0:
        return ""
    
    # Distribute items across slots (max 64 per slot)
    item_entries = []
    remaining = items_needed
    slot = 0
    
    while remaining > 0 and slot < BARREL_SLOTS:
        stack = min(64, remaining)
        item_entries.append(f'{{Slot:{slot}b,id:"{item_id}",count:{stack}}}')
        remaining -= stack
        slot += 1
    
    return f'{{Items:[{",".join(item_entries)}]}}'

def command_block_nbt(command: str, block_type: str = "impulse", 
                      auto: bool = False, conditional: bool = False,
                      facing: str = "east") -> str:
    """Generate NBT for command blocks."""
    # Escape quotes in command
    escaped_cmd = command.replace('\\', '\\\\').replace('"', '\\"')
    nbt_parts = [f'Command:"{escaped_cmd}"']
    
    if auto:
        nbt_parts.append("auto:1b")
    if conditional:
        nbt_parts.append("conditional:1b")
    
    return "{" + ",".join(nbt_parts) + "}"

# ═══════════════════════════════════════════════════════════════════════════════
# FILE UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def mkdir(path: str):
    """Create directory and parents if needed."""
    os.makedirs(path, exist_ok=True)

def write_file(path: str, content: str):
    """Write content to file with UTF-8 encoding."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def mcfunction(lines: list[str]) -> str:
    """Join lines into mcfunction format with comment preservation."""
    return "\n".join(lines) + "\n"

# ═══════════════════════════════════════════════════════════════════════════════
# DATAPACK STRUCTURE
# ═══════════════════════════════════════════════════════════════════════════════

class DatapackBuilder:
    def __init__(self, name: str):
        self.name = name
        self.base_path = name
        self.functions_path = os.path.join(self.base_path, "data", "dcf", "function")
        self.tags_path = os.path.join(self.base_path, "data", "minecraft", "tags", "function")
        
    def setup_directories(self):
        """Create datapack directory structure."""
        mkdir(self.functions_path)
        mkdir(self.tags_path)
        
    def write_pack_mcmeta(self):
        """Write pack.mcmeta with format and description."""
        content = f'''{{
    "pack": {{
        "pack_format": {PACK_FORMAT},
        "description": "{PACK_DESCRIPTION}"
    }}
}}'''
        write_file(os.path.join(self.base_path, "pack.mcmeta"), content)
    
    def write_load_tag(self):
        """Write load.json function tag."""
        content = '{"values": ["dcf:load"]}'
        write_file(os.path.join(self.tags_path, "load.json"), content)
    
    def write_function(self, name: str, lines: list[str]):
        """Write a .mcfunction file."""
        path = os.path.join(self.functions_path, f"{name}.mcfunction")
        write_file(path, mcfunction(lines))

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

def generate_load_function() -> list[str]:
    """Generate initialization function called on datapack load."""
    return [
        "# DCF Protocol — Initialization",
        f"# Generated: {datetime.now().isoformat()}",
        "",
        "# Create scoreboard objectives",
        'scoreboard objectives add dcf_state dummy "DCF State"',
        'scoreboard objectives add dcf_debug dummy "DCF Debug"',
        "",
        "# Set game rules for reliable redstone",
        "gamerule commandBlockOutput false",
        "gamerule randomTickSpeed 0",
        "gamerule doMobSpawning false",
        "",
        "# Notify load complete",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Protocol v3.1 Loaded","color":"green"}]',
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Run: ","color":"gray"},{"text":"/function dcf:install","color":"aqua","clickEvent":{"action":"suggest_command","value":"/function dcf:install"}}]',
    ]

def generate_clock() -> list[str]:
    """
    Generate BORE (Basic Oscillator for Redstone Events) clock.
    
    Layout at Z=0-8:
    - Lever at ~0 ~4 ~0 (master enable)
    - Repeater chain creates stable 4-tick pulse
    - Observer detects edges for downstream sync
    - Output bus at ~0 ~5 ~8 feeds TX layer
    """
    lines = [
        "# DCF BORE Clock — Master Oscillator",
        "# Generates stable 4-tick pulse train",
        "",
        "# Clear clock area",
        "fill ~-1 ~3 ~-1 ~3 ~7 ~10 minecraft:air",
        "fill ~-1 ~3 ~-1 ~3 ~3 ~10 minecraft:smooth_stone",
        "",
        "# Clock core: repeater loop with observer edge detection",
        "# Stage 1: Input from lever",
        "setblock ~0 ~4 ~0 minecraft:lever[face=floor,facing=north]",
        "setblock ~0 ~4 ~1 minecraft:redstone_wire[east=side,west=side]",
        "",
        "# Stage 2: Repeater chain (total delay = 4 ticks)",
        "setblock ~0 ~4 ~2 minecraft:repeater[facing=south,delay=2]",
        "setblock ~0 ~4 ~3 minecraft:redstone_wire[north=side,south=side]",
        "setblock ~0 ~4 ~4 minecraft:repeater[facing=south,delay=2]",
        "",
        "# Stage 3: Observer edge detector",
        "setblock ~0 ~4 ~5 minecraft:redstone_wire[north=side,south=side]",
        "setblock ~0 ~5 ~5 minecraft:observer[facing=down]",
        "",
        "# Stage 4: Output isolation repeater",
        "setblock ~0 ~4 ~6 minecraft:repeater[facing=south,delay=1]",
        "setblock ~0 ~4 ~7 minecraft:redstone_wire[north=side,south=side]",
        "",
        "# Stage 5: Feedback loop (creates oscillation)",
        "setblock ~1 ~4 ~7 minecraft:redstone_wire[west=side,north=side]",
        "setblock ~1 ~4 ~6 minecraft:redstone_wire[south=side,north=side]",
        "setblock ~1 ~4 ~5 minecraft:redstone_wire[south=side,north=side]",
        "setblock ~1 ~4 ~4 minecraft:redstone_wire[south=side,north=side]",
        "setblock ~1 ~4 ~3 minecraft:redstone_wire[south=side,north=side]",
        "setblock ~1 ~4 ~2 minecraft:redstone_wire[south=side,west=side]",
        "",
        "# Output bus to TX layer",
        "setblock ~0 ~4 ~8 minecraft:repeater[facing=south,delay=1]",
        "setblock ~0 ~5 ~8 minecraft:redstone_lamp",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Clock installed. Flip lever to start.","color":"yellow"}]',
    ]
    return lines

def generate_tx_layer() -> list[str]:
    """
    Generate TX (Transmitter) layer — 34 nibble stages.
    
    Each stage:
    - Barrel filled for target signal strength
    - Comparator reads barrel → outputs signal
    - Repeater isolates and propagates to bus
    - Clock input triggers sequential readout
    
    Layout: X=0-6, Y=4-210, Z=10-112
    """
    lines = [
        "# DCF TX Layer — 34 Nibble Transmitter",
        f"# Protocol header: {PROTOCOL_HEADER.hex().upper()}",
        "",
        "# Clear TX area",
        f"fill ~-1 ~3 ~{TX_START_Z-1} ~8 ~{4 + len(NIBBLE_SEQUENCE) * STAGE_HEIGHT} ~{TX_START_Z + 3} minecraft:air",
        f"fill ~-1 ~3 ~{TX_START_Z-1} ~8 ~3 ~{TX_START_Z + 3} minecraft:smooth_stone",
        "",
    ]
    
    for idx, nibble in enumerate(NIBBLE_SEQUENCE):
        y_base = 4 + idx * STAGE_HEIGHT
        z = TX_START_Z
        nbt = barrel_nbt(nibble)
        
        lines.append(f"# Stage {idx:02d}: Nibble 0x{nibble:X} (signal {nibble})")
        
        # Barrel with calculated fill
        lines.append(f"setblock ~2 ~{y_base} ~{z} minecraft:barrel[facing=east]{nbt}")
        
        # Comparator reads barrel
        lines.append(f"setblock ~3 ~{y_base} ~{z} minecraft:comparator[facing=east,mode=compare]")
        
        # Isolation repeater
        lines.append(f"setblock ~4 ~{y_base} ~{z} minecraft:repeater[facing=east,delay={STAGE_DELAY}]")
        
        # Output wire to vertical bus
        lines.append(f"setblock ~5 ~{y_base} ~{z} minecraft:redstone_wire[west=side,east=side]")
        
        # Vertical bus segment (target block for signal propagation)
        lines.append(f"setblock ~6 ~{y_base} ~{z} minecraft:target")
        
        # Clock input tap (from vertical clock line)
        lines.append(f"setblock ~0 ~{y_base} ~{z} minecraft:redstone_wire[east=side]")
        lines.append(f"setblock ~1 ~{y_base} ~{z} minecraft:repeater[facing=east,delay=1]")
        
        # Debug indicator every 8 stages
        if idx % 8 == 0:
            lines.append(f"setblock ~7 ~{y_base} ~{z} minecraft:shroomlight")
        
        lines.append("")
    
    # Vertical clock distribution line
    lines.append("# Clock distribution (vertical)")
    for idx in range(len(NIBBLE_SEQUENCE)):
        y = 4 + idx * STAGE_HEIGHT
        lines.append(f"setblock ~0 ~{y} ~{TX_START_Z - 1} minecraft:redstone_wire[north=side,south=side,up=side]")
    
    lines.append("")
    lines.append('tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"TX layer installed (34 stages)","color":"green"}]')
    
    return lines

def generate_auth_layer() -> list[str]:
    """
    Generate Auth (Authentication) layer — physical validation.
    
    Each stage compares incoming TX signal against reference barrel:
    - Subtract mode comparator: outputs 0 if match, >0 if mismatch
    - Torch inverts: HIGH = match, LOW = mismatch  
    - Sticky piston + gravel: mismatches physically break the signal bus
    
    This creates HARDWARE ENFORCEMENT — wrong signals destroy the circuit.
    """
    lines = [
        "# DCF Auth Layer — Physical Validation (34 stages)",
        "# Mismatch = circuit destruction via piston/gravel",
        "",
        "# Clear auth area",
        f"fill ~10 ~3 ~{AUTH_START_Z-1} ~20 ~{4 + len(NIBBLE_SEQUENCE) * STAGE_HEIGHT} ~{AUTH_START_Z + 5} minecraft:air",
        f"fill ~10 ~3 ~{AUTH_START_Z-1} ~20 ~3 ~{AUTH_START_Z + 5} minecraft:smooth_stone",
        "",
    ]
    
    for idx, nibble in enumerate(NIBBLE_SEQUENCE):
        y_base = 4 + idx * STAGE_HEIGHT
        z = AUTH_START_Z
        nbt = barrel_nbt(nibble)
        
        lines.append(f"# Auth Stage {idx:02d}: Expect signal {nibble}")
        
        # Reference barrel (identical to TX)
        lines.append(f"setblock ~12 ~{y_base} ~{z} minecraft:barrel[facing=west]{nbt}")
        
        # Input from TX bus (signal line)
        lines.append(f"setblock ~10 ~{y_base} ~{z} minecraft:redstone_wire[east=side]")
        lines.append(f"setblock ~11 ~{y_base} ~{z} minecraft:repeater[facing=east,delay=1]")
        
        # Subtract comparator: side input from barrel, rear from TX
        # Output = max(0, rear - side) in subtract mode
        # If TX signal matches barrel, output = 0
        lines.append(f"setblock ~13 ~{y_base} ~{z} minecraft:comparator[facing=east,mode=subtract]")
        
        # Inverter torch: HIGH when comparator outputs 0 (match)
        lines.append(f"setblock ~14 ~{y_base} ~{z} minecraft:redstone_wire[west=side,east=side]")
        lines.append(f"setblock ~15 ~{y_base} ~{z} minecraft:stone")
        lines.append(f"setblock ~15 ~{y_base + 1} ~{z} minecraft:redstone_torch")
        
        # Pass-through when valid (torch HIGH = piston retracted)
        lines.append(f"setblock ~16 ~{y_base} ~{z} minecraft:redstone_wire[west=side,east=side]")
        
        # Kill mechanism: piston pushes gravel to break bus on mismatch
        lines.append(f"setblock ~17 ~{y_base + 1} ~{z} minecraft:sticky_piston[facing=down]")
        lines.append(f"setblock ~17 ~{y_base} ~{z} minecraft:gravel")  # Held by piston
        
        # Output to downstream bus
        lines.append(f"setblock ~18 ~{y_base} ~{z} minecraft:target")
        
        lines.append("")
    
    lines.append('tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Auth layer installed (physical validation)","color":"green"}]')
    
    return lines

def generate_jitter_buffer() -> list[str]:
    """
    Generate jitter buffer — timing normalization.
    
    Uses dropper/hopper FIFO to absorb timing variations:
    - Items enter dropper on signal edge
    - Hopper chain creates delay
    - Output dropper releases on independent clock
    
    This decouples TX timing from decoder timing.
    """
    lines = [
        "# DCF Jitter Buffer — Timing Normalization",
        f"# Location: Z={BUFFER_Z}",
        "",
        f"fill ~10 ~3 ~{BUFFER_Z-2} ~20 ~12 ~{BUFFER_Z+8} minecraft:air",
        f"fill ~10 ~3 ~{BUFFER_Z-2} ~20 ~3 ~{BUFFER_Z+8} minecraft:smooth_stone",
        "",
        "# Input edge detector",
        f"setblock ~12 ~4 ~{BUFFER_Z} minecraft:observer[facing=south]",
        f"setblock ~12 ~4 ~{BUFFER_Z+1} minecraft:redstone_wire[north=side,south=side]",
        "",
        "# Input dropper (items enter buffer)",
        f"setblock ~12 ~4 ~{BUFFER_Z+2} minecraft:dropper[facing=south]{{Items:[{{Slot:0,id:\"minecraft:redstone\",count:32}}]}}",
        "",
        "# Hopper chain FIFO (8 hoppers = 8 item delay)",
        f"setblock ~12 ~4 ~{BUFFER_Z+3} minecraft:hopper[facing=south]",
        f"setblock ~12 ~4 ~{BUFFER_Z+4} minecraft:hopper[facing=south]",
        f"setblock ~12 ~4 ~{BUFFER_Z+5} minecraft:hopper[facing=down]",
        f"setblock ~12 ~3 ~{BUFFER_Z+5} minecraft:hopper[facing=south]",
        f"setblock ~12 ~3 ~{BUFFER_Z+6} minecraft:hopper[facing=south]",
        f"setblock ~12 ~3 ~{BUFFER_Z+7} minecraft:hopper[facing=up]",
        "",
        "# Output comparator (detects items in final hopper)",
        f"setblock ~13 ~3 ~{BUFFER_Z+7} minecraft:comparator[facing=east,mode=compare]",
        f"setblock ~14 ~3 ~{BUFFER_Z+7} minecraft:redstone_wire[west=side,east=side]",
        "",
        "# Output dropper with independent clock",
        f"setblock ~15 ~3 ~{BUFFER_Z+7} minecraft:dropper[facing=east]",
        f"setblock ~15 ~4 ~{BUFFER_Z+7} minecraft:observer[facing=down]",
        f"setblock ~15 ~5 ~{BUFFER_Z+7} minecraft:redstone_wire[north=side,south=side]",
        "",
        "# Release clock (6-tick period)",
        f"setblock ~14 ~5 ~{BUFFER_Z+7} minecraft:repeater[facing=east,delay=3]",
        f"setblock ~13 ~5 ~{BUFFER_Z+7} minecraft:redstone_wire[east=side,west=side]",
        f"setblock ~12 ~5 ~{BUFFER_Z+7} minecraft:repeater[facing=east,delay=3]",
        f"setblock ~16 ~5 ~{BUFFER_Z+7} minecraft:redstone_wire[west=side]",
        "",
        "# Output bus to decoder",
        f"setblock ~16 ~3 ~{BUFFER_Z+7} minecraft:target",
        "",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Jitter buffer installed","color":"green"}]',
    ]
    return lines

def generate_decoder() -> list[str]:
    """
    Generate decoder — payload extraction.
    
    Signal strength thresholds:
    - 15: RESET (clear state)
    - 10: PAYLOAD (execute action)
    - 6: HEARTBEAT (connection alive)
    - 1: SYNC (frame alignment)
    """
    # Build command strings separately to avoid escaping hell
    reset_cmd = 'scoreboard players set @a dcf_state 0'
    reset_msg_cmd = 'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"RESET received","color":"red"}]'
    give_cmd = 'give @p minecraft:diamond 1'
    sound_cmd = 'playsound minecraft:block.note_block.chime master @a ~ ~ ~ 1 2'
    payload_msg_cmd = 'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"PAYLOAD delivered!","color":"green"}]'
    
    lines = [
        "# DCF Decoder — Payload Extraction",
        f"# Location: Z={DECODER_Z}",
        "",
        f"fill ~10 ~3 ~{DECODER_Z-2} ~25 ~10 ~{DECODER_Z+10} minecraft:air",
        f"fill ~10 ~3 ~{DECODER_Z-2} ~25 ~3 ~{DECODER_Z+10} minecraft:iron_block",
        "",
        "# Input bus",
        f"setblock ~12 ~4 ~{DECODER_Z} minecraft:redstone_wire[east=side,south=side]",
        "",
        "# === RESET Detector (Signal >= 14) ===",
        f"setblock ~12 ~4 ~{DECODER_Z+1} minecraft:redstone_wire[north=side,south=side]",
        f"setblock ~12 ~4 ~{DECODER_Z+2} minecraft:comparator[facing=south,mode=compare]",
        "# Reference barrel for signal 14",
        f"setblock ~11 ~4 ~{DECODER_Z+2} minecraft:barrel[facing=east]{barrel_nbt(14)}",
        f"setblock ~12 ~4 ~{DECODER_Z+3} minecraft:repeater[facing=south,delay=1]",
        f"setblock ~12 ~4 ~{DECODER_Z+4} minecraft:command_block[facing=south]{command_block_nbt(reset_cmd)}",
        f"setblock ~12 ~4 ~{DECODER_Z+5} minecraft:chain_command_block[facing=south]{command_block_nbt(reset_msg_cmd, auto=True)}",
        "",
        "# === PAYLOAD Detector (Signal 10) ===",
        f"setblock ~14 ~4 ~{DECODER_Z} minecraft:redstone_wire[west=side,east=side]",
        "# Wire decay: 4 blocks = -4 signal, so signal 10 becomes 6",
        f"setblock ~15 ~4 ~{DECODER_Z} minecraft:redstone_wire[west=side,east=side]",
        f"setblock ~16 ~4 ~{DECODER_Z} minecraft:redstone_wire[west=side,east=side]",
        f"setblock ~17 ~4 ~{DECODER_Z} minecraft:redstone_wire[west=side,east=side]",
        f"setblock ~18 ~4 ~{DECODER_Z} minecraft:comparator[facing=east,mode=compare]",
        "# Reference barrel for signal 6 (after decay)",
        f"setblock ~18 ~4 ~{DECODER_Z-1} minecraft:barrel[facing=north]{barrel_nbt(6)}",
        f"setblock ~19 ~4 ~{DECODER_Z} minecraft:repeater[facing=east,delay=1]",
        f"setblock ~20 ~4 ~{DECODER_Z} minecraft:command_block[facing=east]{command_block_nbt(give_cmd)}",
        f"setblock ~21 ~4 ~{DECODER_Z} minecraft:chain_command_block[facing=east]{command_block_nbt(sound_cmd, auto=True)}",
        f"setblock ~22 ~4 ~{DECODER_Z} minecraft:chain_command_block[facing=east]{command_block_nbt(payload_msg_cmd, auto=True)}",
        "",
        "# === HEARTBEAT Indicator (Signal >= 1) ===",
        f"setblock ~12 ~5 ~{DECODER_Z} minecraft:comparator[facing=up,mode=compare]",
        f"setblock ~12 ~6 ~{DECODER_Z} minecraft:redstone_lamp",
        f'setblock ~11 ~6 ~{DECODER_Z} minecraft:oak_wall_sign[facing=west]{{front_text:{{messages:[\'""\',"SIGNAL","ACTIVE",\'""\']}}}}',
        "",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Decoder installed","color":"green"}]',
    ]
    return lines

def generate_control_panel() -> list[str]:
    """Generate operator control panel."""
    lines = [
        "# DCF Control Panel",
        "",
        "# Clear area in front of clock",
        "fill ~-3 ~3 ~-8 ~5 ~8 ~-2 minecraft:air",
        "fill ~-3 ~3 ~-8 ~5 ~3 ~-2 minecraft:polished_blackstone",
        "",
        "# Panel structure",
        "fill ~-2 ~4 ~-6 ~4 ~6 ~-6 minecraft:black_concrete",
        "fill ~-1 ~4 ~-5 ~3 ~5 ~-5 minecraft:air",
        "",
        "# Signs",
        'setblock ~0 ~5 ~-6 minecraft:oak_wall_sign[facing=north]{front_text:{messages:[\'"DCF v3.1"\',\'"Protocol"\',\'"Control"\',\'"Panel"\']}}',
        "",
        "# Status displays",
        "setblock ~-1 ~4 ~-6 minecraft:redstone_lamp",
        "setblock ~3 ~4 ~-6 minecraft:redstone_lamp",
        "",
        "# Instructions",
        'setblock ~1 ~4 ~-6 minecraft:oak_wall_sign[facing=north]{front_text:{messages:[\'"1. Flip lever"\',\'"2. Watch bus"\',\'"3. Check payload"\',\'""\']}}'  ,
        "",
        "# Teleport operator to panel",
        "tp @s ~1 ~4 ~-7 0 0",
        "",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Control panel ready","color":"green"}]',
    ]
    return lines

def generate_forceload() -> list[str]:
    """Generate chunk forceload commands for reliable operation."""
    # Calculate chunk boundaries
    min_z = -10
    max_z = DECODER_Z + 20
    
    lines = [
        "# DCF Chunk Forceloading",
        "# Required for reliable redstone across chunk boundaries",
        "",
        f"forceload add ~-16 ~{min_z} ~32 ~{max_z}",
        "",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Chunks forceloaded","color":"green"}]',
    ]
    return lines

def generate_master_install() -> list[str]:
    """Generate master installation function."""
    lines = [
        "# DCF Protocol — Master Installation",
        f"# Version: 3.1.0",
        f"# Generated: {datetime.now().isoformat()}",
        "",
        "# Forceload chunks first",
        "function dcf:install_forceload",
        "",
        "# Install layers in order",
        "function dcf:install_clock",
        "function dcf:install_tx",
        "function dcf:install_auth",
        "function dcf:install_buffer",
        "function dcf:install_decoder",
        "function dcf:install_panel",
        "",
        "# Completion message",
        'tellraw @a ["",{"text":"═══════════════════════════════════════","color":"dark_green"}]',
        'tellraw @a ["",{"text":"  DCF PROTOCOL INSTALLATION COMPLETE  ","color":"green","bold":true}]',
        'tellraw @a ["",{"text":"═══════════════════════════════════════","color":"dark_green"}]',
        'tellraw @a ["",{"text":""}]',
        'tellraw @a ["",{"text":"  Protocol: ","color":"gray"},{"text":"17-byte framed analog","color":"white"}]',
        'tellraw @a ["",{"text":"  Stages:   ","color":"gray"},{"text":"34 nibbles","color":"white"}]',
        'tellraw @a ["",{"text":"  Auth:     ","color":"gray"},{"text":"Physical subtraction","color":"white"}]',
        'tellraw @a ["",{"text":""}]',
        'tellraw @a ["",{"text":"  Flip the lever at the clock to begin.","color":"yellow"}]',
    ]
    return lines

def generate_test_functions(builder: DatapackBuilder):
    """Generate test/debug functions."""
    
    # Test: Verify signal math
    verify_lines = [
        "# DCF Test — Verify Signal Calculations",
        "",
    ]
    for sig in range(15):  # 0-14 only
        items = signal_to_items(sig)
        verify_lines.append(f"# Signal {sig:2d} requires {items:3d} items")
    verify_lines.append("# Signal 15 is not achievable with comparator")
    verify_lines.append("")
    verify_lines.append('tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Signal table verified (check function file)","color":"green"}]')
    builder.write_function("test_signals", verify_lines)
    
    # Test: Inject bad signal to test auth
    tamper_lines = [
        "# DCF Test — Tamper Detection",
        "# Removes 1 item from TX stage 0 barrel",
        "",
        f"data modify block ~2 ~4 ~{TX_START_Z} Items[0].count set value 1",
        "",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"TAMPER: Stage 0 barrel modified","color":"red"}]',
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Auth layer should now REJECT transmission","color":"yellow"}]',
    ]
    builder.write_function("test_tamper", tamper_lines)
    
    # Reset: Restore tampered barrel
    reset_lines = [
        "# DCF — Reset Tampered Barrel",
        "",
        f"setblock ~2 ~4 ~{TX_START_Z} minecraft:barrel[facing=east]{barrel_nbt(NIBBLE_SEQUENCE[0])}",
        "",
        'tellraw @a ["",{"text":"[DCF] ","color":"gold"},{"text":"Stage 0 barrel restored","color":"green"}]',
    ]
    builder.write_function("test_reset", reset_lines)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("DCF Protocol — Minecraft Datapack Generator v3.1.0")
    print("=" * 60)
    print()
    
    # Validate signal calculations
    validate_signal_table()
    
    # Create datapack
    builder = DatapackBuilder(DATAPACK_NAME)
    builder.setup_directories()
    builder.write_pack_mcmeta()
    builder.write_load_tag()
    
    # Generate all functions
    print("Generating functions...")
    builder.write_function("load", generate_load_function())
    builder.write_function("install_clock", generate_clock())
    builder.write_function("install_tx", generate_tx_layer())
    builder.write_function("install_auth", generate_auth_layer())
    builder.write_function("install_buffer", generate_jitter_buffer())
    builder.write_function("install_decoder", generate_decoder())
    builder.write_function("install_panel", generate_control_panel())
    builder.write_function("install_forceload", generate_forceload())
    builder.write_function("install", generate_master_install())
    
    # Generate test functions
    generate_test_functions(builder)
    
    print()
    print(f"✓ Datapack generated: {os.path.abspath(builder.base_path)}")
    print()
    print("Installation:")
    print(f"  1. Copy '{DATAPACK_NAME}' folder to .minecraft/saves/<world>/datapacks/")
    print("  2. Run: /reload")
    print("  3. Teleport to flat area: /tp @s 0 100 0")
    print("  4. Clear space: /fill -50 90 -50 50 200 300 air")
    print("  5. Run: /function dcf:install")
    print()

if __name__ == "__main__":
    main()
