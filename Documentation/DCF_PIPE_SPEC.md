# DCF-Pipe: lossless bulk-data transfer over the wire

**0.x — pre-release, reference implementation below**
**Developed by DeMoD LLC** · **License:** LGPL-3.0 (library), consistent with the Punctim core.

> **Scope.** DCF-Pipe adds the one thing the DCF adapter family lacked: a
> **high-throughput, lossless bulk transfer**. It uses the 17-byte wire quantum as a
> *beautiful control layer* — a small, byte-certified control vocabulary steers a
> dumb, fast data lane — while the bytes move on that separate lane as fast as flow
> control allows. The **246-vector wire certificate is untouched**: DCF-Pipe's control
> messages are ordinary frame payloads, and the data lane is a transport *beneath* the
> quantum, exactly like UDP/Steam/JANUS/HydraModem. The wire is plaintext by design —
> deploy behind WireGuard per [`DCF_SECURITY_EXPOSURE.md`](DCF_SECURITY_EXPOSURE.md).

---

## 1. Why it exists

The wire quantum carries 4 payload bytes — superb as a *control* vocabulary (mesh
REPORT/ROLE, beacons, SPA-gated rendezvous), and the adapters stretch it to ~8 KB
messages, but the overhead ratio is wrong for moving megabytes. Meanwhile everything
DCF is already good at — membership, health, election, clocking, channel rendezvous —
is exactly what a raw fast pipe lacks. DCF-Pipe marries the two: **DCF frames are the
control plane; a plain datagram lane is the data plane.**

```
   sender                         control plane (DCF frames)                    receiver
     |  OPEN(session, len, chunk_size, checksum) ------------------------------->|
     |<---------------------------------- CREDIT(n)  (receiver-driven budget) ---|
     |  === data plane: [session|seq|FEC(payload)] datagrams, up to credit ====>|
     |<------------------------------ SACK(base) / NACK(missing) ---------------|
     |  === retransmit the NACKed chunks =====================================>|
     |<----------------------------------------------------- DONE (verified) ---|
```

## 2. Two planes

**Control plane — certified.** A small message set carried as ordinary frame payloads
(the reference runs them on `CTRL` frames; a deployment picks the channel). Byte-exact
across C/Rust/Python, pinned by [`pipe_vectors.json`](pipe_vectors.json). Six messages:

| Msg | Dir | Purpose |
|-----|-----|---------|
| `OPEN` | S→R | begin: `session_id`, `total_len`, `chunk_size`, whole-object FNV-1a checksum |
| `CREDIT` | R→S | permit N more chunks (receiver-driven flow control) |
| `SACK` | R→S | cumulative ack `base` + selective bitmap above base |
| `NACK` | R→S | explicit missing-chunk list (the FEC-budget fallback) |
| `DONE` | R→S | whole object verified — transfer complete |
| `ABORT` | either | tear down (`reason`: checksum/timeout/policy/peer) |

**Data plane — dumb and fast.** A separate datagram lane. One datagram =
`[session_id(2) | chunk_seq(4) | payload…]`, big-endian header (6 bytes). Stateless: the
sender streams chunks against its credit; all intelligence lives in the control frames.
This is the RTP/RTCP split — a fat data channel steered by a thin control channel.

## 3. Wire formats (big-endian)

```
OPEN    [0]=0 [1]=ver  [2:4]=session  [4:8]=total_len  [8:10]=chunk_size  [10:14]=checksum   (14 B)
CREDIT  [0]=1 [1]=ver  [2:4]=session  [4:8]=credit                                            (8 B)
SACK    [0]=2 [1]=ver  [2:4]=session  [4:8]=base  [8]=nbytes  [9:9+n]=bitmap    (LSB-first)   (9+n B)
NACK    [0]=3 [1]=ver  [2:4]=session  [4]=count  [5:5+4k]=seq[k]                               (5+4n B)
DONE    [0]=4 [1]=ver  [2:4]=session                                                          (4 B)
ABORT   [0]=5 [1]=ver  [2:4]=session  [4]=reason                                              (5 B)
CHUNK   [0:2]=session  [2:6]=chunk_seq  [6:]=payload                                          (6+p B)
```

`chunk_seq` and `session_id` are the only routing fields the data lane needs. `ver = 1`.

