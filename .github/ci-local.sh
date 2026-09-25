#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
#
# ci-local.sh — run the Wire Certification workflow (.github/workflows/wire-certify.yml)
# job-for-job on this machine instead of GitHub Actions.
#
# Every job in the workflow has a shell function of the SAME NAME below, run in the SAME
# ORDER (the JOBS array), mirroring that job's steps. Host toolchains are used where
# present; a missing one (node, lua, sbcl, jdk, kotlin, ghc, swift) is supplied with
# `nix shell nixpkgs#…` when nix is installed, and otherwise the job is SKIPPED with the
# reason. Each job prints one line — `PASS name`, `FAIL name`, or `SKIP name (reason)` —
# and a summary follows. Exit status is non-zero iff a job FAILED (SKIP does not fail).
# A PASS with a "partial:" note ran every step except the ones named there.
#
# Hosted Actions have never run a real job for this repo (billing lock since 2026-06);
# until that clears, this script + .github/LOCAL_CI_RESULTS.md are the path of record.
#
#   make ci-local                              # or: bash .github/ci-local.sh
#   bash .github/ci-local.sh certify-medium io-matrix   # run only the named jobs
#   bash .github/ci-local.sh --list            # print the jobs in workflow order
#   CI_LOCAL_REPORT=1 make ci-local            # also APPEND a dated section to
#                                              #   .github/LOCAL_CI_RESULTS.md
#   CI_LOCAL_KEEP=1 …                          # keep the per-job logs (kept on FAIL anyway)
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
ROOT="$PWD"
WORKFLOW=.github/workflows/wire-certify.yml

# The workflow's jobs, in workflow order. Keep in sync with $WORKFLOW (checked below).
JOBS=(
  certify-python certify-c c-sdk-unit certify-hydramodem certify-rust spa certify-wasm
  certify-audio certify-game certify-text certify-qkd qkd-bridge certify-sstv certify-snake
  certify-lua certify-go certify-haskell certify-java certify-minecraft certify-kotlin certify-node
  certify-perl certify-cpp certify-swift certify-lisp certify-medium io-matrix
  certify-exsecutor
)

if [ "${1:-}" = "--list" ]; then printf '%s\n' "${JOBS[@]}"; exit 0; fi

WORK="$(mktemp -d)"
KEEP_WORK="${CI_LOCAL_KEEP:-0}"
trap '[ "$KEEP_WORK" = 1 ] || rm -rf "$WORK"' EXIT
if [ -t 1 ]; then G="\033[32m"; Y="\033[33m"; Rd="\033[31m"; Z="\033[0m"; else G=""; Y=""; Rd=""; Z=""; fi
NPROC="$(nproc 2>/dev/null || echo 2)"
declare -a SUMMARY=()

have() { command -v "$1" >/dev/null 2>&1; }
HAVE_NIX=0; have nix && HAVE_NIX=1

# ── helpers usable inside a job function (each job runs in its own `set -e` subshell) ──
CUR_JOB=""
# skip_job <reason> : end this job as SKIP (a missing toolchain — never a failure).
skip_job() { printf '%s\n' "$*" >"$WORK/$CUR_JOB.skip"; exit 200; }
# note <text> : the job still PASSes, but this step did not run here (reported as partial).
note() { printf '%s\n' "$*" >>"$WORK/$CUR_JOB.note"; echo "NOTE: $*"; }
# step <title> : a log marker per workflow step.
step() { echo; echo "--- step: $*"; }
# need <cmd> [nixpkgs attr…] : make <cmd> available (host first, then nix), else SKIP.
need() {
  local cmd="$1"; shift
  have "$cmd" && return 0
  if [ "$HAVE_NIX" = 1 ] && [ "$#" -gt 0 ]; then
    local pkgs=() p; for p in "$@"; do pkgs+=("nixpkgs#$p"); done
    local np; np="$(nix shell "${pkgs[@]}" -c printenv PATH)" || skip_job "nix could not provide $cmd (${pkgs[*]})"
    export PATH="$np"
    have "$cmd" && { echo "(using $cmd from nix: ${pkgs[*]})"; return 0; }
    skip_job "no $cmd on host; nix ${pkgs[*]} did not provide it"
  fi
  if [ "$#" -gt 0 ]; then skip_job "no $cmd on host and no nix"; else skip_job "no $cmd on host"; fi
}
# vdiff <family> : diff a regenerated $WORK/<family>_vectors.{json,gen.h} against every
# committed copy (Documentation/, python/MCP/, codec/).
vdiff() {
  diff "$WORK/$1_vectors.json"  "Documentation/$1_vectors.json"
  diff "$WORK/$1_vectors.json"  "python/MCP/$1_vectors.json"
  diff "$WORK/$1_vectors.gen.h" "codec/$1_vectors.gen.h"
}
# ccert <name> <src> : compile a dependency-free C certification test and run it.
ccert() {
  gcc -std=c11 -Wall -Wextra -I codec -o "$WORK/$1" "$2" -lm
  "$WORK/$1"
}

# ══ jobs (same names + order as wire-certify.yml) ═══════════════════════════════

