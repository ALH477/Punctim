# DCF Field Testing & Intended Use

How DCF / Punctim is meant to be deployed off-grid — over cheap handheld radios and
heterogeneous mesh links — and how to field-test it rigorously before you trust it.

> **What DCF is NOT.** DCF is an experimental, **plaintext**, encryption-free research
> protocol. It is **not a certified life-safety system** and must never replace certified
> emergency communications (P25, TETRA, licensed dispatch, a PLB/satellite messenger).
> Use it to *supplement* those — extra telemetry, a backup text path, a game mechanic —
> with independent fallback for anything where a dropped packet costs a life.

DCF's fitness for the field rests on being **adaptive on two independent axes**:

1. **Modulation-adaptive** — the wire quantum is medium-agnostic; you pick a *waveform
   profile* to match the channel. A walkie-talkie is a narrow 300–3000 Hz band-pass
   channel with AGC and squelch, so the field profile is the **FSK family**
   (AFSK → MSK/GFSK, 4-FSK for throughput) wrapped in **RS-FEC** — never AGC-fragile
   ASK/QAM. See [`DCF_MODEM_SPEC.md`](DCF_MODEM_SPEC.md) and [`DCF_SDR_SPEC.md`](DCF_SDR_SPEC.md).
2. **Topology-adaptive (uplink-oriented)** — the self-healing RTT/AUTO-master mesh
   **orients toward an uplink**. One node with backhaul (Starlink, cellular, a ham
   gateway) becomes the egress, and RTT-weighted routing pulls everyone's off-grid
   traffic toward whoever is closest to it. See [`DCF_MESH_SPEC.md`](DCF_MESH_SPEC.md).

---

# Part A — Intended Use (operator-facing)

## The uplink-oriented mesh

Off-grid teams are rarely uniform: somebody has the Starlink Mini, the cell-coverage
ridge, or the ham gateway back to civilization. DCF treats that node as the mesh's
**egress** without any new wire format. Each node advertises its uplink and its backhaul
cost; routing then orients every node's next hop along the lowest-total-RTT path to the
nearest/cheapest uplink. The runnable demo:

```sh
python3 python/modem/uplink_demo.py
```

```
SAR: BaseCamp has Starlink — teams orient egress to base
    BaseCamp   -> egress here    (5ms to BaseCamp uplink)  [UPLINK]
    Relay-N    -> via BaseCamp   (17ms to BaseCamp uplink)
    TeamA      -> via Relay-N    (37ms to BaseCamp uplink)
    Relay-S    -> via BaseCamp   (20ms to BaseCamp uplink)
    TeamB      -> via Relay-S    (45ms to BaseCamp uplink)
```

When the base link drops and a TeamB member raises a hotspot, the orientation **flips
automatically** — nearby nodes re-egress toward the new uplink. With two uplinks the mesh
partitions itself so each node uses its cheapest egress. This is the self-healing mesh
applied to *backhaul*, not just peer loss: the mechanism is
`meshlab_core.egress_routes(n, edges, uplinks)`, which models "the uplink" as a virtual
sink and reuses the certified RTT Dijkstra (prototype; the certified REPORT/ROLE wire
bytes are unchanged).

## Deployment scenarios

Every scenario carries ordinary `DeModFrame`s — text, **position** (`MSG_POSITION`,
a 12-byte x/y/z triple, usable as lat/lon/alt), or **DCF-Game** events
([`DCF_GAME_SPEC.md`](DCF_GAME_SPEC.md)) — over the profile and topology below.

| Scenario | Carries | Topology / egress | Duty | Profile | Why DCF fits |
|---|---|---|---|---|---|
| **Hiking** | position beacons + short text | small P2P/mesh; one member's sat messenger is the uplink | very low (beacon every 1–5 min) | handheld AFSK | no infra; ridge relays heal around terrain shadow |
| **Search & Rescue** | grid-search position telemetry, status, text | team mesh → **Starlink base camp**; ICS hand-off at the uplink | low–moderate | handheld AFSK / MSK | weak-signal **FEC recovery**; last-known-position survives node loss; egress concentrates at base |
| **Disaster aid** | inter-agency text, position, store-and-forward | mesh around the **one surviving uplink**; multi-agency | moderate | handheld AFSK | works with infra down; plaintext is *interoperable* across agencies (apply crypto above the frame if needed) |
| **Firefighting** | PAR / accountability, hazard flags, position | crew mesh; engine/IC node may hold the uplink | bursty; **respect radio heat** | handheld AFSK | constant-envelope FSK survives a hot, noisy HT; duty cycle protects the radio |
| **Hunting** | infrequent position + simple status | tiny P2P; quiet by design | **very low** (battery, cold, stealth) | handheld AFSK | sub-minute beacons; no chatter; long battery life |
| **Paintball / airsoft** | DCF-Game events + fast position | direct P2P / LAN-style, short range | **high OK** | gfsk/qpsk (clean SR link) | low-latency event frames; the Game adapter already exists |
| **Marathon / endurance** | runner telemetry at scale | fixed **course relays** + aid-station uplinks | steady | gfsk + relays | many nodes funnel to staffed uplinks; RTT routing keeps egress short |

