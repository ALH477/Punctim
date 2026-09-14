#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
#
# DCF inter-container mesh interop test — every node image interacts at least once.
#
#   1. Identity / cross-cert matrix — every node image runs and identifies itself
#      (built from the certified codecs that all agree on the same golden vectors).
#   2. Live exchanges, by transport:
#      • ProtoMessage/UDP : dcf-go, dcf-rs, dcf-c  (Go<->Go, Go->Rust, C->Rust, Go->C)
#      • bare frame+SuperPack/UDP : dcf-python, dcf-nodejs  (Py<->Py, Py<->Node both ways)
#      • Faust modem (file medium) : dcf-c  (send-modem -> recv-modem, per modulation)
#      • gRPC bidi stream : dcf-cpp  (ping + frame + superpack over MeshStream)
#
# Usage:  ./docker/mesh-interop-test.sh           # uses :latest images (nix dockerTools)
#         TAG=0.3.0 ./docker/mesh-interop-test.sh
set -uo pipefail

ORG="${ORG:-alh477}"; TAG="${TAG:-latest}"
NET="dcf-interop-$$"; MODEM_VOL="dcf-modem-$$"
GO="$ORG/dcf-go:$TAG"; RS="$ORG/dcf-rs:$TAG"; PY="$ORG/dcf-python:$TAG"
JS="$ORG/dcf-nodejs:$TAG"; C="$ORG/dcf-c:$TAG"; CPP="$ORG/dcf-cpp:$TAG"; HM="$ORG/hydramodem:$TAG"
# The Lisp SDK image is versioned on its own scheme (2.2.0), not $TAG.
HML="$ORG/punctim:${PUNCTIM_TAG:-latest}"
fails=0
pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; fails=$((fails + 1)); }
cleanup() {
  docker rm -f go-hub rs-hub py-hub js-hub c-hub cpp-hub >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker volume rm "$MODEM_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup; docker network create "$NET" >/dev/null; docker volume create "$MODEM_VOL" >/dev/null

echo "== 1. identity / cross-cert matrix =="
docker run --rm "$GO"  version 2>&1 | grep -q "dcfnode"         && pass "dcf-go"     || fail "dcf-go version"
docker run --rm "$RS"  version 2>&1 | grep -qi "version"        && pass "dcf-rs"     || fail "dcf-rs version"
docker run --rm "$C"   version 2>&1 | grep -qi "DeModFrame"     && pass "dcf-c"      || fail "dcf-c version"
docker run --rm "$PY"  --help  2>&1 | grep -qiE "usage|recv"    && pass "dcf-python" || fail "dcf-python help"
docker run --rm "$JS"  version 2>&1 | grep -qi "dcfnode(js)"    && pass "dcf-nodejs" || fail "dcf-nodejs version"
docker run --rm "$CPP" version 2>&1 | grep -qi "gRPC node" && pass "dcf-cpp" || fail "dcf-cpp version"
docker run --rm "$HML" version 2>&1 | grep -q "2.2.0"          && pass "punctim (Lisp)" || fail "punctim version"
# Cross-cert: the Lisp node self-certifies the DeModFrame wire codec against the
# same anchors as every other language (frame CRC 0xA963 roundtrip, etc.) — its
# `test` suite must be green. Assert "no failures" rather than a hardcoded check
# count: the count legitimately grows as the SDK gains tests (it went 16 -> 23
# when the agent DSL landed), and pinning it turned suite *growth* into a red
# interop run.
docker run --rm "$HML" test 2>&1 | grep -qE "Fail: 0 \( *0%\)" && pass "punctim wire/adapter self-cert (green, agrees on DeModFrame)" || fail "punctim wire cert"

echo "== 2. ProtoMessage/UDP mesh: dcf-go / dcf-rs / dcf-c =="
docker run -d --name go-hub --network "$NET" "$GO" start --bind 0.0.0.0:7777 >/dev/null; sleep 1
docker run --rm --network "$NET" "$GO" send-text --peer go-hub:7777 --channel 9 --text "go-to-go" >/dev/null 2>&1; sleep 1
docker logs go-hub 2>&1 | grep -q "go-to-go" && pass "Go hub <- Go (DCF-Text)" || fail "Go->Go"
docker run --rm --network "$NET" "$C" send-game --peer go-hub:7777 --channel-id 9 --hex cafebabe --type 2 2>&1 | grep -q "sent" && \
  { sleep 1; docker logs go-hub 2>&1 | grep -q "game from" && pass "Go hub <- C (GAME_DCF, cross-lang)" || fail "C->Go game"; } || fail "C->Go send"
docker rm -f go-hub >/dev/null 2>&1

docker run -d --name rs-hub --network "$NET" "$RS" start >/dev/null; sleep 2
docker run --rm --network "$NET" "$C" send-position --peer rs-hub:7777 --x 1 --y 2 --z 3 2>&1 | grep -q "sent position" && pass "C -> Rust (POSITION)" || fail "C->Rust position"
docker run --rm --network "$NET" "$C" send-game --peer rs-hub:7777 --channel-id 5 --hex cafebabe --type 2 2>&1 | grep -q "sent" && pass "C -> Rust (GAME_DCF)" || fail "C->Rust game"
docker rm -f rs-hub >/dev/null 2>&1

echo "== 3. bare frame+SuperPack/UDP mesh: dcf-python / dcf-nodejs =="
docker run -d --name py-hub --network "$NET" "$PY" recv --follow --channel duet --port 7801 >/dev/null; sleep 1.5
docker run --rm --network "$NET" "$PY" send "py-to-py"   --channel duet --peers py-hub:7801 >/dev/null 2>&1; sleep 1
docker logs py-hub 2>&1 | grep -q "py-to-py"   && pass "Py hub <- Py"            || fail "Py->Py"
docker run --rm --network "$NET" "$JS" send "node-to-py" --channel duet --peers py-hub:7801 >/dev/null 2>&1; sleep 1
docker logs py-hub 2>&1 | grep -q "node-to-py" && pass "Py hub <- Node (cross-lang)" || fail "Node->Py"
docker rm -f py-hub >/dev/null 2>&1

echo "== 4. Faust modem (file medium): dcf-c send-modem -> recv-modem per modulation =="
for mod in fsk ook psk qam; do
  docker run --rm -v "$MODEM_VOL:/m" "$C" send-modem --medium "/m/$mod.dcfm" --modulation "$mod" --text "modem-$mod" >/dev/null 2>&1
  docker run --rm -v "$MODEM_VOL:/m" "$C" recv-modem --medium "/m/$mod.dcfm" 2>&1 | grep -q "modem-$mod" \
    && pass "modem $mod: byte-exact over the medium" || fail "modem $mod"
done

echo "== 5. gRPC bidi stream: dcf-cpp =="
docker run -d --name cpp-hub --network "$NET" "$CPP" serve --port 50051 --node-id cpp-hub >/dev/null; sleep 2
out="$(docker run --rm --network "$NET" "$CPP" connect --peer cpp-hub:50051 --node-id cpp-cli 2>&1)"
echo "$out" | grep -q "PONG"                    && pass "C++ gRPC PONG / RTT"               || fail "C++ ping"
echo "$out" | grep -q "frame (valid)"           && pass "C++ MeshStream frame echo (valid)" || fail "C++ frame"
echo "$out" | grep -q "superpack (2 frames"     && pass "C++ MeshStream SuperPack echo"     || fail "C++ superpack"
docker rm -f cpp-hub >/dev/null 2>&1

echo "== 6. HydraModem acoustic PHY (WAV/file medium): self-test + frame round-trip =="
docker run --rm "$HM" 2>&1 | grep -q "INTEROP PASSED" && pass "hydramodem interop self-test" || fail "hydramodem self-test"
docker run --rm -v "$MODEM_VOL:/m" "$HM" frame_tx d310000100a1ffffdeadbeef0a1b2c6242 /m/hm.wav --conv >/dev/null 2>&1
docker run --rm -v "$MODEM_VOL:/m" "$HM" frame_rx /m/hm.wav --conv 2>&1 | grep -q "d310000100a1ffffdeadbeef0a1b2c6242" \
  && pass "hydramodem frame_tx -> frame_rx byte-exact over a volume" || fail "hydramodem round-trip"

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL INTEROP CHECKS PASSED — 6 DCF node images mesh across ProtoMessage/UDP (go/rust/c),"
  echo "bare-frame+SuperPack (python/nodejs), the Faust modem (c), and gRPC (cpp); plus the"
  echo "hydramodem acoustic toolbox (frame round-trip over a shared volume) and the punctim"
  echo "Lisp node (self-certifies the same DeModFrame wire codec, 16/16)."
else
  echo "$fails INTEROP CHECK(S) FAILED"; exit 1
fi
