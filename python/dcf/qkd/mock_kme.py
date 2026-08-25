# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""
Mock ETSI GS QKD 014 KME pair.

Stands in for two Key Management Entities sitting at opposite ends of a QKD
link. The link itself is simulated by the simple fact that both KMEs read from
ONE keystore: that is precisely the property a real QKD link provides — both
ends already hold identical key material, and the SAE-facing API is the only
thing an integrator actually touches.

What is faithful to the spec:
  * the three methods and their URL shapes
  * the key container format ({"keys": [{"key_ID", "key"}]}), key base64-encoded
  * per-key master/slave role binding
  * single-delivery semantics (a key is consumed once the slave fetches it)
  * status field set

What is NOT (and must never be mistaken for) real QKD:
  * keys come from os.urandom, not from photons. There is no quantum anything
    here. This is an INTEGRATION TEST FIXTURE for the classical plane.

Run:
    python3 -m dcf.qkd.mock_kme                    # http on 8010/8020
    python3 -m dcf.qkd.mock_kme --tls --certdir certs
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_KEY_PER_REQUEST = 128
MIN_KEY_SIZE = 64
MAX_KEY_SIZE = 1024
DEFAULT_KEY_SIZE = 256


@dataclass
class KeyRecord:
    key_ID: str
    key: bytes
    master_sae: str
    slave_sae: str
    delivered: bool = False


@dataclass
class KeyStore:
    """Shared by both KMEs — the stand-in for the QKD link."""

    allow_replay: bool = False
    _keys: dict[str, KeyRecord] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def mint(self, master_sae: str, slave_sae: str, number: int, size_bits: int) -> list[KeyRecord]:
        out = []
        with self._lock:
            for _ in range(number):
                rec = KeyRecord(
                    key_ID=str(uuid.uuid4()),
                    key=os.urandom(size_bits // 8),
                    master_sae=master_sae,
                    slave_sae=slave_sae,
                )
                self._keys[rec.key_ID] = rec
                out.append(rec)
        return out

    def resolve(self, key_id: str, master_sae: str) -> KeyRecord:
        with self._lock:
            rec = self._keys.get(key_id)
            if rec is None:
                raise KeyError(f"unknown key_ID {key_id}")
            if rec.master_sae != master_sae:
                raise PermissionError(
                    f"key_ID {key_id} was issued with master {rec.master_sae}, not {master_sae}"
                )
            if rec.delivered and not self.allow_replay:
                raise PermissionError(f"key_ID {key_id} already delivered (single-delivery)")
            rec.delivered = True
            return rec

    def stored_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._keys.values() if not r.delivered)


def _container(records: list[KeyRecord]) -> dict:
    return {
        "keys": [
            {"key_ID": r.key_ID, "key": base64.b64encode(r.key).decode()} for r in records
        ]
    }


