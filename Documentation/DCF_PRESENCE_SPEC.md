# DCF-Presence — discover and ping multi-kind agents over the wire

**Version 1** · DeMoD LLC · This document is normative. Companion to
[`DCF_TEXT_SPEC.md`](DCF_TEXT_SPEC.md) and [`WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md).

DCF-Presence is an **adapter** over DCF-Text (which is itself an adapter over the
17-byte `DeModFrame` quantum).  It introduces **no new wire format**: a presence
probe (PING) and reply (PONG) are ordinary DCF-Text DATA frames, told apart from
chat by the descriptor's **flag byte**, which L2 treats as opaque — so the certified
`text_vectors.json` certificate is **untouched**.

## Flags

DCF-Text's descriptor flags (`payload[2]` of the descriptor frame) are opaque to L2.
Bits 0–2 are `AGENT` / `MORE` / `RELIABLE` (`DCF_TEXT_SPEC.md`).  DCF-Presence claims
two of the reserved bits:

| Bit | Name | Value | Meaning |
|-----|------|-------|---------|
| 3 | `PING` | 0x08 | presence probe: "what agents are here, and what kind?" |
| 4 | `PONG` | 0x10 | presence reply: "I am here, I am kind K" |

A PING carries `FLAG_AGENT | FLAG_PING` (0x09); a PONG carries
`FLAG_AGENT | FLAG_PONG` (0x11).  Because the flags are opaque to L2, a PING of a
given text and a bare chat message of the same text produce **byte-identical**
framing — only the flag byte and the delivery path differ.

## The DCP1 line

A presence message's payload is one deterministic line, **not JSON**:

```
DCP1 <kind> <node_id_hex> <name>\n
```

- `kind` — the agent kind, constrained to `[a-z0-9-]{1,32}` (e.g. `echo`,
  `coordinator`, `langgraph-llm`, `lua`, `exsecutor`).
- `node_id_hex` — `0x` followed by exactly four lowercase hex digits.
- `name` — a human label, constrained to `[a-z0-9-]{1,32}`.
- The line is LF-terminated, UTF-8, space-separated, no quoting.

Example: `DCP1 exsecutor 0x00c3 exs-agent\n`.

The line is deliberately minimal so that **any** binding can parse it with a byte
scan — including Exsecutor, which has no JSON parser and no `textus` methods in its
lowering yet.

## Protocol

1. A **pinger** sends a PING: a DCF-Text message whose descriptor flags are
   `FLAG_AGENT | FLAG_PING`, addressed to a channel (`dst = crc16(channel)`) or
   broadcast (`dst = 0xFFFF`).
2. Every agent listening on that channel reads the descriptor's flags.  If the
   PING bit is set and the PONG bit is clear, the agent answers with a PONG: a
   DCF-Text message whose descriptor flags are `FLAG_AGENT | FLAG_PONG`, carrying
   a `DCP1` line with its own kind, node id, and name.
3. The pinger folds PONGs into a **roster** keyed by node id, in first-seen
   (insertion) order.  Re-seeing a node id refreshes its record in place.

An agent that is **not** being pinged (chat traffic, or a PONG arriving at a
pinger) is simply not answered.  There is no handshake and no connection — the
same "rendezvous, not a connection" model as the rest of DCF.

## The Exsecutor agent

The Exsecutor presence agent (`exsecutor/agent/`) answers PINGs from an Exsecutor
runtime.  Exsecutor's `rete` (network) atom is declared (`exsecutor` spec §4.6) but
**not derivable** (§4.7), and the prelude's syscall table lists `rete` as `[OPEN]`
with no routines — so an `.exsc` program cannot open a UDP socket.  The agent instead
speaks the **`stdio:` medium** (`DCF_MEDIUM_SPEC.md`, stream class): the host driver
feeds it whole 17-byte `DeModFrame`s on **stdin** and reads whole frames from
**stdout**.  Its audited syscall set is `read(0)` / `write(1)` / `exit_group(231)` —
the same set the entry-23 certificate binary audits to.

The agent implements the DCF-Text L2 framing itself, minimally, for the one message
shape it needs: a presence payload is ≤ 40 bytes, so it always fits in one data
fragment after the descriptor.  A PING arrives as two frames:

```
  descriptor  frag_idx=0  payload = [len_hi, len_lo, PING_flags, 0]
  data        frag_idx=1  payload = the DCP1 line, zero-padded to 4
```

Its identity (kind, node id, name) is **compiled in** as literals: Exsecutor has no
argv and no getenv (`ambitus` beyond the standard streams is `[OPEN]`).  Different
kinds of Exsecutor agent are different builds.

## Certification

DCF-Presence adds **no** vectors: the flag bits are opaque to L2 and the `DCP1` line
is application payload.  The 246-vector wire certificate and the `text_vectors.json`
certificate are **untouched**.  What is tested instead:

- `matrix-bridge/dcf_presence.py` — selftest: line codec round-trip + rejection of
  malformed lines + flag predicates + roster fold (insertion order, dedup).
- `matrix-bridge/tests/test_a2a_ping.py` — two-node PING/PONG loopback over UDP,
  channel isolation, roster fold across multiple kinds.
- `exsecutor/agent/selftest.sh` — builds the Exsecutor agent, pipes a PING in,
  compares the PONG frames byte-for-byte against a Python-computed oracle, checks
  determinism (two runs byte-identical), and checks that a corrupt frame fails
  loudly.  Skips cleanly without `exsc`/`fasmg`; fails loudly under
  `PUNCTIM_REQUIRE_EXSECUTOR=1`.

```sh
make test-a2a                          # all of the above (loopback)
python3 matrix-bridge/dcf_presence.py   # line codec + roster selftest
./exsecutor/agent/selftest.sh          # Exsecutor agent (skips without toolchain)
```

## Security & export posture

Like the rest of DCF, presence traffic is **plaintext** — kind, node id, name, and
timing are fully readable by any on-path observer.  This is the wire's design (see
[`DCF_SECURITY_EXPOSURE.md`](DCF_SECURITY_EXPOSURE.md)); run inside a VPN underlay.
Do not put secrets or personally identifying names on the mesh.
