# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""
ETSI GS QKD 014 v1.1.1 key delivery client — stdlib only.

Three methods, per the spec:
    GET  /api/v1/keys/{slave_SAE_ID}/status
    POST /api/v1/keys/{slave_SAE_ID}/enc_keys      (master SAE requests fresh keys)
    POST /api/v1/keys/{master_SAE_ID}/dec_keys     (slave SAE resolves by key_ID)

Roles are per-key, not per-node: the SAE that calls enc_keys is the master for
those keys; the SAE that later calls dec_keys with those IDs is the slave. A
bidirectional peer performs BOTH roles depending on traffic direction.

Real KMEs require HTTPS with mutual TLS. Pass cert/key/ca to enable it; this
same client works unmodified against QuKayDee or vendor hardware.

EXPORT NOTE: this module implements no cryptographic algorithm.  It is an HTTP
client; the TLS is stdlib `ssl` against an operator-supplied endpoint.  It does
hold delivered key material in memory — see the export section of
Documentation/DCF_QKD_SPEC.md.  Key bytes MUST NOT be handed to the DCF wire.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass


class KmeError(RuntimeError):
    """KME returned a non-2xx response or an unparseable body."""


@dataclass
class Key:
    key_ID: str
    key: bytes  # decoded from base64

    @property
    def bits(self) -> int:
        return len(self.key) * 8


class Etsi014Client:
    def __init__(
        self,
        base_url: str,
        sae_id: str,
        certfile: str | None = None,
        keyfile: str | None = None,
        cafile: str | None = None,
        insecure: bool = False,
        timeout: float = 10.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sae_id = sae_id
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self._ctx: ssl.SSLContext | None = None
        if self.base_url.startswith("https"):
            ctx = ssl.create_default_context(cafile=cafile)
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            if certfile:
                ctx.load_cert_chain(certfile, keyfile)
            self._ctx = ctx

    # --- transport ---------------------------------------------------------
    def _call(self, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.extra_headers)
        req = urllib.request.Request(
            url,
            data=data,
            method="POST" if data is not None else "GET",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise KmeError(f"{exc.code} from {url}: {detail}") from exc
        except (urllib.error.URLError, json.JSONDecodeError, ssl.SSLError) as exc:
            raise KmeError(f"{url}: {exc}") from exc

    # --- ETSI 014 methods --------------------------------------------------
    def status(self, peer_sae_id: str) -> dict:
        return self._call(f"/api/v1/keys/{peer_sae_id}/status")

    def get_key(self, slave_sae_id: str, number: int = 1, size: int = 256) -> list[Key]:
        """Master SAE: request `number` fresh keys of `size` bits for a slave SAE."""
        body = self._call(
            f"/api/v1/keys/{slave_sae_id}/enc_keys",
            {"number": number, "size": size},
        )
        return self._parse_container(body)

    def get_key_with_ids(self, master_sae_id: str, key_ids: list[str]) -> list[Key]:
        """Slave SAE: resolve key_IDs previously delivered out-of-band by the master."""
        body = self._call(
            f"/api/v1/keys/{master_sae_id}/dec_keys",
            {"key_IDs": [{"key_ID": k} for k in key_ids]},
        )
        return self._parse_container(body)

    @staticmethod
    def _parse_container(body: dict) -> list[Key]:
        try:
            return [
                Key(key_ID=entry["key_ID"], key=base64.b64decode(entry["key"]))
                for entry in body["keys"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise KmeError(f"malformed key container: {body!r}") from exc
