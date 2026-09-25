#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
#
# Certify the in-tree Exsecutor DeModFrame codec against THIS repo's live
# Documentation/golden_vectors.json — the same 246-vector certificate every
# other binding is held to (109 encode + 137 syndrome), plus the anchors and
# the affine laws.
#
# The codec, its driver and the declaration live beside this script and are
# LGPL-3.0-only by an explicit grant from the copyright holder; see their
# headers, LICENSING.md and Documentation/DCF_EXSECUTOR.md. The COMPILER
# (exsc) stays GPL-3.0-or-later and is never linked — it is run as a
# subprocess, exactly like janus-c and quanta, and Exsecutor's Exception A
# puts no obligation on its output.
#
#   exsecutor/certify.sh
#
# Toolchain, in order of preference:
#   $EXSC / $FASMG        explicit paths
#   exsc / fasmg on PATH
#   nix build .#exsc      the pinned upstream compiler (needs nix)
# fasmg has no fallback: without it the script skips with status 0 in a
# plain checkout and fails loudly under CI ($PUNCTIM_REQUIRE_EXSECUTOR=1).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

req="${PUNCTIM_REQUIRE_EXSECUTOR:-0}"
skip() {  # a missing optional toolchain is a skip locally, a failure in CI
  if [ "$req" = "1" ]; then echo "FAIL: $1" >&2; exit 1; fi
  echo "SKIP: $1" >&2; exit 0
}

usable() { [ -n "$1" ] && command -v "$1" >/dev/null 2>&1; }

EXSC="${EXSC:-$(command -v exsc || true)}"
usable "$EXSC" || EXSC=""    # an explicit but bogus $EXSC must not look found
if [ -z "$EXSC" ]; then
  if command -v nix >/dev/null 2>&1; then
    echo "building the pinned Exsecutor compiler (nix build .#exsc) ..." >&2
    if nix build "$ROOT#exsc" --no-link --print-out-paths >"$WORK/p" 2>"$WORK/nixlog"; then
      EXSC="$(cat "$WORK/p")/bin/exsc"
    else
      sed 's/^/  /' "$WORK/nixlog" >&2 || true
      skip "could not build .#exsc"
    fi
  else
    skip "no exsc on PATH and no nix to build one (set \$EXSC)"
  fi
fi
FASMG="${FASMG:-$(command -v fasmg || true)}"
usable "$FASMG" || skip "no usable fasmg (set \$FASMG); it assembles exsc's output"

# fasmg locates the x86 macro package through $INCLUDE. Supply it rather than
# inherit it: without this the assemble step dies on "source file
# 'format/format.inc' not found", and depending on the caller's environment for
# it is exactly the ambient state Exsecutor exists to avoid.
if [ -z "${INCLUDE:-}" ] || [ ! -d "${INCLUDE:-/nonexistent}" ]; then
  if command -v nix >/dev/null 2>&1 \
     && nix build "$ROOT#fasmg-x86" --no-link --print-out-paths >"$WORK/inc" 2>"$WORK/inclog"; then
    INCLUDE="$(cat "$WORK/inc")"
    export INCLUDE
  else
    skip "no \$INCLUDE for fasmg's macro package and could not build .#fasmg-x86"
  fi
fi

VEC="${PUNCTIM_GOLDEN_VECTORS:-$ROOT/Documentation/golden_vectors.json}"
[ -f "$VEC" ] || { echo "FAIL: no certificate at $VEC" >&2; exit 1; }
export PUNCTIM_GOLDEN_VECTORS="$VEC"

echo "exsc:    $EXSC"
echo "fasmg:   $FASMG"
echo "vectors: $VEC"

# The unit is order-sensitive: the declaration, then the codec, then the driver.
"$EXSC" aedifica --hospes x86_64-linux \
    "$HERE/demodframe.exsc" "$HERE/codex.exsc" "$HERE/probatio.exsc" \
    -o "$WORK/e.asm" >"$WORK/exsclog" 2>&1 \
  || { echo "FAIL: exsc refused the unit:" >&2; sed 's/^/  /' "$WORK/exsclog" >&2; exit 1; }

"$FASMG" "$WORK/e.asm" "$WORK/e.bin" >"$WORK/asmlog" 2>&1 \
  || { echo "FAIL: fasmg could not assemble exsc's output:" >&2; sed 's/^/  /' "$WORK/asmlog" >&2; exit 1; }
chmod +x "$WORK/e.bin"

"$WORK/e.bin" >"$WORK/out" 2>"$WORK/err" \
  || { echo "FAIL: the certifier exited non-zero:" >&2; sed 's/^/  /' "$WORK/err" >&2; exit 1; }
[ -s "$WORK/err" ] && { echo "FAIL: the certifier wrote to stderr:" >&2; sed 's/^/  /' "$WORK/err" >&2; exit 1; }

# Determinism (spec §9.3): the same unit must emit the same bytes twice.
"$WORK/e.bin" >"$WORK/out2" 2>/dev/null
cmp -s "$WORK/out" "$WORK/out2" \
  || { echo "FAIL: two runs wrote different streams (determinism)" >&2; exit 1; }

python3 "$HERE/expecta.py" --compara "$WORK/out"
echo "Exsecutor DeModFrame codec: certified against $(basename "$VEC")"
