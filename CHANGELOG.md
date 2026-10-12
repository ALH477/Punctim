# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The project is pre-1.0; `1.0.0` is
reserved for when the full advertised language set is green in CI.

## [Unreleased]

### Added
- **Multi-kind agent presence & ping** (`matrix-bridge/dcf_presence.py`,
  `matrix-bridge/a2a_ping.py`, `exsecutor/agent/`) — a pinger that discovers and pings
  several kinds of agents on one machine over the DCF mesh: Python `DcfTextNode` agents
  (echo / coordinator / langgraph-llm / lua) over UDP, and an **Exsecutor runtime** agent
  over a stdio pipe.  Presence rides ordinary DCF-Text DATA frames distinguished by two new
  descriptor flag bits (`PING` 0x08, `PONG` 0x10 — opaque to L2, so the certified
  `text_vectors.json` is untouched); the payload is a deterministic `DCP1 <kind> <node_id>
  <name>` line, not JSON, so the Exsecutor agent can parse it byte-by-byte.  The Exsecutor
  agent (`exsecutor/agent/*.exsc`) is pure codec + a stdio driver: `rete` is not derivable
  yet, so it speaks the `stdio:` medium (read(0)/write(1)/exit_group only, the same audited
  set as the entry-23 certificate).  Selftest: `exsecutor/agent/selftest.sh` (build + PING→PONG
  byte-compared against a Python oracle + determinism + loud failure on a corrupt frame);
  `make test-a2a` runs the loopback demo (`a2a_ping.py demo`) which pings a Python ponger
  **and** the Exsecutor agent.  Nix apps: `#a2a-ping`, `#a2a-ping-demo`.
- **DCF-SPA** (`spa/`) — single-packet port authorization: an authentication-only (EAR99) side
  channel that gates HydraMesh data ports. Rust `dcf-spa-authorizer` (HMAC-SHA256 / Ed25519
  token verification, nonce-replay + freshness lifecycle, nftables granter) + `python/dcf/spa/`
  knock client; `certify-spa` CI job (unit + Rust↔Python cross-language). Spec:
  `Documentation/DCF_SPA_SPEC.md`. The wire certificate is untouched.
- **C SDK hardening + regression suite.** `DCF_SAN=address|thread|undefined` sanitizer configs;
  new `test_regressions` (C4/H2/C6) and `test_streamdb_regress` (C5, the first C-level StreamDB
  test) run under ASan+UBSan and TSan in a new `c-sdk-unit` CI job — the unit suite had never
  run in CI. All previously-orphaned C certify binaries are now CMake/ctest targets.
- **`DCF_BUILD_PLUGINS`** compile-only target + `include/dcf_transport_v1.h` (the real v1 plugin
  ABI), replacing the phantom `<dcf_sdk/...>` include path.

### Changed
- **Licensing made checkable.** `REUSE.toml` and `LICENSES/` declare the holder and licence of
  every file, and `reuse lint` runs in CI (`ci.yml`). `AUTHORS` credits the one outside
  contribution (`README.es-ES.md`, webbrain-one); StreamDB is documented as LGPL-2.1-or-later,
  as its headers say, not GPLv3; `GUI/THIRD_PARTY_NOTICES.md` lists the code and fonts the
  review page embeds; the unsupported "USAF Validated" claim is removed; `CONTRIBUTING.md`
  states the contributor-licence-agreement policy; `CODE_OF_CONDUCT.md` replaces a placeholder.
- **UDP receive truncation closed** across all C paths (MTU buffers + `MSG_TRUNC` fail-closed);
  `dcf_proto_serialize` takes a capacity; connpool/logging races fixed; `dcf_once` added.
- **Certification completed.** Haskell/Kotlin/Swift/Lisp are Certified (ungated `certify-*`
  jobs). Lisp now certifies the full 246 wire vectors + FEC set from the canonical JSON via a
  dependency-free in-tree reader (was an embedded subset); the `certify-lisp` Quicklisp step is
  `continue-on-error`.
- **Header→impl gap closed**: `dcf_interface.h`/`dcf_plugin_manager.h` moved to
  `include/experimental/`; every header left in `include/` has a shipping implementation.
- **Go SDK graduation** (`go/`). The Go binding goes from a certified wire codec to a full
  **stdlib-only** SDK mirroring the working Rust subset:
  - Certified **game** (`go/game`), **audio** (`go/audio`), and **text** (`go/text`) adapters,
    byte-checked against `Documentation/{game,audio,text}_vectors.json` + `pm_param_vectors.json`
    with the existing golden-vector loader; each self-certifies its framing anchor on import.
  - A UDP **`DcfNode`** (`go/node`): a `ProtoMessage` transport byte-identical to Rust's, a peer
    table with per-peer EMA RTT, PING/PONG, reliable-ARQ retransmit, a `MessageHandler` interface,
    and `SendGameDCF`/`SendAudioDCF`/`SendTextDCF` + `Reassemble{Game,Audio,Text}Payload` hookups.
    `TEXT_DCF=10` is a Go extension ahead of the Rust SDK (game+audio are wired there, text is not);
    flagged in source for back-port.
  - Tests: a two-node ephemeral-port UDP loopback (game + text reassembly, PING→PONG RTT) and a
    hand-computed `ProtoMessage` golden byte-vector. `certify-go` now runs `go vet ./...`,
    `go test ./...`, and `go test -race ./node/` — still stdlib-only and no-network.
  - Removed `go/src/dcf.go`, a broken gRPC stub (unbuildable; gRPC is out of scope for the
    stdlib-only Go SDK), which had made `go test ./...` fail with `[setup failed]`.

