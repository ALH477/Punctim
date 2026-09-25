# DeMoD 17-Byte Redstone Framework: Use Cases & Command Block Integrations

> **Scope note.** This addendum extends the Minecraft redstone demo (D17BCP), which is
> **not** the DCF wire quantum: its 17-byte frame has no `0xD3` sync, no version nibble
> and no CRC-16, so it is non-conforming to
> [`WIRE_QUANTUM_SPEC.md`](../Documentation/WIRE_QUANTUM_SPEC.md) and uncertified.
> See [`README.md`](README.md).

**Version**: 2.1 Extension  
**Minecraft**: Java 1.21+  
**Overview**: This addendum expands the pure-redstone DeMoD build into hybrid systems. **Use Cases** showcase real-world MC applications (survival, servers, maps). **Command Block Integrations** layer `/command` logic atop the decoder/shim for dynamic, programmable comms—turning static verification into a full protocol stack. No mods; datapacks optional for functions.

**Key Benefits**:
- **Secure**: Exact header auth rejects fakes (e.g., griefers).
- **Jitter-Resilient**: Buffer handles lag/desync.
- **Scalable**: Mesh via multi-bus.
- **Educational**: Visualizes real DSP/physics protocols.

## I. Use Cases
DeMoD excels in trustless, low-overhead signaling. Below: prioritized by practicality.

| Use Case | Description | Setup | Benefits | Scale |
|----------|-------------|--------|----------|-------|
| **Secure Base-to-Base Comms** | Transmit "unlock" signals between distant bases (e.g., ally alert). TX at base A, RX at B. | Wire TX to player detector; auth → door/TNT disable. | Anti-griefer: Only valid DeMoD header opens. Lag-proof for servers. | Multi-node mesh (parallel buses). |
| **Automated Trading/Trust** | Vendor verifies buyer header before dispensing items. | Hopper minecart "packet" → TX → auth → /give loot. | Scam-proof economy: Dynamic payload (sig10=trade). | Server economies (e.g., shops). |
| **Redstone Computer Networking** | Interconnect computors (e.g., Create mod, RedLogic). | SSS bus as "wireless" link; auth gates data. | Deterministic mesh; simulates real protocols. | Mega-bases/computers. |
| **Multiplayer Alerts/Signaling** | Notify players: "Raid incoming" via header + payload. | Player joins → TX header → command decoder → /title. | Cross-dimension (nether portals). No chat spam. | SMP servers (100+ players). |
| **Adventure/Puzzle Maps** | "Hack the signal" puzzles: Players craft valid header items. | Custom fill barrels → auth puzzle. | Immersive tech-puzzles; exportable worlds. | Custom maps (speedruns). |
| **Performance Monitoring** | Heartbeat (sig1) pings server status. | Repeating TX → auth → /tellraw uptime. | Server admins: Detect lag/jitter visually. | Any multiplayer. |
| **Punctim Sim** | Full P2P demo: Multiple TX/RX nodes. | OR-bus merge; buffer per node. | Visualizes your OSS framework. Viral X/YouTube. | Educational/demos. |

**Implementation Tip**: Start with Use Case 1 (comms)—duplicate build 2x, link via long repeater lines/nether.

## II. Command Block Integrations Guide
**Philosophy**: Pure redstone handles PHY/MAC (tx/auth/buffer). Commands handle APP layer (dynamic payloads, logging). Use **Chain/Repeat** blocks powered by decoder signals.

**Prerequisites**:
- Enable cheats or OP.
- Place **Impulse** (start), **Chain** (conditional/always), **Repeat** (loop).
- **Datapack Optional**: For `/function demood:payload` (below).

### A. Basic Decoder Upgrade (Sig → Commands)
Replace TNT/Glowstone with command blocks. Power via repeater from comps.

**ASCII Diagram** (Z=245-255, Y=4-6):
```
Buffered SSS ─── Split Dust ───► Comp15 ───► Impulse CB "Reset" ───► Chain CB "Clear Score"
                          │                    │
                          │                    │──► Repeat CB "/scoreboard players reset @a demood"
                          │
                          ├── Comp10 ───► Impulse CB "Payload" ───► Chain "/give @p[distance=..5] diamond 1"
                          │
                          └── Comp1 ───► Repeat CB "Heartbeat" ───► Chain "/title @a actionbar {'text':'DeMoD Alive','color':'green'}"
```
**Steps**:
1. **Comp15 (Reset)** `(245,5,245)` → R1t → **Impulse CB** `(246,5,245)` facing +Z: `/scoreboard objectives remove demood` (always active).
2. **Chain CB** `(247,5,245)`: `/scoreboard players reset @a demood` (conditional).
3. **Comp10 (Payload)** `(248,5,247)` → **Impulse**: `/give @p[distance=..5] diamond 1 {display:{Name:'"DeMoD Reward"'}}`.
4. **Chain** `(249,5,247)`: `/playsound entity.player.levelup master @a ~ ~ ~ 1 1.2`.
5. **Comp1 (Heartbeat)** `(250,5,249)` → **Repeat**: `/title @a actionbar "DeMoD: Signal Purity ✓"`.
6. **Chain** `(251,5,249)`: `/particle happy_villager ~ ~1 ~ 0.5 0.5 0.5 0 5`.