For competitive games the link is usually short and clean (helmet/SBC to SBC, or an SDR),
so the faster `gfsk`/`qpsk`/`fsk4` profiles apply; for the wilderness/emergency cases the
link is a handheld FM voice channel, so the reliability-first **handheld** profile applies.

---

# Part B — Field Testing (engineer-facing)

## The handheld FM channel model

A walkie-talkie is not a wire — it is an aggressive audio band-pass with active gain and
gating. Design to its constraints, not around them:

```
[0 Hz] ──(hard cut)── [300 Hz] ═════ usable ═════ [3000 Hz] ──(hard cut)──
                                  ^ anchor tones 1000–2000 Hz ^
```

- **300–3000 Hz pass-band.** Anything below 300 / above 3000 Hz is attenuated or gone.
  Anchor tones mid-band; keep both AFSK tones off the rolloffs.
- **AGC.** The receiver normalizes volume over the first milliseconds and continuously
  fights amplitude structure → **amplitude modulations (ASK/OOK/QAM) corrupt**. Use
  **constant-envelope FSK or PSK**; on an FM HT, **FSK wins** (the radio's own FM
  discriminator works *for* you, as 40 years of APRS/Bell-202 on 2 m attests).
- **Squelch + CTCSS/DCS.** Turn privacy tones **off**; run **carrier/open squelch**.
  CTCSS adds speaker-open delay that clips packet heads. Even so, the receiver needs a
  **keyup preamble** long enough for AGC to settle and squelch to open *before* the sync
  word.

## The recommended profile (and why)