def make_handler(kme_id: str, peer_kme_id: str, store: KeyStore, quiet: bool = False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MockKME/ETSI-014"

        def log_message(self, fmt, *args):  # quieter, prefixed
            if not quiet:
                print(f"  [{kme_id}] {fmt % args}")

        # --- helpers -------------------------------------------------------
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code: int, message: str) -> None:
            self._send(code, {"message": message})

        def _requester(self) -> str:
            """Real KMEs identify the SAE from its mTLS client cert. The mock
            accepts an X-SAE-ID header so the flow works over plain http too."""
            return self.headers.get("X-SAE-ID", "unknown-SAE")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return {}

        def _route(self):
            parts = urlparse(self.path)
            seg = [p for p in parts.path.split("/") if p]
            # expect: api v1 keys {SAE_ID} {action}
            if len(seg) != 5 or seg[0:3] != ["api", "v1", "keys"]:
                return None, None, None
            return seg[3], seg[4], parse_qs(parts.query)

        # --- methods -------------------------------------------------------
        def do_GET(self):
            sae_id, action, query = self._route()
            if action is None:
                return self._err(404, "not an ETSI GS QKD 014 route")
            if action == "status":
                return self._send(200, self._status(sae_id))
            if action == "enc_keys":
                number = int(query.get("number", ["1"])[0])
                size = int(query.get("size", [str(DEFAULT_KEY_SIZE)])[0])
                return self._enc(sae_id, number, size)
            if action == "dec_keys":
                ids = query.get("key_ID", [])
                return self._dec(sae_id, ids)
            return self._err(404, f"unknown action {action}")

        def do_POST(self):
            sae_id, action, _ = self._route()
            if action is None:
                return self._err(404, "not an ETSI GS QKD 014 route")
            body = self._body()
            if action == "enc_keys":
                return self._enc(
                    sae_id,
                    int(body.get("number", 1)),
                    int(body.get("size", DEFAULT_KEY_SIZE)),
                )
            if action == "dec_keys":
                ids = [e.get("key_ID") for e in body.get("key_IDs", []) if e.get("key_ID")]
                return self._dec(sae_id, ids)
            return self._err(404, f"unknown action {action}")

        # --- ETSI 014 semantics --------------------------------------------
        def _status(self, slave_sae_id: str) -> dict:
            return {
                "source_KME_ID": kme_id,
                "target_KME_ID": peer_kme_id,
                "master_SAE_ID": self._requester(),
                "slave_SAE_ID": slave_sae_id,
                "key_size": DEFAULT_KEY_SIZE,
                "stored_key_count": store.stored_count(),
                "max_key_count": 100000,
                "max_key_per_request": MAX_KEY_PER_REQUEST,
                "max_key_size": MAX_KEY_SIZE,
                "min_key_size": MIN_KEY_SIZE,
                "max_SAE_ID_count": 0,
            }

        def _enc(self, slave_sae_id: str, number: int, size: int):
            if not 1 <= number <= MAX_KEY_PER_REQUEST:
                return self._err(400, f"number must be 1..{MAX_KEY_PER_REQUEST}")
            if not MIN_KEY_SIZE <= size <= MAX_KEY_SIZE or size % 8:
                return self._err(400, f"size must be {MIN_KEY_SIZE}..{MAX_KEY_SIZE} and a multiple of 8")
            recs = store.mint(self._requester(), slave_sae_id, number, size)
            return self._send(200, _container(recs))

        def _dec(self, master_sae_id: str, key_ids: list[str]):
            if not key_ids:
                return self._err(400, "no key_IDs supplied")
            recs = []
            for kid in key_ids:
                try:
                    recs.append(store.resolve(kid, master_sae_id))
                except KeyError as exc:
                    return self._err(404, str(exc))
                except PermissionError as exc:
                    return self._err(401, str(exc))
            return self._send(200, _container(recs))

    return Handler


def serve(kme_id, peer_kme_id, port, store, tls_ctx=None, quiet: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(kme_id, peer_kme_id, store, quiet)
    )
    if tls_ctx:
        httpd.socket = tls_ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def build_tls_context(certdir: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(f"{certdir}/kme.crt", f"{certdir}/kme.key")
    ctx.load_verify_locations(f"{certdir}/ca.crt")
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS, as real KMEs require
    return ctx


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock ETSI GS QKD 014 KME pair")
    ap.add_argument("--port-a", type=int, default=8010)
    ap.add_argument("--port-b", type=int, default=8020)
    ap.add_argument("--tls", action="store_true", help="serve https with mutual TLS")
    ap.add_argument("--certdir", default="certs")
    ap.add_argument("--allow-replay", action="store_true",
                    help="disable single-delivery consumption (debugging only)")
    ap.add_argument("--quiet", action="store_true", help="suppress per-request logging")
    args = ap.parse_args()

    store = KeyStore(allow_replay=args.allow_replay)
    ctx = build_tls_context(args.certdir) if args.tls else None
    scheme = "https" if args.tls else "http"

    serve("KME-A", "KME-B", args.port_a, store, ctx, args.quiet)
    serve("KME-B", "KME-A", args.port_b, store, ctx, args.quiet)
    print(f"KME-A  {scheme}://127.0.0.1:{args.port_a}")
    print(f"KME-B  {scheme}://127.0.0.1:{args.port_b}")
    print("shared keystore up — Ctrl-C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