**Whole-object checksum.** OPEN carries **FNV-1a 32-bit** over the entire object (init
`0x811C9DC5`, prime `0x01000193`). It is deterministic and table-free, so C/Rust/Python
agree byte-for-byte. Anchors: `FNV("") = 0x811C9DC5`, `FNV("hello") = 0x4F9F2CAB`. This
verifies a completed transfer's integrity; it is **not** a security MAC (the wire is
plaintext — confidentiality stays beneath the socket).

## 4. Loss recovery: FEC first, ARQ second

Each chunk's payload is wrapped by the **certified DCF-FEC multi-codeword layer**
(`feclab_core.encode_message` / `demod_fec.h` / `fec.rs`) before it hits the data lane.
So the two loss regimes are handled at two levels:

- **Byte corruption** in a *delivered* chunk (RF/acoustic bit flips) is **healed forward**
  by FEC — no round trip, no retransmit — as long as it stays within the RS budget.
- **A wholly dropped datagram** is a gap the receiver detects (a hole below the highest
  received `chunk_seq`, or a tail gap near completion) and **NACK**s; the sender
  retransmits it. This is the fallback, not the common case.

The transfer completes only when every chunk is present *and* the reassembled object's
FNV-1a matches OPEN. A mismatch (should be impossible after per-chunk FEC + full receipt)
yields `ABORT(checksum)`.

## 5. Flow control: receiver-driven credit

The receiver, not the sender, sets the pace: it grants a **credit budget** sizing how
many chunks the sender may put on the lane, covering a window of fresh chunks plus any it
has just NACKed. This is simpler than sender-side congestion windows and naturally
rate-limits on constrained links — the right default for a fleet of known devices.
(Sender-side windows with RTT estimation get closer to TCP-fairness on the open internet;
that is a future, additive option, not the default.)

## 6. The invariant: Φ, the deficit potential

The wire quantum has one scalar invariant — a frame is valid iff its CRC syndrome is 0.
DCF-Pipe has the analogous single scalar for a *transfer*:

> **Φ = N − |R|**  — the number of chunks not yet correctly received
> (N = chunk count, R = the set of correctly reassembled chunk indices).

Φ is the minimal **complete** certificate of a lossless transfer, because it fuses the two
things a reliability proof needs — a safety invariant and a termination variant — into one
well-founded scalar. Given a receiver that (a) admits a chunk to R only after it decodes,
and (b) never removes one, over a channel delivering each requested chunk with probability
p = 1 − drop > 0:

| | Property | Statement |
|---|---|---|
| **S1** | safety (monotone) | Φ is non-increasing — R never loses a chunk |
| **S2** | safety (correct) | every index in R holds the true bytes, so \|R\| = N ⟹ the object is byte-exact |
| **L1** | liveness (grounded) | Φ ∈ {0…N} is bounded below and well-founded |
| **L2** | liveness (progress) | while Φ > 0 every hole is re-requested, each delivered in expected ≤ 1/p rounds ⟹ Φ → 0 in expected finite time |
| **C** | completion soundness | DONE is emitted ⟺ Φ = 0 ⟺ the object is byte-exact |

**Corollary (lossless).** The transfer terminates with the object delivered byte-for-byte
for *any* drop < 1 and any correctable corruption. Loss costs rounds, never bytes.

**Corollary (optimal throughput).** With no whole-chunk drops, Φ falls by the credit window
W every round, so the transfer completes in exactly **⌈N/W⌉ rounds** — the minimum possible
for a window-W receiver — with **zero retransmit round-trips**, because in-budget bit
corruption is absorbed inline by FEC. *Φ's slope is the throughput.*

Φ is not just prose: `python/dcf/pipe/invariant.py` implements it as a checked monitor
(`PipeInvariantMonitor` asserts S1/S2/C every single round against ground truth;
`check_transfer` additionally verifies L2 and the optimality corollary), and
`python/dcf/pipe/laws.py` sweeps it — plus five derived transfer laws (INTEGRITY,
LOSSLESS, FEC-FORWARD, FLOW-BOUND, IDEMPOTENT) — across objects, chunk sizes, drop rates,
corruption rates, duplication and reordering. CI runs both.

