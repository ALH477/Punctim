#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
#
# Exercise the mutual-TLS path against a throwaway mock KME pair.  Real KMEs require
# mTLS; this proves the same client code works unmodified.
#
# Manual / bench script — deliberately NOT run in CI (it needs openssl and binds
# ports), mirroring spa/scripts/nft_integration_test.sh.
#
#   ./tls_check.sh [certdir]     (default: certs)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYROOT="$(cd "$HERE/../../.." && pwd)"          # .../python
CERTDIR="${1:-certs}"

test -f "$CERTDIR/ca.crt" || "$HERE/gen_certs.sh" "$CERTDIR" >/dev/null

PYTHONPATH="$PYROOT" python3 -m dcf.qkd.mock_kme --tls --certdir "$CERTDIR" --quiet \
  >/tmp/kme_tls.log 2>&1 &
KME_PID=$!
trap 'kill "$KME_PID" 2>/dev/null || true' EXIT
sleep 1.5

PYTHONPATH="$PYROOT" python3 - "$CERTDIR" <<'PY'
import sys
from dcf.qkd.etsi014 import Etsi014Client

certdir = sys.argv[1]
client = Etsi014Client(
    "https://localhost:8010",
    "SAE-MASTER-01",
    certfile=f"{certdir}/sae.crt",
    keyfile=f"{certdir}/sae.key",
    cafile=f"{certdir}/ca.crt",
    extra_headers={"X-SAE-ID": "SAE-MASTER-01"},
)
status = client.status("SAE-SLAVE-01")
key = client.get_key("SAE-SLAVE-01", number=1, size=256)[0]
print(f"  [PASS] mTLS status   -> {status['source_KME_ID']}")
print(f"  [PASS] mTLS enc_keys -> {key.key_ID} ({key.bits} bits)")
PY