### Fixed
- Folded the remaining Lisp **HIGH-severity** fixes from `hydramesh-hotfix.lisp` into
  `lisp/src/hydramesh.lisp`: **F2** RTT pong now echoes the ping timestamp (cross-process RTT was
  garbage), **F3** `stop-udp-endpoint` closes the socket before joining the receiver (no shutdown
  hang), **F5** `jref` matches cl-json's mangled keys case/punctuation-insensitively, **F6**
  `save-state`/`restore-state` persist peers as `{"peers":[...]}`, **F7** `dcf-db-insert` accepts
  string | octet-vector | any object (no more `aref` on a list), **F9** `dcf-benchmark` records
  only strictly-positive RTT samples. `hydramesh-hotfix.lisp` is now a thin compatibility shim —
  every fix lives in source, which self-certifies on load.

### Changed
- `flake.nix`: added `meta.license = lgpl3Only` to every derivation; fixed three source paths
  (`nodejs` → `JS/nodejs`, `docs` → `Documentation`, `streamdb` → `lisp/streamdb`); switched
  `dcf-rust` to a deterministic `cargoLock.lockFile` (no precomputed hash). No `lib.fakeHash`
  placeholders remain: Go uses `vendorHash = null` (stdlib-only), nodejs needs no lockfile hash,
  and streamdb is a C derivation (not Rust). Added the `dcf-spa-authorizer` package.

## [0.3.0] - 2026-06-17

First tagged release — the repository legitimacy program. Every advertised language now has a
golden-vector-certified (or CI-gated) `DeModFrame` wire codec, every known critical bug is
closed (C1–C9), and the documentation advertises only what CI backs.

### Added
- **Wire-codec bindings across the whole language set.** From-scratch 17-byte `DeModFrame`
  codecs, each byte-identical to the Python reference and certified against all 246 golden
  vectors, with a `certify-<lang>` CI job in `wire-certify.yml` and `SPDX` headers:
  - **Certified** (verified locally + CI): **Go** (`go/dcf/`), **Java** (`java/com/demod/dcf/`),
    **Node.js** (`JS/nodejs/`), **Perl** (`perl/`), **C++** (`cpp/include/dcf/`) — joining the
    existing C / Rust / Python / Lua.
  - **Design** (written + CI-gated): **Haskell** (`haskell/`, made to compile + a cabal cert),
    **Kotlin** (`kotlin/`, a Gradle module with a UDP `DcfNode.kt`), **Swift** (`swift/`, a real
    SwiftPM package), and a dependency-free Lisp `lisp/src/wire.lisp`.
- DCF-Text: a certified text adapter over `DeModFrame` (`python/MCP/textlab_core.py`).
- Multi-agent client integration: `matrix-bridge/a2a.py` (`config`/`send`/`recv`), a per-agent
  MCP config generator, an OpenClaw ClawHub skill, a non-MCP CLI bridge, and a shared HTTP mesh
  service (see `Documentation/AGENT_CLIENTS.md`).
- Governance/licensing docs: `LICENSING.md`, `SECURITY.md`, `AUTHORS`, this `CHANGELOG.md`,
  `Documentation/DCF_BACKLOG.md`, and `RELEASING.md`.
- `SPDX-License-Identifier: LGPL-3.0-only` headers across the certified core (`codec/`, `C_SDK/`,
  `python/MCP/`, `rust/`) and every new binding.
- README **Language status** table (Certified / Design / Experimental) that tracks CI reality,
  plus a wire-certify CI badge.

### Changed
- Truth-in-advertising pass on `README.md`, `CLAUDE.md`, `C_SDK/README.md`: replaced
  "11 production-ready bindings" / "5.2.0" with the tiered status; flagged Dijkstra / TUI / AUTO
  / AI-topology as planned; scoped the DCF-Audio "byte-certified" claim to L2 framing + PCM-diag
  bytes + PM param layout (Opus output and PM synthesis audio are **not** byte-certified);
  replaced non-compiling C client examples.
- `Documentation/Specs/export_compliance.markdown`: license label GPL-3.0 → LGPL-3.0.
- Reconciled component versions to `0.3.0` (`codec/`, `rust/`, `client/`, `flake.nix`).
- Fixed the `flake.nix` C-SDK source path case (`c_sdk` → `C_SDK`).
- `mesh_mcp.py`: configurable HTTP transport port (`DCF_MCP_HTTP_PORT`); default 8765 preserved.
- Expanded `.gitignore` (C/JVM/Node/Lisp/Swift/CMake build artifacts).

### Removed
- An offensive line in `lisp/README.md` acknowledgements.
- Non-functional stubs replaced by real codecs: `swift/package.swift` (empty, mis-cased) and the
  Kotlin gRPC sketch `Networking.kt`.
- `C_SDK/c_sdk_fixes.patch` — redundant; its C1–C6 fixes are already applied in source.

### Fixed
- Lisp wire-codec criticals folded from `lisp/hydramesh-hotfix.lisp` into source
  (`lisp/src/hydramesh.lisp`): **C7** the vacuous `crc16-ccitt` (a shadowing inner `let*` made it
  return `#xFFFF` for all input, so wire validity was vacuous), **C8** the `dcf-stop`
  use-after-free, and **C9** the `collect-streamdb-results` `Result*` linked-list ABI; plus a
  load-time self-cert and the `certify-lisp` CI job so the CRC can never silently regress.
- Haskell `FrameSpec.hs` no longer fails to compile (ambiguous `seq`, missing `xor`/`Bits`
  imports, and an `Integral`-less `hi`/`lo` signature).

[Unreleased]: https://github.com/ALH477/HydraMesh/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ALH477/HydraMesh/releases/tag/v0.3.0
