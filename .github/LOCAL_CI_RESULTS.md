# Local CI results — `wire-certify.yml` run off-platform

> **Status note (2026-09-24): hosted GitHub Actions have never executed a real job
> for this repository.** All 57 `wire-certify.yml` runs (2026-06-10 → 2026-06-21)
> failed at startup within seconds (an account billing lock; no step ever ran), and
> no run was triggered by any later commit. Separately, the workflow file itself was
> **invalid YAML** from commit `70beec5` (committed 2026-08-16; an unquoted `run:`
> one-liner in the Lua transport step) until this fix, so it could not have run even
> with billing cleared. Until the owner clears the billing lock, **`make ci-local`**
> (`.github/ci-local.sh`) and the dated attestations in this file are the certification
> path of record. `workflow_dispatch` is now enabled on `wire-certify.yml`, `ci.yml`
> and `cmake-multi-platform.yml`, so a manual hosted run can be triggered as soon as
> Actions is unblocked.

The Wire Certification workflow (`.github/workflows/wire-certify.yml`) was executed
**locally**, job-for-job, instead of on GitHub Actions. Toolchains not installed on
the host were supplied hermetically with Nix (`nix shell nixpkgs#…`), exactly the
versions CI would provision. This file is a dated attestation, not a substitute for
the hosted run.

- **Date:** 2026-07-25
- **Commit:** `790ebae` (Phase 4 certification pass)
- **Host toolchains:** Python 3, Rust (cargo), GCC, Go 1.25, GHC 9.10 (host)
- **Nix:** Determinate Nix 3.16 (supplied jdk, kotlin, ghc, sbcl, swift)

## Result: focused re-run of the certification-tier jobs

This refresh targets the four languages whose tier changed (Haskell/Kotlin/Swift/
Lisp) plus the new/changed jobs (spa, c-sdk-unit, lisp full-vector). The host-native
adapter jobs (python/rust/c/go/…) are unchanged from the `394731f` run below and were
not re-executed here.

| Job | Status | How |
|-----|--------|-----|
| certify-lisp | ✅ PASS | nix sbcl — **full 109 encode + 137 syndrome + FEC vectors** read from the canonical JSON (upgraded from the embedded subset; still no Quicklisp) |
| certify-java | ✅ PASS | nix jdk — Certify + SuperPackCertify + **FECCertify** (all 246 + FEC) |
| certify-kotlin | ✅ PASS | nix kotlin+jdk — Frame+SuperPack+**FEC**+Certify → run (109 encode + 137 syndrome + FEC) |
| certify-haskell | ✅ PASS | nix ghcWithPackages — Certify.hs (109 encode + 137 syndrome + 7 superpack) |
| certify-swift | ⏭️ CI-only | the Nix Swift-on-Linux wrapper's `swift test` did not finish within the local budget; the `swift-actions` runner is authoritative (unchanged from `394731f`) |
| spa | ✅ PASS | host cargo (`cargo test`: token codec, §13 matrix, Python↔Rust cross-language) + python unittest |
| c-sdk-unit | ✅ PASS | host cmake — plain + **ASan+UBSan** + **TSan** (unit suite, regressions C4/H2/C6, streamdb C5) |

## Note

The `certify-lisp` job now certifies the entire golden certificate (not a hand-picked
subset) by reading `Documentation/{golden,fec}_vectors.json` through a small
dependency-free JSON reader added to `lisp/src/wire.lisp`. TSan's first run of the new
StreamDB C test found a real data race on the auto-flush `running` flag, now fixed.

---

## Prior full run (retained for the host-native jobs)

- **Date:** 2026-06-18 · **Commit:** `394731f` · **Result:** 15 PASS · 1 SKIP · 0 FAIL

| Job | Status |
|-----|--------|
| certify-python | ✅ PASS |
| certify-rust | ✅ PASS |
| certify-c | ✅ PASS |
| certify-cpp | ✅ PASS |
| certify-go | ✅ PASS |
| certify-perl | ✅ PASS |
| certify-audio | ✅ PASS |
| certify-game | ✅ PASS |
| certify-text | ✅ PASS |
| certify-node | ✅ PASS |
| certify-lua | ✅ PASS |
| certify-swift | ⏭️ SKIP (nix wrapper lacks `swift-test`) |