**Progress rule.** The receiver advances an *authorization horizon* by one window each
round. Holes below the previous horizon were authorized a full round ago and still haven't
arrived, so they are genuinely lost and get NACKed; chunks above it simply aren't due yet.
Distinguishing "in flight" from "dropped" **by round rather than by position** is what
makes a lost *final* chunk recoverable (nothing higher arrives to expose it) while never
spuriously retransmitting. It is also why the invariant is timing-agnostic: a "round" is
a control exchange, not a clock, so a 2-millisecond LAN round and a 2-minute acoustic round
behave identically.

## 7. Link profiles — including HydraModem

The control bytes and the invariant are identical on every link; only the *economics*
change. `python/dcf/pipe/protocol.py` ships tuned presets (`profile("hydramodem")`):

| Profile | chunk_size | nparity | credit_window | Rationale |
|---------|-----------|---------|---------------|-----------|
| `lan` | 1400 | 16 | 16 | MTU-sized; parity is cheap at 10 Mb/s+; deep window |
| `hydramodem` | 256 | **0** | 2 | see below |
| `sneakernet` | 8192 | 0 | 32 | fast reliable medium; maximize per-chunk payload |

**Why HydraModem inverts the LAN tuning.** The acoustic M-FSK link carries ~8–12
application bytes/second, so **airtime is the only scarce resource** (CPU is free by
comparison):

- **`nparity = 0`.** HydraModem's PHY *already* runs convolutional FEC + a block
  interleaver. Wrapping chunks in a second RS layer would spend ~37 bytes of parity and
  header per chunk — minutes of airtime — to correct errors the modem has already
  corrected. Setting `nparity=0` passes chunks through unwrapped; measured on-air overhead
  drops to the 6-byte chunk header alone (<3%). **Losslessness is unaffected**: a chunk the
  PHY can't recover simply never arrives, which is exactly the gap Φ's NACK path handles.
- **`chunk_size = 256`.** At ~10 B/s a 1400-byte chunk is ~2.3 minutes, so one loss costs
  2.3 minutes; a 256-byte chunk costs ~25 s. Small enough to bound loss cost, large enough
  that the 6-byte header stays negligible.
- **`credit_window = 2`.** Authorizing chunks the link needs minutes to carry buys nothing;
  a shallow window keeps the NACK feedback tight.

Because Φ counts rounds rather than seconds, no timeout retuning is needed — the same
certified state machine drives Ethernet, WireGuard-over-IP, and a speaker-to-microphone
acoustic path.

## 8. Reference implementation

- **Control codec (certified):** `python/MCP/pipelab_core.py` (canonical), `codec/demod_pipe.h`
  (C), `codec/src/pipe.rs` (Rust). Vectors: `Documentation/pipe_vectors.json` (+ identical
  `python/MCP/` copy) and `codec/pipe_vectors.gen.h` (dependency-free C test).
- **Runtime (reference):** `python/dcf/pipe/` — pure `PipeSender`/`PipeReceiver` state
  machines (credit, FEC-wrapped chunks, SACK/NACK, checksum completion) and a `run_transfer`
  driver over an in-memory lossy channel. The state machines touch no socket, so they drive
  equally over UDP, `LoopbackTransport`, or a real link.
- **Positioning:** composes with **DCF-SPA** (a knock opens the data port), **DCF-Mesh**
  (election picks which node serves), and the **BEACON** clock (optional pacing). Reliability
  is not cryptography, so the export posture is unchanged.

```sh
python3 python/MCP/gen_pipe_vectors.py /tmp/pv.json            # regen + verify laws
cd codec && cargo test --test certify_pipe                     # Rust
gcc -std=c11 -I codec C_SDK/tests/test_pipe_certify.c -o /tmp/pc && /tmp/pc   # C
cd python && python3 -m unittest tests.test_pipe -v            # runtime: loopback + lossy
```

## 9. Non-goals

- **Confidentiality / integrity crypto.** None here — plaintext, EAR99, WireGuard beneath
  the socket (`DCF_SECURITY_EXPOSURE.md`). The FNV checksum detects accidental corruption,
  not tampering.
- **A new wire format.** The control messages are frame payloads; the data lane is a
  transport beneath the quantum. `golden_vectors.json` does not move.
- **Congestion fairness on the open internet.** The default is receiver credit for a known
  fleet; TCP-friendly sender windows are a future additive mode.

---

*[DeMoD LLC](https://DeMoD.ltd) — Cut the bullshit, cut the price. Innovation without the overhead.*
