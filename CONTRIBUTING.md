# Contributing to Punctim / DCF

Thanks for helping build the DeMoD Communication Framework. This is the **human**
companion to [`CLAUDE.md`](CLAUDE.md); read [`ARCHITECTURE.md`](ARCHITECTURE.md)
for the repo map and [`Documentation/DCF_CODE_REVIEW.md`](Documentation/DCF_CODE_REVIEW.md)
for frank, module-by-module status before trusting any module's surface area.

## The one rule: the certificate is the contract

Punctim has a single wire invariant — the **17-byte `DeModFrame` quantum** — and
everything else (audio, game, transports) is an **adapter** over it. The reference
implementations across C, Rust, Python, Lua, Haskell, and Lisp are kept **byte-identical**
by finite golden-vector certificates:

- Wire: `Documentation/golden_vectors.json` (246 vectors).
- Audio: `Documentation/audio_vectors.json` (+ `pm_param_vectors.json`).

**If you touch any codec, you must regenerate the vectors and run every cert.** CI
(`.github/workflows/wire-certify.yml`) fails on drift, and the committed copies in
`Documentation/` and `python/MCP/` must stay identical.

```sh
make certify          # runs the Python/Rust/C wire + audio certs (see Makefile)
# or, by hand:
python3 python/MCP/verify_laws.py /tmp/gv.json        # wire: regenerate + verify laws
python3 python/MCP/gen_audio_vectors.py /tmp/av.json  # audio: regenerate + verify laws
cd codec && cargo test --test certify --test certify_audio
gcc -std=c11 -I codec C_SDK/tests/test_wire_certify.c -lm -o /tmp/wc && /tmp/wc
```

## Getting set up

Pick one toolchain path (all are documented in the [README](README.md#quick-start)):

```sh
nix develop          # all toolchains in one shell (recommended)
./install_deps.sh    # distro-aware native install (Debian/Arch/Fedora)
docker build -t punctim .
make help            # list every task: setup / certify / test / docs / client
```

## Per-language build & test

| Dir | Build | Test |
|-----|-------|------|
| `codec/` (Rust wire+audio) | `cargo build` (`--features opus,pm`) | `cargo test` |
| `rust/` (gRPC SDK) | `cargo build` | `cargo test` |
| `python/` | `pip install -r python/requirements.txt` | `pytest python/tests/` |
| `go/` | `go build ./...` | `go test ./...` |
| `C_SDK/` | `cd C_SDK && mkdir build && cd build && cmake .. && make` | `ctest` |
| `client/` (Tauri) | `nix develop .#comms && cd client && npm install && cargo tauri dev` | `cd client/src-tauri && cargo test` |
| `Documentation/` (Sphinx) | `make docs` | — |

The C SDK ships only four modules (`dcf_platform`, `dcf_error`, `dcf_ringbuf`,
`dcf_connpool`); `include/experimental/`, `plugins/experimental/`, `tests/legacy/`
are **quarantined** — don't expect them to build (see `ARCHITECTURE.md`).

## Conventions

- **Adapters, not new wire formats.** New transports/codecs/message types are
  adapters over `DeModFrame`. Don't invent a second wire format.
- **No encryption in the core wire path.** The protocol is deliberately
  encryption-free for EAR/ITAR export compliance
  (`Documentation/Specs/export_compliance.markdown`).
- **A change to any codec is a change to all of them** — keep them byte-identical
  and regenerate vectors.
- **License:** the linkable library is **LGPL-3.0**; GPL-3.0 is scoped to the DOOM
  example only. New files should carry an `SPDX-License-Identifier: LGPL-3.0-only`
  header where the surrounding code does.

## Pull requests

1. Branch off `main` (`git checkout -b feature/xyz`); never commit straight to `main`.
2. Keep changes focused; match the style of the surrounding code (`black`, `clang-format`,
   `cargo fmt`, `perltidy`, etc.).
3. Run the relevant tests and **`make certify`** if you touched the wire/audio path.
4. Open a PR with the [pull request template](.github/PULL_REQUEST_TEMPLATE.md),
   filling in what you changed, how you tested, and confirming certs pass.
5. Be kind — see [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

Questions? Open a [GitHub issue](https://github.com/ALH477/DeMoD-Communication-Framework/issues)
or start a discussion.
