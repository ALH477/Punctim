#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
#
# Build and exercise the Exsecutor presence agent (exsecutor/agent/).
#
#   exsecutor/agent/selftest.sh
#
# What it proves, in order:
#   1. the agent unit COMPILES (demodframe + codex + presence + driver);
#   2. a PING piped on stdin produces the exact expected PONG frames on
#      stdout (byte-for-byte, compared against a Python-computed oracle);
#   3. two runs over the same input write byte-identical output (determinism,
#      spec §9.3);
#   4. empty stdin exits 0 (clean EOF);
#   5. a corrupt frame exits non-zero (the driver fails loudly).
#
# Toolchain resolution mirrors exsecutor/certify.sh: $EXSC / $FASMG, then PATH,
# then `nix build .#exsc` / `.#fasmg-x86`.  Missing any of it SKIPS (exit 0) in
# a plain checkout and FAILS loudly under PUNCTIM_REQUIRE_EXSECUTOR=1 (CI).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

req="${PUNCTIM_REQUIRE_EXSECUTOR:-0}"
skip() {
  if [ "$req" = "1" ]; then echo "FAIL: $1" >&2; exit 1; fi
  echo "SKIP: $1" >&2; exit 0
}
usable() { [ -n "$1" ] && command -v "$1" >/dev/null 2>&1; }

# ── toolchain (same resolution as certify.sh) ────────────────────────────────
EXSC="${EXSC:-$(command -v exsc || true)}"
usable "$EXSC" || EXSC=""
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

if [ -z "${INCLUDE:-}" ] || [ ! -d "${INCLUDE:-/nonexistent}" ]; then
  if command -v nix >/dev/null 2>&1 \
     && nix build "$ROOT#fasmg-x86" --no-link --print-out-paths >"$WORK/inc" 2>"$WORK/inclog"; then
    INCLUDE="$(cat "$WORK/inc")"
    export INCLUDE
  else
    skip "no \$INCLUDE for fasmg's macro package and could not build .#fasmg-x86"
  fi
fi

echo "exsc:    $EXSC"
echo "fasmg:   $FASMG"

# ── 1. compile ───────────────────────────────────────────────────────────────
# The unit is order-sensitive: declaration, then codec, then presence, then
# driver.
"$EXSC" aedifica --hospes x86_64-linux \
    "$HERE/demodframe.exsc" "$HERE/codex.exsc" "$HERE/presence.exsc" "$HERE/driver.exsc" \
    -o "$WORK/agent.asm" >"$WORK/exsclog" 2>&1 \
  || { echo "FAIL: exsc refused the agent unit:" >&2; sed 's/^/  /' "$WORK/exsclog" >&2; exit 1; }

"$FASMG" "$WORK/agent.asm" "$WORK/agent.bin" >"$WORK/asmlog" 2>&1 \
  || { echo "FAIL: fasmg could not assemble the agent:" >&2; sed 's/^/  /' "$WORK/asmlog" >&2; exit 1; }
chmod +x "$WORK/agent.bin"
echo "compiled: agent.bin"

# ── build the canned PING and the expected PONG with the Python oracle ───────
python3 - "$WORK" "$ROOT" <<'PYEOF'
import os, sys
work, root = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(root, "python", "MCP"))
sys.path.insert(0, os.path.join(root, "matrix-bridge"))
from wirelab_core import encode
import dcf_presence as pres


pid = 5
# PING descriptor: frag_idx=0, payload=[len_hi, len_lo, PING_flags, 0]
line = pres.pack_presence("pinger", 0x0001, "pinger")
plen = len(line.encode())
desc = encode(0, (pid << 10) | 0, 0x00A1, 0xFFFF,
              bytes([(plen >> 8) & 0xFF, plen & 0xFF, pres.FLAGS_PING, 0]), 0)
# PING data: frag_idx=1, first 4 bytes of the line
data = encode(0, (pid << 10) | 1, 0x00A1, 0xFFFF, line.encode()[:4].ljust(4, b"\x00"), 0)
with open(os.path.join(work, "ping.bin"), "wb") as f:
    f.write(desc + data)

# expected PONG: the agent answers with its identity (exsecutor / 0x00c3 / exs-agent)
pong_line = pres.pack_presence("exsecutor", 0x00C3, "exs-agent")
pong_plen = len(pong_line.encode())
pong_desc = encode(0, (pid << 10) | 0, 0x00C3, 0x00A1,
                   bytes([(pong_plen >> 8) & 0xFF, pong_plen & 0xFF, pres.FLAGS_PONG, 0]), 0)
pong_data = encode(0, (pid << 10) | 1, 0x00C3, 0x00A1,
                   pong_line.encode()[:4].ljust(4, b"\x00"), 0)
with open(os.path.join(work, "expected_pong.bin"), "wb") as f:
    f.write(pong_desc + pong_data)
print(f"built ping.bin ({plen}B line) and expected_pong.bin ({pong_plen}B line)")
PYEOF

# ── 2. PING -> PONG over the pipe ────────────────────────────────────────────
"$WORK/agent.bin" < "$WORK/ping.bin" > "$WORK/pong.bin" 2>"$WORK/err" \
  || { echo "FAIL: the agent exited non-zero on a valid PING:" >&2; sed 's/^/  /' "$WORK/err" >&2; exit 1; }
[ -s "$WORK/err" ] && { echo "FAIL: the agent wrote to stderr:" >&2; sed 's/^/  /' "$WORK/err" >&2; exit 1; }

cmp -s "$WORK/pong.bin" "$WORK/expected_pong.bin" \
  || {
       echo "FAIL: the PONG bytes differ from the oracle:" >&2
       echo "  got:      $(xxd -p "$WORK/pong.bin" | tr -d '\n')" >&2
       echo "  expected: $(xxd -p "$WORK/expected_pong.bin" | tr -d '\n')" >&2
       exit 1
     }
echo "PING -> PONG: byte-identical to the oracle"

# ── 3. determinism (spec §9.3): same input -> same bytes ─────────────────────
"$WORK/agent.bin" < "$WORK/ping.bin" > "$WORK/pong2.bin" 2>/dev/null
cmp -s "$WORK/pong.bin" "$WORK/pong2.bin" \
  || { echo "FAIL: two runs wrote different bytes (determinism)" >&2; exit 1; }
echo "determinism: two runs byte-identical"

# ── 4. empty stdin exits 0 ───────────────────────────────────────────────────
"$WORK/agent.bin" < /dev/null > /dev/null 2>&1 \
  || { echo "FAIL: empty stdin did not exit 0" >&2; exit 1; }
echo "clean EOF: exit 0"

# ── 5. a corrupt frame exits non-zero ────────────────────────────────────────
set +e
printf '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' \
  | "$WORK/agent.bin" > /dev/null 2>&1
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
  echo "FAIL: a corrupt frame did not fail (got exit 0)" >&2; exit 1
fi
echo "corrupt frame: exit $rc (loud failure, as designed)"

echo "Exsecutor presence agent: CERTIFIED (compiles, answers PING, deterministic)"
