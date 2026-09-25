#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""DCF WireLab — MCP server for the DeMoD 17-byte wire quantum.

Run as MCP (stdio):     python3 wirelab_mcp.py
Self-test:              python3 wirelab_mcp.py --selftest
Client config example:  {"mcpServers": {"dcf-wirelab":
                          {"command": "python3", "args": ["/path/to/wirelab_mcp.py"]}}}

Companion artifacts: wirelab_core.py (reference codec), golden_vectors.json
(finite affine certificate), wirelab.lua (DeMoD UI front panel). All four share
one codec definition; the vectors are the cross-language source of truth.
"""
import json, pathlib, sys
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp 2.x renamed FastMCP -> MCPServer and changed the API
    FastMCP = None
import wirelab_core as core

HERE = pathlib.Path(__file__).resolve().parent


class _NoMCP:
    """Stand-in for FastMCP when the `mcp` SDK is absent or is the incompatible 2.x.

    The tools below are ordinary functions that the decorator merely registers, so
    `--selftest` certifies the codec without the SDK installed. Only `run()` (the
    actual stdio server) needs it. This keeps the certification path free of a
    third-party API whose breaking changes would otherwise fail CI.
    """

    def tool(self, *_a, **_k):
        return lambda fn: fn

    def run(self):
        raise SystemExit(
            "the `mcp` package (v1) is required to serve: pip install 'mcp<2'\n"
            "(the codec self-test needs no SDK: python3 wirelab_mcp.py --selftest)"
        )


mcp = FastMCP("dcf-wirelab") if FastMCP is not None else _NoMCP()

FIELD_MAP = [("sync", 0, 1), ("flags(ver|type)", 1, 2), ("seq", 2, 4), ("src", 4, 6),
             ("dst", 6, 8), ("payload", 8, 12), ("ts_us", 12, 15), ("crc16", 15, 17)]

def _bytes(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr.replace(" ", "").replace("0x", ""))

@mcp.tool()
def crc16_ccitt(data_hex: str) -> dict:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) of arbitrary hex bytes.
    This is the DeModFrame integrity function, computed over frame bytes [0..14]."""
    crc = core.crc16_ccitt(_bytes(data_hex))
    return {"crc": f"0x{crc:04X}", "crc_int": crc}

@mcp.tool()
def encode_frame(frame_type: int, seq: int, src: int, dst: int,
                 payload_hex: str = "00000000", ts_us: int = 0) -> dict:
    """Encode one DCF wire quantum (17-byte DeModFrame, version 1).
    frame_type: 0=FData 1=FAck 2=FBeacon 3=FCtrl. payload_hex: exactly 4 bytes.
    dst=65535 (0xFFFF) is broadcast. ts_us is a 24-bit microsecond timestamp (wraps ~16.7 s)."""
    payload = _bytes(payload_hex)
    w = core.encode(frame_type, seq, src, dst, payload, ts_us)
    return {"frame_hex": w.hex().upper(), "fields": core.decode(w)}

@mcp.tool()
def decode_frame(frame_hex: str) -> dict:
    """Decode and validate a 17-byte hex frame. Returns fields, or valid=false with
    the exact reason (length / sync / version / CRC) and the computed syndrome."""
    w = _bytes(frame_hex)
    try:
        return {"valid": True, "fields": core.decode(w)}
    except ValueError as e:
        out = {"valid": False, "reason": str(e)}
        if len(w) == core.FRAME_LEN:
            out["syndrome"] = f"0x{core.syndrome(w):04X}"
        return out

@mcp.tool()
def field_map(frame_hex: str) -> dict:
    """Annotated byte/field map of a 17-byte frame — which bytes belong to which field,
    with per-field hex. Useful for hexdump-style inspection regardless of validity."""
    w = _bytes(frame_hex)
    if len(w) != core.FRAME_LEN:
        return {"error": f"length {len(w)} != 17"}
    return {"bytes": " ".join(f"{b:02X}" for b in w),
            "fields": [{"field": n, "bytes": w[a:b].hex().upper(), "offset": [a, b - 1]}
                       for n, a, b in FIELD_MAP],
            "version": w[1] >> 4, "frame_type": w[1] & 0xF,
            "syndrome": f"0x{core.syndrome(w):04X}"}

@mcp.tool()
def bitflip_audit(frame_hex: str) -> dict:
    """Quantum-integrity audit: flip every one of the 136 bits of a valid frame and
    confirm each corruption is rejected. Proves single-bit error detection for this frame."""
    w = _bytes(frame_hex)
    core.decode(w)  # must be valid to start
    rejected = []
    for bit in range(core.FRAME_LEN * 8):
        bad = bytearray(w); bad[bit // 8] ^= 1 << (7 - bit % 8)
        try:
            core.decode(bytes(bad)); rejected.append(False)
        except ValueError:
            rejected.append(True)
    return {"bits_tested": len(rejected), "all_rejected": all(rejected),
            "accepted_bits": [i for i, r in enumerate(rejected) if not r]}

@mcp.tool()
def certify(implementation_vectors_json: str = "") -> dict:
    """Run the finite affine certificate. With no argument, re-verifies the reference
    codec against golden_vectors.json. To certify ANOTHER implementation (C, Lisp,
    Haskell, Rust, ASM), pass its emitted vectors as JSON: {"encode_basis":[{"frame":hex},...],
    "syndrome_basis":[{"syndrome":int},...]} in golden order. Agreement on these
    109+137 vectors equals agreement on all 2^108 frames / 2^136 words (see CATEGORY.md, Thm 4)."""
    golden = json.loads((HERE / "golden_vectors.json").read_text())
    subject = json.loads(implementation_vectors_json) if implementation_vectors_json else golden
    enc_diffs = [i for i, (g, s) in enumerate(zip(golden["encode_basis"], subject["encode_basis"]))
                 if g["frame"].lower() != s["frame"].lower()]
    syn_diffs = [i for i, (g, s) in enumerate(zip(golden["syndrome_basis"], subject["syndrome_basis"]))
                 if int(g["syndrome"]) != int(s["syndrome"])]
    cemented = not enc_diffs and not syn_diffs and \
        len(subject["encode_basis"]) == len(golden["encode_basis"]) and \
        len(subject["syndrome_basis"]) == len(golden["syndrome_basis"])
    return {"cemented": cemented, "encode_mismatches": enc_diffs[:8],
            "syndrome_mismatches": syn_diffs[:8],
            "anchors": golden["anchors"], "theorem": golden["theorem"]}

@mcp.tool()
def golden_vectors(section: str = "anchors") -> dict:
    """Fetch the certificate: section = 'anchors' | 'encode_basis' | 'syndrome_basis' | 'all'."""
    golden = json.loads((HERE / "golden_vectors.json").read_text())
    return golden if section == "all" else {section: golden[section]}

def _selftest() -> int:
    ex = encode_frame(0, 0x1234, 1, 0xFFFF, "DEADBEEF", 0xAB12CD)
    assert ex["frame_hex"] == "D31012340001FFFFDEADBEEFAB12CDA963", ex["frame_hex"]
    assert decode_frame(ex["frame_hex"])["valid"]
    assert not decode_frame(ex["frame_hex"][:-1] + "0")["valid"]
    audit = bitflip_audit(ex["frame_hex"])
    assert audit["all_rejected"] and audit["bits_tested"] == 136
    assert crc16_ccitt("313233343536373839")["crc"] == "0x29B1"
    cert = certify()
    assert cert["cemented"]
    print("WIRELAB SELFTEST: all tools verified against the certificate")
    print(f"  exampleFrame = {ex['frame_hex']}")
    return 0

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    mcp.run()