certify-python() {
  need python3
  python3 -c 'import numpy' 2>/dev/null || skip_job "python3 lacks numpy (CI: pip install mcp numpy)"
  step "Verify wire laws and regenerate golden vectors"
  python3 python/MCP/verify_laws.py "$WORK/golden_vectors.json"
  step "Diff the regenerated C mirror of the certificate"
  diff "$WORK/golden_vectors.gen.h" codec/golden_vectors.gen.h
  step "Diff regenerated golden vectors against committed"
  python3 - "$WORK/golden_vectors.json" <<'PY'
import json, sys
committed = json.load(open('Documentation/golden_vectors.json'))
regenerated = json.load(open(sys.argv[1]))
assert committed['anchors'] == regenerated['anchors'], 'anchors mismatch'
assert len(committed['encode_basis']) == len(regenerated['encode_basis']), 'encode_basis length mismatch'
assert len(committed['syndrome_basis']) == len(regenerated['syndrome_basis']), 'syndrome_basis length mismatch'
for i, (c, r) in enumerate(zip(committed['encode_basis'], regenerated['encode_basis'])):
    assert c['frame'].lower() == r['frame'].lower(), f'encode_basis[{i}] mismatch'
for i, (c, r) in enumerate(zip(committed['syndrome_basis'], regenerated['syndrome_basis'])):
    assert int(c['syndrome']) == int(r['syndrome']), f'syndrome_basis[{i}] mismatch'
print('golden_vectors.json: committed and regenerated match')
PY
  step "Run certify_sdk self-test"
  python3 python/MCP/certify_sdk.py --selftest
  step "Run MCP self-test"
  if python3 -c 'import mcp' 2>/dev/null; then python3 python/MCP/wirelab_mcp.py --selftest
  else note "wirelab_mcp --selftest not run (python 'mcp' package not installed)"; fi
  step "Run DCF-Radio + dcf-rec unit tests"
  python3 -m unittest python/tests/test_radio.py python/tests/test_dcf_rec.py -v
  step "SuperPack — self-test, regenerate vectors, diff the committed copies"
  python3 python/MCP/superpack.py
  python3 python/MCP/gen_superpack_vectors.py "$WORK/superpack_vectors.json"; vdiff superpack
  step "FEC — regenerate Reed-Solomon vectors, diff the committed copies, test"
  python3 python/MCP/gen_fec_vectors.py "$WORK/fec_vectors.json"; vdiff fec
  python3 -m unittest python/tests/test_fec.py -v
  step "DCF-Pipe — regenerate control vectors, diff copies, verify transfer laws"
  python3 python/MCP/gen_pipe_vectors.py "$WORK/pipe_vectors.json"; vdiff pipe
  python3 python/dcf/pipe/laws.py
  ( cd python; python3 -m unittest tests.test_pipe -v )
  step "HydraPack — regenerate vectors, diff the committed copies"
  python3 python/MCP/gen_hydrapack_vectors.py "$WORK/hydrapack_vectors.json"; vdiff hydrapack
  step "Multi-Control — regenerate vectors, diff the committed copies"
  python3 python/MCP/gen_pipemulti_vectors.py "$WORK/pipemulti_vectors.json"; vdiff pipemulti
  step "SDR/IQ + acoustic + egress + transport/bridge — loopback tests + smokes"
  python3 -m unittest python/tests/test_iq_loopback.py python/tests/test_acoustic.py \
                      python/tests/test_egress.py python/tests/test_transport.py \
                      python/tests/test_bridge.py -v
  python3 python/dcf/bridge.py --demo
  local mod
  for mod in gfsk msk fsk4; do
    python3 python/modem/sdr.py tx --text "DCF!" --mod "$mod" --iq "$WORK/d.cf32"
    python3 python/modem/sdr.py rx --iq "$WORK/d.cf32" --mod "$mod" | tee "$WORK/rx.txt"
    grep -q "44434621" "$WORK/rx.txt"
    echo "dcf-sdr $mod cf32 round-trip: recovered \"DCF!\""
  done
  python3 python/modem/acoustic.py tx --text "DCF!" --profile handheld --wav "$WORK/a.wav"
  python3 python/modem/acoustic.py rx --profile handheld --wav "$WORK/a.wav" | tee "$WORK/arx.txt"
  grep -q "44434621" "$WORK/arx.txt"
  echo "acoustic WAV round-trip: recovered \"DCF!\""
  python3 python/modem/uplink_demo.py >/dev/null
  echo "uplink_demo: ok"
  step "Modulation — self-test, regenerate vectors, diff the committed copies"
  python3 python/MCP/modulationlab_core.py
  python3 python/MCP/gen_modulation_vectors.py "$WORK/modulation_vectors.json"; vdiff modulation
  step "Mesh — self-test, regenerate vectors, diff the committed copies"
  python3 python/MCP/meshlab_core.py
  python3 python/MCP/gen_mesh_vectors.py "$WORK/mesh_vectors.json"; vdiff mesh
  step "Vector copies — python/MCP/*_vectors.json byte-identical to Documentation/ (all families)"
  local fail=0 n=0 req doc mcp
  for req in golden audio pm_param; do
    [ -f "Documentation/${req}_vectors.json" ] || { echo "MISSING required family: $req"; fail=1; }
  done
  for doc in Documentation/*_vectors.json; do
    mcp="python/MCP/$(basename "$doc")"
    if [ ! -f "$mcp" ]; then echo "MISSING copy: $mcp"; fail=1; continue; fi
    if cmp -s "$doc" "$mcp"; then n=$((n+1)); echo "ok  $(basename "$doc")"
    else echo "DRIFT: $doc != $mcp"; diff "$doc" "$mcp" | head -20 || true; fail=1; fi
  done
  for mcp in python/MCP/*_vectors.json; do
    [ -f "Documentation/$(basename "$mcp")" ] || { echo "MISSING copy: Documentation/$(basename "$mcp")"; fail=1; }
  done
  echo "$n vector families: python/MCP/ == Documentation/"
  [ "$fail" = 0 ]
}

certify-c() {
  need gcc; need cmake
  step "Build + run C wire certification"
  ccert wire_certify C_SDK/tests/test_wire_certify.c
  local t
  for t in superpack fec pipe hydrapack pipemulti modulation mesh; do
    step "Build and run C $t certification"
    ccert "test_${t}_certify" "C_SDK/tests/test_${t}_certify.c"
  done
  step "node/modem (cmake)"
  cmake -S C_SDK -B "$WORK/cbuild" -DDCF_BUILD_NODE=ON -DDCF_BUILD_TESTS=ON -DDCF_BUILD_EXAMPLES=OFF
  cmake --build "$WORK/cbuild" -j"$NPROC" --target dcfnode test_proto_certify test_modulation_certify test_mesh_certify test_modem_loopback test_modem_fec
  ctest --test-dir "$WORK/cbuild" -R "proto_certify|modulation_certify|mesh_certify|modem_loopback|modem_fec" --output-on-failure
}

c-sdk-unit() {
  need cmake; need gcc
  step "Plain build + full ctest (unit, regressions, all C certs)"
  cmake -S C_SDK -B "$WORK/cb" -DDCF_BUILD_NODE=ON -DDCF_BUILD_TESTS=ON -DDCF_BUILD_EXAMPLES=OFF
  cmake --build "$WORK/cb" -j"$NPROC"
  ctest --test-dir "$WORK/cb" --output-on-failure
  step "ASan+UBSan (unit + regressions + streamdb)"
  cmake -S C_SDK -B "$WORK/cb-asan" -DDCF_SAN=address -DDCF_BUILD_NODE=OFF -DDCF_BUILD_TESTS=ON -DDCF_BUILD_EXAMPLES=OFF
  cmake --build "$WORK/cb-asan" -j"$NPROC" --target dcf_tests test_regressions test_streamdb_regress
  ctest --test-dir "$WORK/cb-asan" -R "unit_tests|regressions|streamdb_regress" --output-on-failure
  step "TSan (unit + regressions + streamdb)"
  # TSan rejects high ASLR entropy; CI lowers it with sudo. Best-effort here too.
  if [ "$(cat /proc/sys/vm/mmap_rnd_bits 2>/dev/null || echo 28)" -gt 28 ]; then
    { sysctl -w vm.mmap_rnd_bits=28 || sudo -n sysctl -w vm.mmap_rnd_bits=28; } >/dev/null 2>&1 || true
  fi
  cmake -S C_SDK -B "$WORK/cb-tsan" -DDCF_SAN=thread -DDCF_BUILD_NODE=OFF -DDCF_BUILD_TESTS=ON -DDCF_BUILD_EXAMPLES=OFF
  cmake --build "$WORK/cb-tsan" -j"$NPROC" --target dcf_tests test_regressions test_streamdb_regress
  ctest --test-dir "$WORK/cb-tsan" -R "unit_tests|regressions|streamdb_regress" --output-on-failure
}

certify-hydramodem() {
  need make; need cc
  step "HydraModem — full suite (unit/fuzz/stream/channel/loopback) + sanitizers"
  ( cd hydramodem; make check; make asan )
  step "HydraModem — DCF interop (a real DeModFrame survives TX->RX byte-exact)"
  ( cd hydramodem/dcf-tools; ./build.sh; ./build/dcf_loopback )
  step "HydraModem — hydra_symbols vectors vs the real hydra_frame_build (+ TX->RX per case)"
  ( cd hydramodem/dcf-tools; ./build/hydra_symbols_certify )
  step "HydraModem — Faust DSP compile-check (continue-on-error in CI)"
  if have faust; then ( cd hydramodem && make validate ) || echo "faust validate failed (continue-on-error in CI)"
  else echo "faust not installed; skipping (as on the CI runner)"; fi
}

certify-rust() {
  need cargo
  step "Run Rust wire certification"
  local t
  for t in certify certify_superpack certify_modulation certify_mesh certify_fec certify_pipe certify_hydrapack certify_pipemulti; do
    ( cd codec; cargo test --test "$t" )
  done
}

spa() {
  need cargo; need python3
  step "Rust authorizer tests (+ Python cross-language)"
  ( cd spa; cargo test )
  step "Python knock-client tests"
  ( cd python; python3 -m unittest tests.test_spa -v )
}

certify-wasm() {
  have wasm-pack || skip_job "no wasm-pack (CI installs it with the rustwasm installer)"
  need rustup
  rustup target list --installed | grep -qx wasm32-unknown-unknown \
    || skip_job "rust target wasm32-unknown-unknown not installed (rustup target add wasm32-unknown-unknown)"
  need node nodejs
  step "Build the WASM codec"
  wasm-pack build codec-wasm --target web --out-dir ../web/src/wasm --out-name dcf_codec_wasm
  node web/scripts/inline-wasm.mjs
  step "Certify WASM is byte-identical to the golden vectors"
  node web/certify/certify_wasm.mjs
}

certify-audio() {
  need python3; need gcc; need cargo
  step "Regenerate audio vectors and diff against committed"
  python3 python/MCP/gen_audio_vectors.py "$WORK/audio_vectors.json"
  diff "$WORK/audio_vectors.json"    Documentation/audio_vectors.json
  diff "$WORK/pm_param_vectors.json" Documentation/pm_param_vectors.json
  diff "$WORK/audio_vectors.gen.h"   codec/audio_vectors.gen.h
  echo "audio vectors: committed == regenerated"
  step "Build and run C audio certification (L2-only)"
  ccert test_audio_certify C_SDK/tests/test_audio_certify.c
  step "Run Rust audio certification"
  ( cd codec; cargo test --test certify_audio )
  step "Regenerate Faust C and diff (continue-on-error in CI)"
  if have faust; then
    faust -lang c -cn DcfPmCodec codec/faust/dcf_pm_codec.dsp -o "$WORK/dcf_pm_faust.c" \
      && diff "$WORK/dcf_pm_faust.c" codec/faust/dcf_pm_faust.c && echo "Faust C: committed == regenerated" \
      || echo "Faust PM regeneration differs (continue-on-error in CI)"
    faust -lang c -cn DcfRfModulator codec/faust/dcf_rf_modulator.dsp -o "$WORK/dcf_rf_modulator.gen.c" \
      && diff "$WORK/dcf_rf_modulator.gen.c" codec/faust/dcf_rf_modulator.gen.c && echo "Faust RF modulator C: committed == regenerated" \
      || echo "Faust RF regeneration differs (continue-on-error in CI)"
  else echo "faust not installed — skipping Faust regeneration check (as on the CI runner)"; fi
}

certify-game() {
  need python3; need gcc; need cargo
  step "Regenerate game vectors and diff against committed"
  python3 python/MCP/gen_game_vectors.py "$WORK/game_vectors.json"; vdiff game
  echo "game vectors: committed == regenerated"
  step "Build and run C game certification (L2-only)"
  ccert test_game_certify C_SDK/tests/test_game_certify.c
  step "Run Rust game certification"
  ( cd codec; cargo test --test certify_game )
}

certify-text() {
  need python3; need gcc; need cargo
  step "Regenerate text vectors and diff against committed"
  python3 python/MCP/gen_text_vectors.py "$WORK/text_vectors.json"; vdiff text
  echo "text vectors: committed == regenerated"
  step "Build and run C text certification (L2-only)"
  ccert test_text_certify C_SDK/tests/test_text_certify.c
  step "Run Rust text certification"
  ( cd codec; cargo test --test certify_text )
}

certify-qkd() {
  need python3; need gcc; need cargo
  step "Run the canonical Python reference selftest"
  python3 python/MCP/qkdlab_core.py
  step "Regenerate qkd vectors and diff against committed"
  python3 python/MCP/gen_qkd_vectors.py "$WORK/qkd_vectors.json"; vdiff qkd
  echo "qkd vectors: committed == regenerated"
  step "Build and run C qkd certification (L2-only)"
  ccert test_qkd_certify C_SDK/tests/test_qkd_certify.c
  step "Run Rust qkd certification"
  ( cd codec; cargo test --test certify_qkd; cargo test --lib qkd )
}

qkd-bridge() {
  need python3   # stdlib-only runtime; CI's `pip install numpy` is not needed here
  step "Beacon L2 laws + bridge end-to-end"
  ( cd python; python3 -m unittest tests.test_qkd_beacon tests.test_qkd_bridge -v )
  step "End-to-end demo (machine-readable)"
  python3 python/dcf/qkd/demo.py --json
}

certify-sstv() {
  need python3; need gcc; need cargo
  step "Regenerate SSTV vectors and diff against committed"
  python3 python/MCP/sstvlab_core.py
  python3 python/MCP/gen_sstv_vectors.py "$WORK/sstv_vectors.json"; vdiff sstv
  echo "sstv vectors: committed == regenerated"
  step "Build and run C SSTV certification (L2-only)"
  ccert test_sstv_certify C_SDK/tests/test_sstv_certify.c
  step "Run Rust SSTV certification"
  ( cd codec; cargo test --test certify_sstv )
}

certify-snake() {
  need python3; need gcc; need cc; need cargo
  step "Self-test the DCF-Snake (record) and DCF-Cue (cue) L2 references"
  python3 python/MCP/snakelab_core.py
  python3 python/MCP/monitorlab_core.py
  step "Regenerate Snake vectors (record plane + BEACON clock + unwrap_pid) and diff"
  python3 python/MCP/gen_snake_vectors.py "$WORK/snake_vectors.json"; vdiff snake
  echo "snake vectors: committed == regenerated"
  step "Regenerate Cue vectors (cue plane PCM) and diff"
  python3 python/MCP/gen_monitor_vectors.py "$WORK/monitor_vectors.json"; vdiff monitor
  echo "cue vectors: committed == regenerated"
  step "Build and run C Snake + Cue certification (L2-only)"
  ccert test_snake_certify   C_SDK/tests/test_snake_certify.c
  ccert test_monitor_certify C_SDK/tests/test_monitor_certify.c
  step "Build and run C raw-L2 SuperPack batching (+ compile the AF_PACKET socket impl)"
  ccert test_snake_l2 C_SDK/tests/test_snake_l2.c
  gcc -std=c11 -Wall -Wextra -I codec -c hydramodem/dcf-tools/snake_l2.c -o "$WORK/snake_l2.o"
  step "Run the Python raw-L2 transport tests (privilege-free batch double)"
  python3 -m unittest python/tests/test_snake_l2.py -v
  step "Run the quanta <-> DCF-Snake bridge tests (QSS splitter; no quanta binary needed)"
  python3 -m unittest python/tests/test_quanta_bridge.py -v
  step "Build and run the mixer/spoke DSP unit tests (servo/jitter/ASRC/PLC/cue-mix)"
  ccert test_snake_dsp C_SDK/tests/test_snake_dsp.c
  step "Build the DCF-Snake nodes + run the end-to-end in-process integration demo"
  ( cd hydramodem/dcf-tools
    cc -std=gnu11 -O2 -Wall -Wextra -I../../codec snake_loopback.c -lm -o "$WORK/snake_loopback"
    "$WORK/snake_loopback"
    # snake_source/snake_mixer #include "snake_ipc.h" (the separate DeMoD audio-stack repo,
    # not vendored) -- same guard as the workflow and dcf-tools/build.sh.
    if [ -f "${DEMOD_IPC_INCLUDE:-/nonexistent}/snake_ipc.h" ]; then
      cc -std=gnu11 -O2 -Wall -Wextra -I../../codec -I"$DEMOD_IPC_INCLUDE" snake_source.c snake_l2.c -lm -lrt -o "$WORK/snake_source"
      cc -std=gnu11 -O2 -Wall -Wextra -I../../codec -I"$DEMOD_IPC_INCLUDE" snake_mixer.c  snake_l2.c -lm -lrt -o "$WORK/snake_mixer"
      head -c 200000 /dev/urandom > "$WORK/pcm.f32"
      "$WORK/snake_source" --selftest --no-quanta --block 1024 --in "$WORK/pcm.f32"
      "$WORK/snake_mixer"  --selftest --no-quanta --out "$WORK/rec.f32"
    else
      echo "skip: snake_source/snake_mixer need snake_ipc.h (external DeMoD audio-stack; set DEMOD_IPC_INCLUDE)"
    fi )
  step "Run Rust Snake + Cue certification"
  ( cd codec; cargo test --test certify_snake; cargo test --test certify_monitor )
}

certify-lua() {
  # CI installs lua5.4; accept a host `lua` only if it is 5.4.
  local LUA=""
  if have lua5.4; then LUA=lua5.4
  elif have lua && lua -v 2>&1 | grep -q 'Lua 5\.4'; then LUA=lua
  elif [ "$HAVE_NIX" = 1 ]; then need lua lua5_4; LUA=lua
  else skip_job "no lua5.4 on host and no nix"; fi
  step "Certify the Lua DCF-Audio framework";         "$LUA" lua/selftest.lua
  step "Certify the Lua voice L3 + StreamDB history"; "$LUA" lua/selftest_voice.lua
  step "Certify the deployment profiles and link budget"
  "$LUA" lua/selftest_profile.lua
  "$LUA" lua/dcf_talk.lua --budget
  step "Certify the Lua agent harness";               "$LUA" lua/selftest_agent.lua
  step "Certify the Lua DCF-Snake adapter";           "$LUA" lua/selftest_snake.lua
  step "Certify the Lua transport (SuperPack + FEC batching)"
  "$LUA" -e 'local X=dofile("lua/dcf_transport.lua") local r=X.report(40,"wan") assert(r.datagrams==1 and r.kbps<90, "transport batching regressed") print(("transport OK: %d frames -> %d datagram, %.1f kbps"):format(r.frames,r.datagrams,r.kbps))'
  step "Certify the Lua SuperPack container";         "$LUA" lua/dcf_superpack.lua
  step "Certify the Lua FEC adapter";                 "$LUA" lua/dcf_fec.lua
  step "Smoke the rendezvous CLI";                    "$LUA" lua/dcf_jam.lua --passphrase ci-jam --codec pcm --blocks 20
  step "Smoke the full chat stack end to end"
  "$LUA" lua/dcf_talk.lua --blocks 40 --quiet
  "$LUA" lua/dcf_talk.lua --blocks 40 --loss 0.15 --quiet
  "$LUA" lua/dcf_talk.lua --blocks 40 --transport rf --corrupt 0.3 --quiet
  "$LUA" lua/dcf_talk.lua --blocks 40 --hub 8 --quiet
}

certify-go() {
  need go go
  step "Vet the Go SDK";                ( cd go; go vet ./... )
  step "Certify the Go SDK";            ( cd go; go test ./... -v )
  step "Race-check the UDP node";       ( cd go; go test -race ./node/ )
}

certify-haskell() {
  if have cabal && have ghc; then
    step "Certify the Haskell DeModFrame codec against the golden vectors (cabal test)"
    ( cd haskell; cabal test --test-show-details=direct )
  elif [ "$HAVE_NIX" = 1 ]; then
    # No host cabal/ghc: a pinned ghcWithPackages from nix, env-files disabled (same suite).
    note "ran Certify.hs with nix ghcWithPackages instead of 'cabal test'"
    step "Certify the Haskell DeModFrame codec against the golden vectors (nix ghc)"
    nix shell --impure --expr 'with import <nixpkgs> {}; [ (haskellPackages.ghcWithPackages (p: [p.aeson p.bytestring p.text p.scientific p.directory])) ]' \
      -c bash -c 'cd haskell && GHC_ENVIRONMENT=- ghc -isrc -outputdir "$1/hsout" -o "$1/hscert" test/Certify.hs && "$1/hscert"' _ "$WORK"
  else skip_job "no ghc/cabal on host and no nix"; fi
}

certify-java() {
  need javac jdk; need java jdk
  step "Certify the Java DeModFrame codec against the golden vectors"
  javac -d "$WORK/jout" java/com/demod/dcf/Frame.java java/com/demod/dcf/Certify.java \
    java/com/demod/dcf/SuperPack.java java/com/demod/dcf/SuperPackCertify.java \
    java/com/demod/dcf/FEC.java java/com/demod/dcf/FECCertify.java \
    java/com/demod/dcf/Medium.java java/com/demod/dcf/MediumCertify.java
  java -cp "$WORK/jout" com.demod.dcf.Certify Documentation/golden_vectors.json
  java -cp "$WORK/jout" com.demod.dcf.SuperPackCertify Documentation/superpack_vectors.json
  java -cp "$WORK/jout" com.demod.dcf.FECCertify Documentation/fec_vectors.json
  java -cp "$WORK/jout" com.demod.dcf.MediumCertify Documentation/medium_vectors.json
}

certify-minecraft() {
  need javac jdk; need java jdk
  step "Regenerate minecraft vectors and diff against committed"
  python3 python/MCP/gen_minecraft_vectors.py "$WORK/minecraft_vectors.json"
  diff "$WORK/minecraft_vectors.json" Documentation/minecraft_vectors.json
  diff "$WORK/minecraft_vectors.json" python/MCP/minecraft_vectors.json
  step "Python — vectors, datapack generator, sidecar (fake server), punctim mc, Bedrock bridge"
  (cd python && python3 -m unittest tests.test_minecraft_vectors tests.test_minecraft_datapack \
      tests.test_minecraft_sidecar tests.test_minecraft_cli tests.test_bedrock_ws -v)
  python3 python/punctim.py version --json | grep -q '"mc"'
  step "Build the core + Paper plugin jars (javac) and run the core loopback"
  # The plugin needs a JDK >= 25 (Paper 26.2's API is Java-25 bytecode): $PAPER_JDK, else a
  # Nix-store openjdk-25 (build.sh looks for one), else the core alone is built.
  if [ -n "${PAPER_JDK:-}" ] || ls -d /nix/store/*-openjdk-25* >/dev/null 2>&1; then
    bash minecraft/build.sh
    unzip -l minecraft/build/dcf-minecraft-paper.jar | grep -q plugin.yml
  else
    echo "no JDK 25 for the Paper plugin; core only"
    bash minecraft/build.sh --core-only
  fi
}

certify-kotlin() {
  need kotlinc kotlin jdk; need java jdk
  step "Certify the Kotlin DeModFrame codec against the golden vectors"
  kotlinc kotlin/src/main/kotlin/dcf/Frame.kt kotlin/src/main/kotlin/dcf/SuperPack.kt kotlin/src/main/kotlin/dcf/FEC.kt kotlin/src/main/kotlin/dcf/Certify.kt -include-runtime -d "$WORK/kcert.jar"
  java -cp "$WORK/kcert.jar" dcf.CertifyKt Documentation/golden_vectors.json
}

certify-node() {
  need node nodejs
  step "Certify the Node.js DeModFrame codec against the golden vectors"; node JS/nodejs/test/certify.js
  step "Certify the Node.js SuperPack container"; node JS/nodejs/test/certify_superpack.js
  step "Certify the Node.js FEC adapter";        node JS/nodejs/test/certify_fec.js
  step "Certify the Node.js DCF-Text adapter";   node JS/nodejs/test/certify_text.js
  step "Certify the Node.js DCF-SSTV adapter";   node JS/nodejs/test/certify_sstv.js
}

certify-perl() {
  need perl perl; need make
  step "Certify the Perl DeModFrame codec against the golden vectors (make test: t/*.t incl. t/medium.t)"
  ( cd perl; perl Makefile.PL; make test )
}

certify-cpp() {
  need g++
  step "Certify the C++ DeModFrame codec against the golden vectors"
  g++ -std=c++17 -Wall -Wextra -I cpp/include -o "$WORK/cpp_certify" cpp/tests/certify.cpp
  "$WORK/cpp_certify" Documentation/golden_vectors.json
  step "Certify the C++ SuperPack container"
  g++ -std=c++17 -Wall -Wextra -I cpp/include -o "$WORK/cpp_superpack" cpp/tests/certify_superpack.cpp
  "$WORK/cpp_superpack"
  step "Certify the C++ FEC adapter"
  g++ -std=c++17 -Wall -Wextra -I cpp/include -o "$WORK/cpp_fec" cpp/tests/certify_fec.cpp
  "$WORK/cpp_fec"
}

certify-swift() {
  need swift swift
  step "Certify the Swift DeModFrame codec against the golden vectors"
  if ! ( cd swift; swift test ) 2>&1 | tee "$WORK/swift.out"; then
    # The nix Swift-on-Linux wrapper omits `swift-test`; CI uses swift-actions.
    grep -q "swift-test: not found" "$WORK/swift.out" && skip_job "nix swift wrapper lacks swift-test; CI uses swift-actions"
    exit 1
  fi
}

certify-lisp() {
  need sbcl sbcl
  step "Certify the Lisp DeModFrame wire + FEC codecs against the golden vectors"
  sbcl --non-interactive --load lisp/src/wire.lisp --load lisp/src/fec.lisp
  # CI's second step (libstreamdb into /usr/local/lib via sudo + a Quicklisp bootstrap over
  # the network + the FiveAM suite) is continue-on-error there; not reproduced locally.
  note "FiveAM/Quicklisp step not run (continue-on-error in CI; needs sudo + network)"
}

certify-medium() {
  need python3; need gcc; need g++; need cargo; need go go; need node nodejs
  python3 -c 'import numpy' 2>/dev/null || note "numpy absent: test_medium's numpy-gated cases skip (CI installs numpy)"
  step "Regenerate medium vectors (laws) and diff every committed copy"
  python3 python/MCP/gen_medium_vectors.py "$WORK/medium_vectors.json"; vdiff medium
  echo "medium vectors: committed == regenerated"
  step "Python — punctim certify + medium unit tests"
  python3 python/punctim.py certify
  ( cd python; python3 -m unittest tests.test_medium -v )
  step "Build and run C medium certification (header-only codec/demod_medium.h)"
  ccert test_medium_certify C_SDK/tests/test_medium_certify.c
  step "Run Rust medium certification"
  ( cd codec; cargo test --test certify_medium )
  step "Run Go medium certification"
  ( cd go; go test ./medium/ -v )
  step "Run Node.js medium certification"
  node JS/nodejs/test/certify_medium.js
  step "Build and run C++ medium certification"
  g++ -std=c++17 -Wall -Wextra -I cpp/include -o "$WORK/cpp_medium" cpp/tests/certify_medium.cpp
  "$WORK/cpp_medium"
}

io-matrix() {
  need python3; need cmake; need cargo; need go go; need node nodejs
  [ -f tests/io_matrix.py ] || { echo "tests/io_matrix.py is missing (the workflow job would fail)"; exit 1; }
  # Same builds as the workflow; the C and Go CLIs go to $WORK (not the tree) and are
  # handed to the matrix through its PUNCTIM_C / PUNCTIM_GO overrides.
  step "Build the C punctim CLI (cmake)"
  cmake -S C_SDK -B "$WORK/iobuild" -DDCF_BUILD_NODE=ON -DDCF_BUILD_EXAMPLES=OFF
  cmake --build "$WORK/iobuild" -j"$NPROC" --target punctim
  step "Build the Rust punctim CLI (codec/target/debug/punctim)"
  ( cd codec; cargo build --bin punctim )
  step "Build the Go punctim CLI"
  ( cd go; go build -o "$WORK/punctim-go" ./cmd/punctim )
  step "Build the HydraModem tools (best-effort)"
  ( cd hydramodem/dcf-tools && ./build.sh ) || echo "hydramodem tools did not build; hydra media will be skipped"
  step "Run the I/O matrix"
  # CI writes tests/io_matrix_results.md and uploads it as an artifact; locally the table
  # goes to the job log instead, so a ci-local run never rewrites the committed report
  # (refresh that with `make io-matrix`).
  local rep=()
  python3 tests/io_matrix.py --help 2>/dev/null | grep -q -- '--report' && rep=(--report "$WORK/io_matrix_results.md")
  PUNCTIM_C="${PUNCTIM_C:-$WORK/iobuild/punctim}" PUNCTIM_GO="${PUNCTIM_GO:-$WORK/punctim-go}" \
    python3 tests/io_matrix.py "${rep[@]}"
}

# ══ driver ══════════════════════════════════════════════════════════════════════

certify-exsecutor() {
  # The in-tree Exsecutor codec vs the live certificate: needs nix (builds .#exsc and
  # .#fasmg-x86) — SKIP without it, FAIL if the toolchain builds but the codec drifts.
  have nix || skip_job "no nix to build the Exsecutor toolchain (.#exsc, .#fasmg-x86)"
  need fasmg fasmg
  step "Certify the in-tree Exsecutor DeModFrame codec against golden_vectors.json"
  PUNCTIM_REQUIRE_EXSECUTOR=1 ./exsecutor/certify.sh
}

record() { # record <PASS|SKIP|FAIL> <job> <detail>
  SUMMARY+=("$1|$2|$3")
  local c="$G"; [ "$1" = SKIP ] && c="$Y"; [ "$1" = FAIL ] && c="$Rd"
  if [ "$1" = SKIP ]; then printf "${c}SKIP${Z} %s (%s)\n" "$2" "$3"
  else printf "${c}%s${Z} %s%s\n" "$1" "$2" "${3:+ ($3)}"; fi
}

runjob() {
  local j="$1" rc t0 dt detail
  CUR_JOB="$j"; t0=$(date +%s)
  ( set -e; cd "$ROOT"; "$j" ) >"$WORK/$j.log" 2>&1
  rc=$?; dt=$(( $(date +%s) - t0 ))s
  if [ "$rc" = 200 ] && [ -f "$WORK/$j.skip" ]; then
    record SKIP "$j" "$(head -1 "$WORK/$j.skip")"
  elif [ "$rc" = 0 ]; then
    detail="$dt"
    [ -f "$WORK/$j.note" ] && detail="$dt; partial: $(paste -sd';' "$WORK/$j.note" | sed 's/;/; /g')"
    record PASS "$j" "$detail"
  else
    record FAIL "$j" "$dt; exit $rc"
    echo "      log: $WORK/$j.log"
    tail -15 "$WORK/$j.log" | sed 's/^/      | /'
  fi
}

# first line of a tool's version banner (drops the JVM's "Picked up JAVA_TOOL_OPTIONS" noise)
ver() { "$@" 2>&1 | grep -v '^Picked up' | head -1; }

main() {
  # Parity guard: the workflow's job list (order included) must equal JOBS.
  local WF_JOBS=() PARITY_FAIL=0 SELECTED=() j e st nm dt P=0 S=0 F=0
  mapfile -t WF_JOBS < <(awk '/^jobs:/{j=1; next} j && /^[^ #]/{j=0} j && match($0, /^  [A-Za-z0-9_-]+:[[:space:]]*$/){s=$0; sub(/^  /,"",s); sub(/:.*/,"",s); print s}' "$WORKFLOW")
  if [ "${WF_JOBS[*]}" != "${JOBS[*]}" ]; then
    echo "ci-local.sh is out of sync with $WORKFLOW:"
    echo "  workflow: ${WF_JOBS[*]}"
    echo "  script:   ${JOBS[*]}"
    PARITY_FAIL=1
  fi

  if [ "$#" -gt 0 ]; then SELECTED=("$@"); else SELECTED=("${JOBS[@]}"); fi
  for j in "${SELECTED[@]}"; do
    declare -F "$j" >/dev/null || { echo "unknown job: $j (see --list)"; return 2; }
  done

  echo "### Local wire-certify run — ${#SELECTED[@]} of ${#JOBS[@]} jobs (host toolchains$( [ "$HAVE_NIX" = 1 ] && echo " + nix" || echo "; no nix" )) ###"
  [ "$PARITY_FAIL" = 1 ] && record FAIL "ci-local-parity" "job list differs from $WORKFLOW"
  for j in "${SELECTED[@]}"; do runjob "$j"; done

  # ── summary ─────────────────────────────────────────────────────────────────
  echo; echo "================= SUMMARY ================="
  for e in "${SUMMARY[@]}"; do
    IFS='|' read -r st nm dt <<<"$e"
    case "$st" in PASS) P=$((P+1));; SKIP) S=$((S+1));; FAIL) F=$((F+1));; esac
    if [ "$st" = SKIP ]; then echo "SKIP $nm ($dt)"; else echo "$st $nm${dt:+ ($dt)}"; fi
  done
  echo "==========================================="
  echo "PASS: $P   SKIP: $S   FAIL: $F"
  [ "$F" -gt 0 ] && { KEEP_WORK=1; echo "logs kept in $WORK"; }

  if [ "${CI_LOCAL_REPORT:-0}" = 1 ]; then
    # Append (never overwrite): the file's header and earlier dated runs are the history.
    local REPORT=.github/LOCAL_CI_RESULTS.md dirty=""
    [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ] && dirty=" + uncommitted working-tree changes"
    {
      echo
      echo "---"
      echo
      echo "## Local run — $(date -u +%Y-%m-%d) · \`$(git rev-parse --short HEAD)\`$dirty"
      echo
      echo "- Generated by \`CI_LOCAL_REPORT=1 make ci-local\` (\`.github/ci-local.sh\`, job-for-job with \`wire-certify.yml\`; ${#SELECTED[@]} of ${#JOBS[@]} jobs run)."
      echo "- **Host:** $(uname -sm); gcc $(gcc -dumpfullversion 2>/dev/null || echo -); $(ver cargo --version); $(ver go version); node $(ver node --version); $(ver java -version); $(ver python3 --version); perl $(perl -e 'print $^V' 2>/dev/null || echo -); nix: $( [ "$HAVE_NIX" = 1 ] && echo yes || echo no)."
      echo "- **Result: $P PASS · $S SKIP · $F FAIL**"
      echo
      echo "| Job | Status | Detail |"
      echo "|-----|--------|--------|"
      for e in "${SUMMARY[@]}"; do
        IFS='|' read -r st nm dt <<<"$e"
        echo "| $nm | **$st** | ${dt//|/\\|} |"
      done
    } >>"$REPORT"
    echo "appended a dated section to $REPORT"
  fi

  [ "$F" -eq 0 ]
}

# Everything above is parsed before anything runs, so editing this file mid-run is safe.
main "$@"; exit $?
