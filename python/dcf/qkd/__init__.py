# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF-QKD — a bridge from DCF/Punctim to ETSI GS QKD 014 key delivery.

  beacon   — key-ID beacon over any DCF transport (thin runtime over qkdlab_core)
  etsi014  — ETSI GS QKD 014 client: status / enc_keys / dec_keys, optional mTLS
  mock_kme — a mock KME pair over one shared keystore (the simulated QKD link)
  sae      — MasterSae / SlaveSae: the two halves of a key-delivery exchange
  demo     — end-to-end harness + negative tests, hardware-free over loopback

ETSI GS QKD 014 deliberately leaves the transport of `key_ID` from master SAE to
slave SAE OUT OF SCOPE.  This package fills that gap with the wire quantum, and
speaks the rest of the standard unmodified so real KME hardware interoperates.

NOTHING HERE IS QUANTUM.  DCF's "wire quantum" is quantum as in *quanta* — the
indivisible 17-byte DeModFrame.  Key material comes from external KME hardware
(from os.urandom in the mock); there are no photons in this repository.

EXPORT / SECURITY (normative): this package implements no cryptographic algorithm.
The wire carries only the key_ID — a non-secret 128-bit identifier.  Key material
MUST NOT be placed in a DeModFrame payload; it never enters the codec or the wire.

An adapter over the wire quantum: the 246-vector certificate is untouched.
See Documentation/DCF_QKD_SPEC.md.
"""
