# Licensing

This repository is **multi-licensed by scope**. When in doubt, the `SPDX-License-Identifier`
header at the top of a file is authoritative for that file.

## The library — LGPL-3.0-only

All linkable library code — the wire codec (`codec/`, `C_SDK/`), the SDKs (`rust/`, `python/`,
`lisp/`, and the other language bindings), and the tooling (`matrix-bridge/`, `client/`) — is
licensed under the **GNU Lesser General Public License v3.0 only** (`LGPL-3.0-only`). The full
text is in [`LICENSE`](LICENSE).

LGPL-3.0 lets you link DCF into proprietary applications, provided changes *to DCF itself* remain
under the LGPL and users can relink. This is the default for the whole tree.

### Why there are two licence texts

LGPLv3 is not a standalone licence. Its own first paragraph says it "incorporates the terms and
conditions of version 3 of the GNU General Public License, supplemented by the additional
permissions listed below" — so the LGPLv3 text alone (7.6 kB of *additional permissions*) is
incomplete without the GPLv3 it sits on top of. Both therefore ship here:

| File | Text | Role |
|---|---|---|
| [`LICENSE`](LICENSE) | GNU **Lesser** General Public License v3.0 | the licence this project grants |
| [`COPYING`](COPYING) | GNU General Public License v3.0 | the base terms LGPLv3 incorporates by reference |

`COPYING` does **not** mean any part of the library is GPL-licensed — the library is
`LGPL-3.0-only`, and the only GPL-scoped code in the repo is the DOOM example below. The FSF ships
these as `COPYING` + `COPYING.LESSER`; this repo keeps the LGPL text at `LICENSE` instead, because
that path is referenced by `CPACK_RESOURCE_FILE_LICENSE`, the per-language manifests and the
READMEs. Both files are verbatim FSF texts and must not be edited.

## The DOOM example — GPL-3.0

The DOOM integration example under [`C_SDK/examples/DOOM/`](C_SDK/examples/DOOM/) is licensed
**GPL-3.0**, because it links GPL-licensed game code. This is the *only* GPL-scoped part of the
repository; it is an example, not part of the linkable library, and does not affect the license
of anything else.

## The Lua framework — dual-licensed

The Lua DCF-Audio binding ([`lua/`](lua/), see [`lua/LICENSING.md`](lua/LICENSING.md)) is
**dual-licensed**: `LGPL-3.0-only` for open-source use, or a commercial license available from
DeMoD LLC on request. Dual-licensing is currently **scoped to Lua only**. DeMoD LLC is the sole
copyright holder and may extend dual-licensing to other components in the future; until then,
the rest of the tree is LGPL-3.0-only.

## DCF-JANUS — GPL-3.0 boundary (subprocess only)

The DCF `janus:` transport ([`python/dcf/transport.py`](python/dcf/transport.py),
[`Documentation/DCF_JANUS_SPEC.md`](Documentation/DCF_JANUS_SPEC.md)) interoperates with the
NATO STANAG-4748 standard by invoking the **GPL-3.0** janus-c reference (`janus-tx`/`janus-rx`)
as a **separate subprocess** — mere aggregation, exactly like the existing `pw-play`/`ffmpeg`
calls. janus-c is **never vendored or linked** into this `LGPL-3.0-only` tree; it is an
**optional, user-installed GPL dependency** (built by a standalone Nix derivation,
`nix build .#janus-c`, kept out of every LGPL package's closure). The transport raises (and
its tests skip) when janus-c is absent, so the LGPL library never depends on GPL code.

## DCF-Snake / quanta — GPL-3.0 boundary (subprocess only)

