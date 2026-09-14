# DCF Remote Engine — splitting the DeMoD audio stack over Punctim

**Draft** · DeMoD LLC · Informative overview. Normative detail lives in
[`DCF_CONTROL_SPEC.md`](DCF_CONTROL_SPEC.md) (GUI → engine) and
[`DCF_TELEMETRY_SPEC.md`](DCF_TELEMETRY_SPEC.md) (engine → GUI).

## Goal

Run the deterministic DeMoD audio stack **split across a link**: the real-time engine
(orchestrator + demod-rt) on a **VM or a tightly-wired offloader**, and the GUI (demod-ui)
on the workstation. Punctim is the transport. The engine goes on isolated/dedicated
hardware; the GUI stays on the host.

## Why it is small

demod-ui never touches the engine directly. Locally it talks over:
- a **Unix control socket** (`$DEMOD_CONTROL_SOCK`, JSON-lines ops) — GUI → engine, and
- **shared memory** (`/dev/shm/demod-rt-meters`, `/dev/shm/demod-params`) — engine → GUI.

Both become Punctim adapters. The engine (orchestrator + demod-rt) is **not modified**.

```
        HOST (workstation)                    ENGINE (VM / wired offloader)
  ┌───────────────────────────┐        ┌──────────────────────────────────────┐
  │ demod-ui  (GUI)           │        │  orchestrator + demod-rt  (unchanged)  │
  │  ├ backend/remote.lua     │        │      ▲  local control.sock             │
  │  └ dm.dcf  (DCF UDP) ──────┼──DCF───┼─────►│  demod-remote-bridge ───────────┤
  │        ▲ telemetry buffer │  UDP   │      │   (DCF ⇄ local-IPC relay)        │
  │        └──────────────────┼◄───────┼──── /dev/shm/demod-rt-meters           │
  └───────────────────────────┘        │      audio ► local interface (or back) │
                                        └──────────────────────────────────────┘
     DCF-Control  (DATA frames, ≤4 KB/op, reliable-where-needed)
     DCF-Telemetry(CTRL frames, ≤124 B/tick, lossy, latest-wins)
```

## The two components (everything else is unchanged)

- **`demod-remote-bridge`** (engine side, C): a DCF ⇄ local-IPC relay. Reassembles DCF-Control
  ops → writes them to `$DEMOD_CONTROL_SOCK` (a normal local client); reads the meters shm
  each tick → streams DCF-Telemetry. Header-only codecs (`codec/demod_frame.h`,
  `demod_text.h`, `demod_audio.h`) + ~150 lines lifted from `C_SDK/node/dcfnode.c`; links
  only libc. Sibling of the existing DeMoD device-bridge.
- **`dm.dcf` + `backend/remote.lua`** (host side): `dm.dcf` is a small C DCF-UDP client in
  demod-ui (sibling of `dm.ctl`/`dm.crypto`). `remote.lua` is the orchestrator backend with
  two swaps — `ctl()` sends over `dm.dcf`, and `poll()` fills the same meter/param structs
  from the telemetry stream. Nothing above the backend changes; the whole DSP Studio works
  over the link unchanged.

## What rides the wire

| Direction | Adapter | Frames | Reliability |
|-----------|---------|--------|-------------|
| GUI → engine (control) | DCF-Control (over DCF-Text) | `DATA`, ≤4 KB/op | ACK+retransmit for state-changing; fire-and-forget for latest-wins |
| engine → GUI (meters/scope) | DCF-Telemetry (over DCF-Audio) | `CTRL`, ≤124 B/block | lossy, latest-wins |
| liveness | `DCF_MSG_PING/PONG` | — | heartbeat → "engine offline" + reconnect |

Audio does **not** ride the control link (see below). Only UDP is used (the certified real
transport); channels are the frame `dst_id` (control / telemetry / scope).

## Audio (configurable; the control/telemetry link is identical either way)

- **Appliance (default, offloader):** demod-rt drives the engine box's own interface. Audio
  never crosses the link → fully deterministic, zero added audio latency.
- **Audio-return (VM):** the VM has no real interface, so route audio to the host — via
  **virtio-sound** (native, near-zero latency; preferred for a same-host VM) or, for a
  remote offloader you want to monitor, **DCF-Audio** (the existing `DCF_AUDIO_SPEC.md`
  adapter) streamed back. virtio-sound is v1; DCF-Audio monitoring is a later phase.

## Latency profiles (slow-link vs VM/offloader)

The DCF-Audio spec's **20 ms** figure is a jamming budget: a 20 ms codec block plus a
20-40 ms jitter buffer, sized for the open internet. **On a VM (virtio/vsock) or a wired
offloader the one-way network is sub-millisecond, so that budget does not apply** and the
block/window can be halved (or more):

| Path | Slow link (default) | VM / offloader (low-latency profile) |
|------|---------------------|--------------------------------------|
| **Control** (GUI->engine) | ~1 op / event, UDP RTT-bound | sub-ms RTT; latency ~= the link, not 20 ms |
| **Telemetry** (meters) | 30 Hz block | **60 Hz** (`--tele-hz`, one block per UI frame); a meter is display-bound, so higher is wasted |
| **Audio-over-DCF** (optional monitoring, P5) | 20 ms block + 1-2 packet jitter buffer | **10 ms block + 1-packet (or no) jitter buffer** -> roughly half the monitoring latency |

So: control latency is already just the (sub-ms) link RTT; telemetry runs at a UI frame
(60 Hz); and when audio is streamed back over DCF for monitoring, the VM/offloader profile
uses a **10 ms block** (half the 20 ms) with a minimal jitter buffer. The appliance audio
model (audio local on the engine box) has no link in the audio path at all, so it is the
lowest-latency option and the default.

## Security

The Punctim wire is **encryption-free by design** (`DCF_SECURITY_EXPOSURE.md`), and control
ops are forgeable. So: plaintext on a **trusted** VM/wired link; **WireGuard beneath** the
UDP on an untrusted one; optional **Ed25519 pairing-auth above the wire** (reusing the DeMoD
`dm.crypto`) so the bridge only accepts ops from an authenticated GUI. Never add crypto to
the L1 codec.

## Phases

0. **Link** — lift `dcfnode.c` glue into the bridge + `dm.dcf`; PING/PONG + text echo.
1. **Control** — DCF-Control relay → local socket; a knob on the host changes a param on the engine.
2. **Telemetry** — DCF-Telemetry stream → `poll()` structs; meters/scope live; MIXER/VIZ remote.
3. **Full remote backend** — `select.lua` `remote` case; all ops + reliability + reconnect + scope.
4. **Hardening + packaging** — pairing-auth, WireGuard module, `engine-appliance` / `gui-client`
   flake targets, a VM image (virtio-sound) + an offloader image, 2-node loopback CI.
5. **(optional)** DCF-Audio monitoring (mix streamed back).

## Where it lives

- **Punctim**: these three docs + the DCF-Control / DCF-Telemetry reference codecs
  (`codec/*.h`, `lua/*`) + golden vectors.
- **DeMoD monorepo**: `demod-remote-bridge` (engine), `dm.dcf` (framework), `remote.lua` +
  `select.lua` case (app), and the flake targets. Consumes Punctim as a pinned input.

## Certification

The L1 frame and the DCF-Text / DCF-Audio L2 framing are already golden-vector certified.
Only the new **op relay** (opaque JSON, no vectors needed) and the two **telemetry block
layouts** (meters, scope) add vectors — C/Lua/Python byte-identical, per `DCF_TELEMETRY_SPEC.md`.
