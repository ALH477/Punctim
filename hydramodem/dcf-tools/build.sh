#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
# dcf-tools/build.sh -- build the vendored hydramodem static lib + the repo DCF
# interop / PER tools. Repo glue (DeMoD LLC, LGPL-3.0).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)

# Build the vendored static library via its own (pristine) Makefile.
make -C "$root" build/libhydramodem.a >/dev/null

CC=${CC:-cc}
CFLAGS=${CFLAGS:--std=gnu11 -O2 -Wall -Wextra}
out="$here/build"
mkdir -p "$out"
# hydra_symbols_certify: certifies the DCF-Medium `hydra_symbols` vectors
# (codec/medium_vectors.gen.h) against the real hydra_frame_build + TX->RX loopback.
for t in dcf_loopback tx_campaign rx_campaign frame_tx frame_rx sense_node sstv_send sstv_recv \
         hydra_symbols_certify; do
    # shellcheck disable=SC2086
    $CC $CFLAGS "$here/$t.c" "$root/build/libhydramodem.a" -lm -o "$out/$t"
done
echo "built: $out/{dcf_loopback,tx_campaign,rx_campaign,frame_tx,frame_rx,sense_node,sstv_send,sstv_recv,hydra_symbols_certify}"

# DCF-Snake nodes (cat5e audio snake): the record/cue planes ride the certified codec + the
# raw-L2 SuperPack transport (snake_l2.c), independent of the hydramodem acoustic lib. The
# in-process integration demo (snake_loopback) needs no socket. See DCF_SNAKE_SPEC.md.
# shellcheck disable=SC2086
$CC $CFLAGS "$here/snake_loopback.c" -lm -o "$out/snake_loopback"

# snake_ipc.h lives in the DeMoD audio-stack (shared memory IPC contract) — a
# separate repo, so it is NOT available in a hermetic Nix build or on a CI
# runner. Build the two IPC-dependent nodes only when that header is actually
# present; skip with a notice otherwise. Without this guard `set -e` aborted the
# whole script, so `nix build .#hydramodem-tools` and the certify-hydramodem CI
# job failed anywhere but a dev box that happened to have the audio-stack
# checked out next door.
DEMOD_IPC_INCLUDE="${DEMOD_IPC_INCLUDE:-/home/asher/Documents/DeMoD/audio-stack/ipc/include}"
if [ -f "$DEMOD_IPC_INCLUDE/snake_ipc.h" ]; then
    for t in snake_source snake_mixer; do
        # shellcheck disable=SC2086
        $CC $CFLAGS -I"$DEMOD_IPC_INCLUDE" "$here/$t.c" "$here/snake_l2.c" -lm -lrt -o "$out/$t"
    done
    echo "built: $out/{snake_loopback,snake_source,snake_mixer}"
else
    echo "built: $out/{snake_loopback}"
    echo "note: snake_source/snake_mixer skipped — snake_ipc.h not found under" \
         "\$DEMOD_IPC_INCLUDE ($DEMOD_IPC_INCLUDE); set it to the DeMoD audio-stack" \
         "ipc/include to build the cue-plane nodes."
fi
