# Exsecutor — the DeModFrame wire codec

A `DeModFrame` codec written in [Exsecutor](https://github.com/ALH477/exsecutor),
certified against this repo's live `Documentation/golden_vectors.json` — the same
246-vector certificate (109 encode + 137 syndrome) every other binding is held to.

```sh
./exsecutor/certify.sh          # or: make certify
```

```
section 1 (encode basis): 109/109
section 2 (syndrome basis): 137/137
section 3 (anchors): 3/3
section 4 (laws: decode verdicts, round trip): 218/218
section 5 (laws: decode order under one-bit corruption): 136/136
certificate: 246/246 vectors
```

## Why this binding is interesting

Exsecutor puts **byte order and layout in the type** at wire boundaries, so the
frame is *declared*, not hand-shuffled:

```
@transitus
publica structura DeModFrame {
    signum:  u8               // 0xD3, the first validity gate
    versio:  u4               // packs MSB-first with genus
    genus:   u4               // frame type
    numerus: u16:maior        // `maior` = big-endian
    fons:    u16:maior
    meta:    u16:maior
    onus:    u32:maior
    tempus:  u24:maior        // 24-bit microsecond offset
    cursus:  u16:maior        // CRC-16/CCITT-FALSE over bytes 0..14
}
```

`u24:maior` and the `u4`/`u4` pair are the parts other bindings write by hand.

## Files

| file | what it is |
|---|---|
| `demodframe.exsc` | the `DeModFrame` declaration above — the layout being certified |
| `codex.exsc` | the codec. Pure: no `poscit`, no `initium`, no capability reachable |
| `probatio.exsc` | the driver; writes the certification stream to stdout |
| `expecta.py` | builds the expected stream from `Documentation/golden_vectors.json` and compares section by section |
| `certify.sh` | compiles, runs, and compares. Skips cleanly without a toolchain |

The compile unit is **order-sensitive**: declaration, then codec, then driver.

## Toolchain

Certifying needs the `exsc` compiler, which this repo does not ship, and `fasmg`
to assemble what `exsc` emits. The only thing you must provide is **fasmg**:

```sh
nix shell nixpkgs#fasmg --command ./exsecutor/certify.sh
```

`certify.sh` supplies the rest itself — it takes `exsc` from `$EXSC`, or `PATH`,
or builds the pinned upstream one with `nix build .#exsc`, and it sets fasmg's
`$INCLUDE` from `nix build .#fasmg-x86` rather than inheriting it. That last part
matters: without it the assemble step dies on `source file 'format/format.inc'
not found`, and depending on the caller's environment for it would be precisely
the ambient state Exsecutor exists to avoid.

Missing any of that, it **skips** (exit 0) so a plain checkout still runs every
other certification — except under `PUNCTIM_REQUIRE_EXSECUTOR=1`, which CI sets
so a missing toolchain fails loudly instead of silently passing.

## Licensing — an exception to the exception

The files here are **`LGPL-3.0-only`**, like the rest of the linkable tree, by an
explicit grant recorded in each file's header. They originate in the Exsecutor
repository, which is `GPL-3.0-or-later`.

DeMoD LLC is the sole copyright holder of both projects, and a sole copyright
holder may license their own work under more than one licence — so this is
dual-licensing of specific files, not a conversion. **The Exsecutor originals
remain `GPL-3.0-or-later`, and no other Exsecutor source is relicensed by
implication.**

A fresh grant was needed because neither of Exsecutor's existing GPLv3 §7
exceptions reaches this case: Exception A covers compiler *output*, Exception B
covers designated *runtime files* linked into a program. Compiler **input**
source — which is what `codex.exsc` is — falls under neither. Hence a third,
deliberately narrow carve-out scoped to exactly this directory.

The **compiler is not relicensed and never linked**: `exsc` stays
`GPL-3.0-or-later` and is run as a separate process at certification time only,
the same boundary this repo already uses for janus-c and quanta. Full writeups:
[`../LICENSING.md`](../LICENSING.md) and
[`../Documentation/DCF_EXSECUTOR.md`](../Documentation/DCF_EXSECUTOR.md).

## Staying in sync with upstream

Bodies here are byte-identical to their Exsecutor originals apart from the licence
header, and the `certify-exsecutor` CI job warns if they drift. Upstream also runs
**mutation checks** this repo does not reproduce — a deliberately broken CRC
polynomial, field byte order, or nibble order must fail at a named vector, which
proves the certificate discriminates rather than merely that one implementation
agrees with itself.
