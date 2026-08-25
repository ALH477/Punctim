# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""The two halves of an ETSI GS QKD 014 key-delivery exchange, wired to DCF.

    MasterSae --enc_keys--> KME-A                       (ETSI GS QKD 014)
    MasterSae --key-ID beacon (4 DeModFrames)--> SlaveSae   (DCF: the out-of-scope bit)
    SlaveSae  --dec_keys--> KME-B                       (ETSI GS QKD 014)
    => both ends hold identical key material

Roles are per-key, not per-node: the SAE that calls enc_keys is the master *for those
keys*; a bidirectional peer plays both roles depending on traffic direction.

The beacon carries ONLY the key_ID.  Key material arrives over the KME's own channel
and never touches a DeModFrame — see Documentation/DCF_QKD_SPEC.md.
"""
import threading

from .beacon import KeyIdBeacon


class MasterSae:
    """Requests fresh keys from its KME and announces each key_ID over DCF."""

    def __init__(self, client, peer_sae_id, transport, node_id, channel):
        self.client = client
        self.peer_sae_id = peer_sae_id
        self.beacon = KeyIdBeacon(transport, node_id, channel)
        self.keys = {}          # key_ID -> key bytes (held in memory; see export note)

    def status(self):
        return self.client.status(self.peer_sae_id)

    def request_keys(self, number=1, size=256):
        """enc_keys: ask the KME for `number` fresh keys of `size` bits."""
        keys = self.client.get_key(self.peer_sae_id, number=number, size=size)
        for k in keys:
            self.keys[k.key_ID] = k.key
        return keys

    def announce(self, key_id, epoch=None):
        """Beacon one key_ID to the slave as four ordinary CTRL DeModFrames."""
        return self.beacon.announce(key_id, epoch=epoch)

    def request_and_announce(self, number=1, size=256):
        """The whole master-side flow.  Returns [(key, epoch), ...]."""
        out = []
        for k in self.request_keys(number=number, size=size):
            out.append((k, self.announce(k.key_ID)))
        return out

    def forget(self):
        """Drop retained key material.  Call once a session is done."""
        self.keys.clear()


class SlaveSae:
    """Receives key_ID beacons over DCF and resolves each against its own KME."""

    def __init__(self, client, master_sae_id, transport, node_id, channel,
                 on_key=None):
        self.client = client
        self.master_sae_id = master_sae_id
        self.on_key = on_key
        self.keys = {}          # key_ID -> key bytes
        self.errors = []
        self._lock = threading.Lock()
        self.beacon = KeyIdBeacon(transport, node_id, channel,
                                  on_key_id=self._on_key_id)

    def start(self):
        self.beacon.start()
        return self

    def stop(self):
        self.beacon.stop()

    def _on_key_id(self, key_id, meta):
        """dec_keys: resolve a beaconed key_ID into the key material itself."""
        try:
            fetched = self.client.get_key_with_ids(self.master_sae_id, [key_id])
        except Exception as exc:              # KmeError, or a transport-level failure
            with self._lock:
                self.errors.append((key_id, str(exc)))
            return
        for k in fetched:
            with self._lock:
                self.keys[k.key_ID] = k.key
            if self.on_key:
                self.on_key(k, meta)

    def forget(self):
        with self._lock:
            self.keys.clear()