| | Reliability-first | Throughput | Avoid |
|---|---|---|---|
| **Modulation** | **MSK / GFSK** (CPFSK, mod-index 0.5 — spectrally compact, fits the narrow pass-band) | **4-FSK** (2 bits/symbol over the certified 2-bit map) | ASK / OOK / QAM (AGC) |
| **Reliability** | RS-FEC + interleaver in front (corrects, doesn't just detect) | same | bare CRC alone |
| **Framing** | long keyup preamble (AGC settle + squelch open), then `0x7E` sync | same | short preamble |

What ships today:

- **Acoustic (walkie-talkie audio)** — two **interoperable** modems. The real-time
  `python/modem/main.py` (JIT Faust FSK DSP) and the pure-numpy `python/modem/acoustic.py`
  both build the on-air bit stream from one shared module, **`python/modem/acoustic_frame.py`**,
  and share the same `PROFILES` tones/baud — so a frame sent by either is decodable by the
  other (verified byte-for-byte). Continuous-phase **AFSK at 300 baud**; the **handheld**
  profile pulls both tones to **1200/1800 Hz** (mid-band) with a **240-bit (~800 ms) keyup
  preamble** for AGC settle / squelch open. Payload is `frame + crc8` by default (the
  deployed format) or the certified **RS codeword** with `fec=True`. The numpy modem ships
  a **modelled radio channel** (`walkie_channel`: 300–3000 Hz band-pass + AGC + noise) so
  the profile is testable without hardware.
- **SDR / IQ baseband** — `python/modem/iq.py` + `dcf-sdr` add **`--mod msk`** (MSK,
  modulation index 0.5) and **`--mod fsk4`** (plus `gfsk`), all RS-FEC-wrapped and
  loopback-tested. (MSK/4-FSK live on the IQ path; the acoustic path is AFSK.)

MSK is the elegant middle: continuous-phase FSK with modulation index 0.5 is
mathematically offset-QPSK — a *frequency* modulation you can detect like a *phase* one
(non-coherent when the channel is ugly, coherent when it's clean). Scale throughput with
**4-FSK**, not by switching to PSK, which the FM voice channel can't carry near capacity.

## The field node

```
[ Smartphone app ] ──Bluetooth/Wi-Fi── [ SBC / MCU ] ──audio── [ Walkie-talkie ]
```

- **Controller.** A **Raspberry Pi Zero 2 W**-class SBC runs the Python/Faust modem
  today. An **ESP32-S3** MCU is the lightweight target but is a **future firmware port**
  (not yet built).
- **Audio I/O.** Use an **external I2S DAC (e.g. PCM5102)**, not an internal MCU DAC —
  internal DACs inject electrical noise that blurs the subtle tone/phase shifts the demod
  must resolve.
- **Field UI.** Host a **Bluetooth-serial or Wi-Fi AP** so a phone terminal is the
  screen/keyboard; keep the hardware in the pack.
- **Power.** **Isolate the rails** — a 5 W radio can pull >1.5 A at keyup and brown out a
  shared MCU. Separate supplies (or a stiff, isolated regulator).
- **Duty / thermal.** Enforce a **duty cycle / cool-down**; continuous bursts overheat an
  HT's tiny heatsink and drain the pack. Tune beacon cadence per scenario (Part A).

## Audio routing (PipeWire / JACK / ALSA)

Getting the modem audio in and out of the radio is just audio routing — DCF works with
any Linux audio server two ways:

- **File route (server-agnostic, no live-audio deps).** The numpy modem reads/writes
  WAV, so you pipe it through whatever player/recorder your server provides:
  ```sh
  python3 python/modem/acoustic.py tx --text "SOS" --profile handheld --wav out.wav
  pw-play out.wav                                  # PipeWire  (or: aplay / jack-play)
  pw-record -d <radio-source> in.wav               # capture the radio's audio
  python3 python/modem/acoustic.py rx --profile handheld --wav in.wav
  ```
  `rx` trims leading capture silence (energy onset) so a `pw-record` file aligns. RS-FEC
  with `--fec`.
- **Live route (real-time, low latency).** The Faust modem uses PortAudio (sounddevice),
  which speaks **PipeWire/Pulse and JACK** through PortAudio's host APIs. PipeWire even
  exposes a JACK-compatible API, so PortAudio's default device already flows through it:
  ```sh
  python3 python/modem/main.py --list-devices            # see PipeWire/JACK/Pulse ports
  python3 python/modem/main.py tx --profile handheld --device pipewire "SOS"
  ```
  Patch the modem's ports to your USB audio interface (→ radio mic) and the radio's
  capture (→ modem input) in a patchbay — **qpwgraph** (PipeWire) or **qjackctl/Carla**
  (JACK). A USB audio interface to the radio's 3.5 mm TRRS keeps the levels clean.

Both routes carry the *same* on-air framing, so a numpy/file node and a live Faust node
interoperate over the air.

## Multi-transport bridge (any network shape)

The same **DeModFrame** rides UDP, audio (PipeWire/JACK), SDR, or a file spool behind one
`dcf.transport.Transport` interface, and `dcf.bridge.Bridge` relays frames *between* them
— so you compose a node's shape from transports and bridge an **acoustic island → RF
backbone → UDP/Starlink uplink**. Because link rates differ by orders of magnitude (Mbps
UDP vs ~300-baud acoustic ≈ 37 B/s), **each transport has a bounded queue** that decouples
them: a fast source can't drown a slow link, and on overflow bulk is shed while priority
(control/mesh) frames survive. Routing is **flood** (mesh; a src/dst/seq/crc dedup set
keeps multi-hop loop-free) or **egress** (reverse-path learning sends a unicast frame to
the transport that reaches its dst; an off-grid dst goes to the `--uplink` transport).

Reproduce locally — **no hardware** (file/loopback media):
```sh
# multi-hop across media: A(acoustic) -> B(acoustic->SDR) -> C(SDR); frame arrives byte-exact
python3 python/dcf/bridge.py --demo

# compose a bridge from transports (swap file/udp/audio/sdr freely):
python3 python/dcf/bridge.py -t 'udp:bind=0.0.0.0:9100' \
                             -t 'audio:in=/tmp/rx,out=/tmp/tx,profile=handheld' \
                             --route egress --uplink udp --text SOS --seconds 3
# (installed as a console command too: `pip install ./python` then `dcf-bridge --demo`)

# unit tests (buffer drop/priority, per-transport round-trips, dedup, egress, multi-hop):
python3 -m unittest python/tests/test_transport.py python/tests/test_bridge.py
```
Live **PipeWire/JACK** (audio transport) and **SoapySDR** (sdr transport) plug in as the
transport I/O; the file/loopback media above are the hardware-free CI path.

## Test ladder

Climb tier by tier; do not skip — most failures are coupling/level/AGC, not the codec.

| Tier | Setup | Command | Pass criteria |
|---|---|---|---|
| **T1 — bench loopback** | no radio; software channel | `python3 python/modem/sdr.py tx --text "DCF!" --mod msk --iq /tmp/d.cf32 && python3 python/modem/sdr.py rx --iq /tmp/d.cf32 --mod msk` | frame recovered, CRC valid |
| **T1 — modelled radio channel** | no hardware; numpy band-pass + AGC + noise | `python3 python/modem/acoustic.py` | handheld AFSK frame recovered (crc8 + fec) through the 300–3000 Hz + AGC channel |
| **T1b — acoustic loopback** | speaker→mic, one box | `python3 python/modem/main.py loopback --profile handheld "DCF field test"` | mark/space **SNR report** + frame recovered |
| **T2 — wired coupling** | DAC→radio mic, radio speaker→ADC, cable | tx on radio A, rx on radio B, **PER over ≥100 frames** | PER < 1% with FEC at nominal level |
| **T2 — two interfaces, line cable** | two audio interfaces cross-cabled (out 1 → in 1), no radio | `hydramodem/dcf-tools/field-test.sh --tx-dev plughw:A,0 --rx-dev plughw:B,0 -n 200` per direction | PER < 1% with FEC. Uses **HydraModem** (1000-baud CPFSK), whose symbol-timing recovery (±3000 ppm) absorbs the two interfaces' **independent sample clocks** — the 300-baud AFSK path has no fractional-symbol recovery and is not for two-clock links |
| **T3 — same-room OTA** | two HTs, low power, open squelch | as T2, vary TX gain | find the level window; PER < 5% |
| **T4 — field OTA** | two nodes, real distance/terrain | beacon + text + position; log per run | range, **PER**, latency, throughput, battery |
| **T5 — mesh + uplink** | ≥3 nodes, one with backhaul | `python3 python/dcf_node.py start --mode auto …`; verify egress orients to the uplink (`uplink_demo.py` mirrors the logic) | traffic reaches the uplink; failover on node/uplink loss |

Metrics to record every run: **PER/FER**, usable **range**, end-to-end **latency**,
**throughput** (good frames/s), **battery** draw, and AGC/squelch anomalies.

## Field-test log template

```csv
date,scenario,tier,radios,band_chan,profile,tx_gain,distance_m,terrain,frames_tx,frames_rx,per_pct,fec_corrections,latency_ms,throughput_fps,batt_pct_start,batt_pct_end,notes
2026-06-19,SAR,T4,2x BF-F8HP,GMRS-15,handheld,mid,820,light forest,200,196,2.0,37,210,1.1,100,93,"squelch tail clipped 2 heads; +1 preamble"
```

## Legal & safety

- **Bands & power.** **FRS** (license-free, ≤2 W, fixed antenna), **GMRS** (license,
  higher power/repeaters), **MURS** (license-free VHF). Data/modem tones over voice
  channels sit in a grey area on FRS/GMRS — **know your regulator's rules** before TX; RX
  is generally license-free.
- **Ham bands — a real advantage.** US **Part 97 forbids encryption** of amateur
  traffic. DCF being **encryption-free by design** means its plaintext frames are
  **legal on the ham bands** where most data/mesh modes can't add crypto anyway.
- **Plaintext on the air = broadcast.** There is no WireGuard over RF. Anything you
  transmit is readable by any receiver — membership, positions, message contents. If you
  need confidentiality, apply operator-supplied, export-compliant crypto **above** the
  frame and **off the regulated band** — see [`DCF_SECURITY_EXPOSURE.md`](DCF_SECURITY_EXPOSURE.md).
- **Emergencies.** Most regulators permit otherwise-restricted transmissions when life or
  property is in immediate danger — but that is an *exception*, not a deployment plan.
  Carry a certified PLB/satellite messenger; DCF is the supplement.