The DCF-Snake record plane ([`Documentation/DCF_SNAKE_SPEC.md`](Documentation/DCF_SNAKE_SPEC.md))
carries the DeMoD **quanta** codec by invoking the **GPL-3.0-only** (dual-licensed
`GPL-3.0-only OR DeMoD-Commercial`) `quanta-stream` / `quanta-stream-decode` binaries
(from the separate DeMoD `quanta` repository) as **separate subprocesses** — mere aggregation, exactly
like the existing janus-c and `pw-play`/`ffmpeg` calls. quanta is **never vendored or linked**
into this `LGPL-3.0-only` tree; it is an **optional, standalone GPL dependency** (built by its
own Nix derivation, `nix build .#quanta`, kept out of every LGPL package's closure —
`flake.nix`). The mixer/spoke nodes shell out to the `quanta-stream`/`quanta-stream-decode`
binaries at runtime (`$QUANTA_STREAM`/`$QUANTA_STREAM_DECODE`), so the LGPL library never
depends on GPL code.

## Exsecutor — GPL-3.0-or-later + §7 exceptions, vendored in the *other* direction

[Exsecutor](https://github.com/ALH477/exsecutor) (`exsc` compiler; same author,
DeMoD LLC) implements the DeModFrame wire codec as a conformance entry
(`entry23`) and certifies it against `Documentation/golden_vectors.json`. See
[`Documentation/DCF_EXSECUTOR.md`](Documentation/DCF_EXSECUTOR.md) for the full
binding writeup.

Exsecutor is licensed **GPL-3.0-or-later** with §7 additional permissions
(`LICENSE.EXCEPTION`, v2.0): **Exception A** covers *compiler output* — "You have
permission to propagate Compiler Output under terms of your choosing," modelled on
the GCC Runtime Library Exception, the FAUST notice, and the Bison parser
exception — so anything `exsc` compiles (including the entry-23 codec) carries no
GPL propagation obligation of its own. **Exception B** is a per-file exception
covering designated runtime files linked into a compiled program.

The dependency direction here is the **reverse** of the janus-c and quanta
boundaries above. Those two are cases of *this* LGPL-3.0-only tree shelling out to
a separate GPL-3.0-or-later binary at runtime. Exsecutor does the opposite: it is a
**separate GPL-3.0 repository that vendors a copy of this repo's certificate**
(`golden_vectors.json` and `WIRE_QUANTUM_SPEC.md`, verified byte-identical as of
2026-09-25) as reference data, and certifies its own independent codec
implementation against it. Consequently:

- **Nothing is vendored or linked in either direction.** `exsc` is never invoked,
  shelled out to, or shipped by anything in this tree; no Exsecutor source or
  binary is present here.
- Because of Exception A, even Exsecutor's own compiled test binary carries no GPL
  propagation obligation — but the question is moot for this repo regardless,
  since nothing here links against or redistributes it.
- The only thing that crosses the boundary is **data, one way**: Exsecutor's
  `entry23` vendors this repo's certificate and spec as read-only reference
  material with sha256 provenance recorded upstream. This repo does not vendor
  anything from Exsecutor.
- A `certify-exsecutor` CI job (`.github/workflows/wire-certify.yml`) checks out
  both repos to catch drift between this repo's `golden_vectors.json` and
  Exsecutor's vendored copy — it verifies the *data* stays in sync, not a build or
  license dependency.

## HydraModem — LGPL-3.0-only

The [`hydramodem/`](hydramodem/) directory is a self-contained acoustic M-FSK modem that carries
the 17-byte `DeModFrame` *opaquely* (a transport beneath the wire quantum). It originated as a
standalone Apache-2.0 release; on integration into this monorepo DeMoD LLC — its sole copyright
holder — **relicensed it to `LGPL-3.0-only`**, consistent with the rest of the tree
(`hydramodem/LICENSE`, `hydramodem/NOTICE`). Repo-specific glue under `hydramodem/dcf-tools/`
carries the standard `LGPL-3.0-only` SPDX header.

## Export compliance

DCF is **encryption-free by design** to remain outside EAR/ITAR licensing requirements; see
[`Documentation/Specs/export_compliance.markdown`](Documentation/Specs/export_compliance.markdown).
Do not add cryptography to the core wire path.

## SPDX headers

Every source file should carry an SPDX header matching its scope:

- Library / SDK / tooling: `SPDX-License-Identifier: LGPL-3.0-only`
- `C_SDK/examples/DOOM/`: `SPDX-License-Identifier: GPL-3.0-only`

Copyright © DeMoD LLC.
