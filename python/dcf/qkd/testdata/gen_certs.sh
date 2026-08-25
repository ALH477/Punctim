#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
# Test PKI for the mutual-TLS path. Real KMEs authenticate each SAE by its
# client certificate — this mirrors that so the lab exercises the same code
# path you will use against vendor hardware or QuKayDee.
#
#   ./gen_certs.sh [outdir]     (default: certs)
set -euo pipefail
OUT="${1:-certs}"
mkdir -p "$OUT"
cd "$OUT"

echo "==> CA"
# basicConstraints/keyUsage are explicit: OpenSSL 3.x rejects a signing CA that
# omits keyCertSign ("CA cert does not include key usage extension").
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout ca.key -out ca.crt -subj "/CN=DCF-QKD Lab CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign,digitalSignature" 2>/dev/null

echo "==> KME server cert (SAN: localhost, 127.0.0.1)"
openssl req -newkey rsa:2048 -nodes -keyout kme.key -out kme.csr \
  -subj "/CN=localhost" 2>/dev/null
printf "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n" > kme.ext
openssl x509 -req -in kme.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out kme.crt -days 825 -extfile kme.ext 2>/dev/null

echo "==> SAE client cert (CN carries the SAE_ID)"
openssl req -newkey rsa:2048 -nodes -keyout sae.key -out sae.csr \
  -subj "/CN=SAE-MASTER-01" 2>/dev/null
printf "extendedKeyUsage=clientAuth\n" > sae.ext
openssl x509 -req -in sae.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out sae.crt -days 825 -extfile sae.ext 2>/dev/null

rm -f ./*.csr ./*.ext
echo "==> done: $(pwd)"
ls -1
