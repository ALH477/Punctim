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
