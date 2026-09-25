# DCF-Exsecutor — the wire quantum certified in a freestanding systems language

DCF-Exsecutor is not a new wire format, transport, or adapter — it is a **binding**:
an independent implementation of the 17-byte `DeModFrame` codec, written in
[Exsecutor](https://github.com/ALH477/exsecutor), a freestanding systems language
by the same author (DeMoD LLC), and certified against this repo's own
`Documentation/golden_vectors.json`. It lives **upstream**, in Exsecutor's own
repository, not in this tree — see [Licensing boundary](#licensing-boundary) and
[What this is not](#what-this-is-not) below before assuming otherwise.

## What Exsecutor is

Exsecutor's compiler is `exsc`. Its governing thesis is stated plainly in its own
docs: **"Locale and target are capabilities, never ambient state."** Concretely,
for a wire protocol that means byte order and memory layout are not something you
write by hand and hope is consistent — they are expressed **in the type** at every
wire boundary. A field declared `u16:maior` is big-endian by construction; there is
no separate `htons`/`ntohs`-equivalent step to get right or forget.

Exsecutor is also **freestanding**: no libc, no dynamic linking, and syscalls only
from a closed allowlist enforced by `make audit`. For the DeModFrame binding
specifically, that allowlist is exactly `read`, `write`, `exit_group` — and,
notably, **no socket-family syscall is ever permitted**. A conformance test written
in Exsecutor cannot open a network connection even if it wanted to; it can only
read input, compute, and write output. That property is a natural fit for
certifying a wire *codec* in isolation from any transport, which is exactly what
Punctim's own certificate does (`golden_vectors.json` exercises encode/decode/CRC
in-process, never over a socket).

## The binding: entry 23

Exsecutor's own conformance suite includes an entry, `entry23`, dedicated to
Punctim's wire quantum:

- `tests/conformance/entry23_demodframe_golden_vectors.exsc` — the test driver
- `entry23/codex.exsc` — the DeModFrame codec (encode/decode/CRC), written in Exsecutor
- `entry23/probatio.exsc` — the test harness that runs the codec against the
  vendored vector set

`entry23` implements DeModFrame encode/decode in Exsecutor and certifies it against
a **vendored copy** of this repo's `Documentation/golden_vectors.json`. As of
2026-09-25, that vendored copy is **byte-identical** to the one shipped here
(sha256 `8d2b0e63c80826008b5f26e2434b1c972da424afe8b3d028245a5dcd8fba2056` for the
vectors file), and `WIRE_QUANTUM_SPEC.md` is vendored alongside it, also
byte-identical.

Run against the real thing (`nix develop --command bash tests/run.sh` in the
Exsecutor repo, verified 2026-09-25), entry 23 reports:

```
entry 23: certificate: 246/246 vectors (encode basis 109/109, syndrome basis 137/137)
entry 23: anchors: 3/3
entry 23: laws: 218/218 (section 4), 136/136 (section 5)
```

plus three deliberate mutation checks, each of which **must fail** for the test
itself to pass:

- `polynomial 0x1021 -> 0x1020 in redundantia` — fails at vector 0
- `numerus u16:maior -> u16:minor in the declaration` — fails at vector 5
- `versio and genus swapped in the declaration` — fails at vector 0

and two further checks: a second run of the codec writes the **byte-identical**
output stream (determinism), and a syscall audit confirms the built binary's only
syscalls are `read`, `write`, and `exit_group`.

**This mutation testing is something no other Punctim binding currently does.**
Every other certified language (C, Rust, Python, Go, Java, Node.js, Perl, C++,
Haskell, Kotlin, Swift, Lisp, and Lua's self-cert) proves it agrees with the
reference on all 246 vectors. Entry 23 additionally proves the certificate would
have *caught* it if the CRC polynomial, a field's byte order, or the frame's
`versio`/`genus` nibble packing had been wrong — i.e. that 246/246 is a meaningful
signal, not a vacuous one.

`codex.exsc` is also **pure**: every function is declared `publica` with no
`poscit` clause, meaning no capability (not even the syscall allowlist) is named
or available inside the codec itself. The codec cannot perform I/O; only the
surrounding driver can.

## The struct

The frame declaration, from Exsecutor's own spec (§5.2 worked example), is the
clearest illustration of "layout is in the type":

```
@transitus
publica structura DeModFrame {
    signum:  u8               // 0xD3, the first validity gate
    versio:  u4               // packs MSB-first with genus
    genus:   u4               // frame type
    numerus: u16:maior
    fons:    u16:maior
    meta:    u16:maior
    onus:    u32:maior
    tempus:  u24:maior        // 24-bit microsecond offset
    cursus:  u16:maior        // CRC-16/CCITT-FALSE over bytes 0..14
}
```

(`maior` = big-endian.) Every multi-byte field carries its endianness in its own
type annotation, and the `versio`/`genus` nibble pair packs MSB-first as declared —
there is no hand-written shift-and-mask to audit separately from the type. This is
the same 17-byte layout as `Documentation/WIRE_QUANTUM_SPEC.md`: `sync | flags(ver|type)
| seq | src | dst | payload(4B) | ts24 | crc16`.

## Licensing boundary

Exsecutor is licensed **GPL-3.0-or-later** with §7 additional permissions
(`LICENSE.EXCEPTION`, v2.0). Two exceptions matter here:

- **Exception A (compiler output)** — "You have permission to propagate Compiler
  Output under terms of your choosing," modelled on the GCC Runtime Library
  Exception, the FAUST notice, and the Bison parser exception. Anything `exsc`
  *compiles*, including the entry-23 codec and its test binary, carries no GPL
  propagation obligation on its own.
- **Exception B (designated runtime)** — a per-file exception covering runtime
  files linked into a compiled program.

Neither exception changes the direction of this binding, which is the reverse of
DCF-JANUS and DCF-Snake's quanta dependency (see `LICENSING.md`): those two shell
out to a separate GPL-3.0-or-later binary at runtime, from inside this LGPL-3.0-only tree.
**DCF-Exsecutor vendors nothing into this repo, and this repo vendors nothing into
Exsecutor.** The only thing that crosses the boundary is *data*: Exsecutor's
`entry23` vendors a copy of this repo's `golden_vectors.json` and
`WIRE_QUANTUM_SPEC.md` as reference material, with sha256 provenance recorded on
its side. `exsc` is never invoked by, linked into, or shipped with anything in this
tree. See [`LICENSING.md`](../LICENSING.md) for the full boundary statement.

## CI: drift detection, not a build dependency

A `certify-exsecutor` job in [`.github/workflows/wire-certify.yml`](../.github/workflows/wire-certify.yml)
checks out both this repo and the Exsecutor repo, fails if Exsecutor's vendored
`golden_vectors.json` has drifted from the one shipped here, and then runs entry 23
and gates on `246/246`.

The job's real purpose is **two-directional drift detection**. The failure mode it
exists to catch is: `golden_vectors.json` gets regenerated here (a codec change, a
new anchor, an added vector) and the Exsecutor repo's vendored copy is never
updated — so Exsecutor would keep certifying against a stale certificate and
silently stop meaning what it claims to mean. The CI job turns that silent drift
into a hard failure on this repo's own pull requests.

## What this is not

- **Punctim does not depend on Exsecutor to build, run, or certify anything.**
  `make certify`, `make ci-local`, and every `certify-<lang>` job in
  `wire-certify.yml` for the languages that live in this tree run exactly as they
  did before this binding existed.
- The binding **is** in-tree and certified here — `exsecutor/certify.sh` compiles
  `exsecutor/*.exsc` and checks the result against this repo's live
  `Documentation/golden_vectors.json` (246/246), which is the CI gate. What it is
  *not* is self-contained: certifying it needs a **`GPL-3.0-or-later` compiler**
  that this repo does not ship, so the job builds the pinned upstream `exsc` via
  `nix build .#exsc`. Without that toolchain `certify.sh` **skips** rather than
  fails, so a plain checkout with no Nix still runs every other certification.
- `exsc` is **not** relicensed and **not** linked — it is run as a separate
  process at *certification* time only, the same boundary as janus-c and quanta.
  No Punctim node shells out to it at runtime; nothing ships it.
- The relicensing is narrow and does not reach upstream. Only the files in
  `exsecutor/` carry the `LGPL-3.0-only` grant; their Exsecutor originals stay
  `GPL-3.0-or-later`. See [`LICENSING.md`](../LICENSING.md) — "an exception to the
  exception."
- Nothing about this binding touches the 246-vector certificate itself, the wire
  format, or any adapter. It is a second, independent set of eyes on the same
  17-byte layout — proof that the certificate is reproducible from a completely
  different type system and compiler, with no shared code.

## References

- Upstream: [`github.com/ALH477/exsecutor`](https://github.com/ALH477/exsecutor)
  (GPL-3.0-or-later + `LICENSE.EXCEPTION`), `tests/conformance/entry23_demodframe_golden_vectors.exsc`,
  `entry23/{codex,probatio}.exsc`.
- This repo: [`Documentation/WIRE_QUANTUM_SPEC.md`](WIRE_QUANTUM_SPEC.md) (the
  normative frame spec Exsecutor vendors), [`Documentation/golden_vectors.json`](golden_vectors.json)
  (the certificate Exsecutor vendors and certifies against), [`LICENSING.md`](../LICENSING.md)
  (the licensing boundary, including this binding).

```sh
# In the Exsecutor repo (not this one):
nix develop --command bash tests/run.sh
```
