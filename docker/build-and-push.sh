#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
#
# Build and push the DCF node Docker images to Docker Hub (alh477/*), tagged
# :latest and :<VERSION>. Built HERMETICALLY with Nix (dockerTools) — no apt/apk,
# no network to OS package mirrors needed at build time.
#
#   docker login
#   ./docker/build-and-push.sh            # build + load + push dcf-go and dcf-rs
#   PUSH=0 ./docker/build-and-push.sh     # build + load only
#   ./docker/build-and-push.sh dcf-go     # one image
#
# The images run like real nodes: their entrypoint is the node binary with
# `start` as the default command (override with any subcommand, e.g.
#   docker run --rm alh477/dcf-go send-text --peer host:7777 --text hi).
#
# Node-capable images and their on-wire dialect:
#   dcf-go, dcf-rs        — ProtoMessage transport (mesh with each other)
#   dcf-python, dcf-nodejs — bare DeModFrame + SuperPack (mesh with each other)
# Node-capable images and their on-wire transport:
#   dcf-go, dcf-rs, dcf-c   — ProtoMessage/UDP (mesh with each other)
#   dcf-python, dcf-nodejs  — bare DeModFrame + SuperPack/UDP (mesh with each other)
#   dcf-c                   — also a Faust-DSP modem (send-modem/recv-modem over a medium)
#   dcf-cpp                 — a gRPC node (bidi MeshStream)
#   dcf-gns                 — Steam-compatible dedicated server (GameNetworkingSockets
#                             hub: serve-gns; clients connect-gns). See DCF_STEAM_SPEC.md.
#   hydramodem              — acoustic-modem toolbox (frame_tx/frame_rx, tx/rx_campaign,
#                             dcf_loopback, sense_node). A WAV/file PHY, not a UDP node;
#                             default cmd runs the interop self-test. See DCF_SENSE_SPEC.md.
#   punctim               — Common Lisp SDK CLI (the `punctim` node + StreamDB).
#                             Versioned 2.2.0 (its own scheme), not the default VERSION.
#
# Builds run at low priority with capped cores (nice + --cores) so they don't
# saturate the machine. Set NICE=0 to disable.
set -euo pipefail

cd "$(dirname "$0")/.."
VERSION="${VERSION:-0.3.0}"
PUSH="${PUSH:-1}"
ORG="${ORG:-alh477}"
NICE="${NICE:-19}"
CORES="${CORES:-2}"
NIXNICE=(); [ "$NICE" != "0" ] && NIXNICE=(nice -n "$NICE")

build_one() {
  local img="$1" flake="$2" ver="${3:-$VERSION}"
  echo "== nix build .#${flake} (nice ${NICE}, ${CORES} cores) =="
  "${NIXNICE[@]}" nix build ".#${flake}" --cores "$CORES" --max-jobs 1
  docker load < result
  docker tag "${ORG}/${img}:latest" "${ORG}/${img}:${ver}"
  if [ "$PUSH" = "1" ]; then
    docker push "${ORG}/${img}:latest"
    docker push "${ORG}/${img}:${ver}"
  fi
  rm -f result
}

target="${1:-all}"
case "$target" in
  dcf-go)     build_one dcf-go     docker-dcf-go ;;
  dcf-rs)     build_one dcf-rs     docker-dcf-rust ;;
  dcf-python) build_one dcf-python docker-dcf-python ;;
  dcf-nodejs) build_one dcf-nodejs docker-dcf-nodejs ;;
  dcf-c)      build_one dcf-c      docker-dcf-c ;;
  dcf-cpp)    build_one dcf-cpp    docker-dcf-cpp ;;
  dcf-gns)    build_one dcf-gns    docker-dcf-gns ;;
  hydramodem) build_one hydramodem docker-hydramodem ;;
  punctim)  build_one punctim  docker-punctim 2.2.0 ;;  # Lisp SDK; own version
  all)
    build_one dcf-go     docker-dcf-go
    build_one dcf-rs     docker-dcf-rust
    build_one dcf-python docker-dcf-python
    build_one dcf-nodejs docker-dcf-nodejs
    build_one dcf-c      docker-dcf-c
    build_one dcf-cpp    docker-dcf-cpp
    build_one dcf-gns    docker-dcf-gns
    build_one hydramodem docker-hydramodem
    build_one punctim  docker-punctim 2.2.0
    ;;
  *) echo "unknown target: $target (dcf-go|dcf-rs|dcf-python|dcf-nodejs|dcf-c|dcf-cpp|dcf-gns|hydramodem|punctim|all)"; exit 2 ;;
esac
echo "DONE"