**Test**: Valid header → diamond + sound + title. Invalid → nothing.

### B. Dynamic Header via Scoreboards
Store header in scoreboard; TX pulls from player scores (e.g., custom items set scores).

**Setup**:
1. `/scoreboard objectives add demood dummy`.
2. Player holds items: `/scoreboard players set @s demood.nibble0 5` (via custom function).
3. TX Barrels: Replace static fills with hopper-fed from player inventory (advanced: item frames → hoppers).

**Dynamic TX Diagram**:
```
Player Detector ───► /scoreboard players operation demood.nibble0 demood = @s demood.nibble0 ───► Hopper Fill Barrel (item count = score)
                                                                 │
                                                                 └──► TX Stage 0 Barrel
```
**Command Chain** (Pre-TX Impulse):
```
/execute as @a[scores={demood=1..}] run scoreboard players operation tx.nibble0 demood = @s demood.nibble0
/execute store result block X Y Z Items.count run scoreboard players get tx.nibble0 demood
/data modify block X Y Z Items set value {id:"minecraft:stone",Count:1b}  // Scale to min_items
```
**Use**: Players "encode" header via scores → verified auth → personalized payload.

### C. Logging & Multiplayer Broadcast
**Repeat CB Chain** (Powered by Heartbeat Sig1):
1. `/scoreboard players add demood.global 1`
2. `/execute if score demood.global demood matches 34 run tellraw @a [{"text":"[DeMoD] Valid frame received!","color":"gold"}]`
3. `/execute if score demood.global demood matches 34.. run scoreboard players set demood.global demood 0`

**ASCII**:
```
Sig1 ─── Repeat "Increment" ─── Chain "Check 34?" ───► Impulse "Broadcast" ─── Chain "Reset Counter"
/title @a subtitle "Payload: Trade Complete"
```

### D. Advanced: Datapack Functions
Create `data/demood/functions/payload.mcfunction`:
```
# demood:payload
give @p[distance=..10] diamond_sword{display:{Name:'{"text":"DeMoD Blade"}'}} 1
particle explosion ~ ~ ~ 2 2 2 0.1 10
playsound entity.dragon.fireball_explode master @a ~ ~ ~ 2 0.8
scoreboard players add @s demood.trades 1
tellraw @a [{"selector":"@s","color":"aqua"},{"text":" completed DeMoD trade!","color":"green"}]
```
**Integration**: Decoder Sig10 → Impulse: `/function demood:payload`.

**Load**: Place in world/datapacks → `/reload`.

### E. Mesh Node Expansion (Multi-Bus)
**Diagram** (3 Nodes):
```
TX1 ─── Bus1 ─── OR Dust ─── Auth ─── Buffer ─── Decoder CBs
TX2 ─── Bus2 ───┘
TX3 ─── Bus3 ───┘
```
1. Parallel SSS buses → dust OR-merge (priority).
2. Per-bus auth → shared buffer.
3. CB: `/tellraw @a [{"text":"Node ","color":"yellow"},{"score":{"name":"@s","objective":"demood.node"},"color":"gold"},{"text":" validated","color":"green"}]`

### F. Performance & Survival Notes
- **Lag**: <1% TPS hit (34 pulses).
- **Survival**: Use droppers for semi-static barrels; minecarts for mobile TX.
- **Multiplayer**: Nether wiring for 8:1 distance.
- **Security**: Obfuscate barrel locations; use armor stands for hidden CBs.

## III. Full Workflow Example: Secure Comms Use Case
1. **Player A** (Ally Base): Steps on plate → TX header (pre-filled or score-dynamic).
2. **Long Wire/Nether** → RX at Base B.
3. **Auth Passes** → Buffer → Sig10 → CB: `/fill X Y Z X Y Z air` (open vault door).
4. **Log**: `/tellraw @a[team=ally] "Base B unlocked by @s"`.
5. **Invalid**: Breaker → `/particle lava ~ ~ ~` (alert).

**Export**: World ZIP + datapack → itch.io/PlanetMC.

This hybrid unlocks DeMoD as a **protocol engine**—redstone PHY + commands APP.
