#!/usr/bin/env python3
"""Convert Punctim into a JSONL training dataset.

Modes:
  wire     — spec → codec implementation (teaches wire protocol)
  adapter  — spec + wire codec → adapter implementation (teaches adapter patterns)
  certify  — vectors + implementation → verification code (teaches certification)

Usage:
  python build_punctim_dataset.py --mode wire --out wire_train.jsonl
  python build_punctim_dataset.py --mode adapter --out adapter_train.jsonl
  python build_punctim_dataset.py --mode certify --out certify_train.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent
DOC_DIR = REPO_ROOT / "Documentation"

# ── Discovery ───────────────────────────────────────────────────────────────

CODEC_FILES = {
    "C": REPO_ROOT / "codec" / "demod_frame.h",
    "Rust": REPO_ROOT / "codec" / "src" / "lib.rs",
    "Python": REPO_ROOT / "python" / "MCP" / "wirelab_core.py",
    "Lua": REPO_ROOT / "GUI" / "wirelab.lua",
    "Haskell": REPO_ROOT / "haskell" / "src" / "DCF" / "Transport" / "FrameSpec.hs",
    "Lisp": REPO_ROOT / "lisp" / "src" / "wire.lisp",
}

ADAPTER_FILES = {
    "audio": {
        "spec": DOC_DIR / "DCF_AUDIO_SPEC.md",
        "C": REPO_ROOT / "codec" / "demod_audio.c",
        "Rust": REPO_ROOT / "codec" / "src" / "audio.rs",
        "Python": REPO_ROOT / "python" / "MCP" / "audiolab_core.py",
        "Lua": REPO_ROOT / "lua" / "dcf_audio.lua",
        "vectors": DOC_DIR / "audio_vectors.json",
    },
    "game": {
        "spec": DOC_DIR / "DCF_GAME_SPEC.md",
        "C": REPO_ROOT / "codec" / "demod_game.c",
        "Rust": REPO_ROOT / "codec" / "src" / "game.rs",
        "Python": REPO_ROOT / "python" / "MCP" / "gamelab_core.py",
        "vectors": DOC_DIR / "game_vectors.json",
    },
    "text": {
        "spec": DOC_DIR / "DCF_TEXT_SPEC.md",
        "Python": REPO_ROOT / "python" / "MCP" / "textlab_core.py",
        "Rust": REPO_ROOT / "codec" / "src" / "text.rs",
        "vectors": DOC_DIR / "text_vectors.json",
    },
}

CERTIFY_FILES = {
    "Python": REPO_ROOT / "python" / "MCP" / "verify_laws.py",
    "Rust": REPO_ROOT / "codec" / "tests" / "certify.rs",
    "C": REPO_ROOT / "C_SDK" / "tests" / "test_wire_certify.c",
}


def load_spec(path: Path) -> str | None:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def load_code(path: Path) -> str | None:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def load_vectors(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text())
    return None


def write_jsonl(records: list[dict], out: Path):
    with out.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    total_chars = sum(len(json.dumps(r, ensure_ascii=False)) for r in records)
    avg_chars = total_chars // max(len(records), 1)
    print(f"Wrote {len(records)} examples → {out}", file=sys.stderr)
    print(f"  total: {total_chars:,} chars | avg: {avg_chars:,} chars/line", file=sys.stderr)


# ── System prompts ──────────────────────────────────────────────────────────

SYSTEM_WIRE = """\
You are an expert DCF (DeMoD Communication Framework) wire protocol implementer. \
You write byte-certified codecs for the 17-byte DeModFrame across multiple languages.

THE WIRE QUANTUM:
- 17 bytes: sync(0xD3) | flags[ver|type] | seq | src | dst | payload(4B) | ts24 | crc16
- Version nibble = 1 (always)
- CRC-16/CCITT-FALSE over bytes [0..14], anchors: CRC("123456789")=0x29B1, CRC(0^15)=0x4EC3
- Valid iff sync byte + version nibble + CRC match

CONVENTIONS:
- Encode: take fields → pack into 17-byte buffer → compute CRC → write CRC at [15..16]
- Decode: verify sync + version + CRC → unpack fields → return struct or error
- All implementations MUST produce byte-identical output for identical input
- Use big-endian for multi-byte fields (network byte order)
- CRC polynomial: 0x1021, init: 0xFFFF, no final XOR
- Test against golden_vectors.json (246 vectors: 109 encode + 137 syndrome)
"""

SYSTEM_ADAPTER = """\
You are an expert DCF adapter implementer. You build protocols (audio, game, text, SSTV, snake, mesh) \
as adapters over the 17-byte DeModFrame wire quantum.

ADAPTER PATTERN:
- One adapter message → 1 + ceil(payload_len/4) DeModFrame packets
- seq = packet_id[15:5] | frag_idx[4:0] (for audio/game); text uses frag_idx[9:0]
- frag_idx 0 = descriptor: [len, frag_total, codec_id|msg_type_id, flags]
- Payload ≤ 124 B/block (4 bytes per frame × 31 frames max)
- L2 framing is codec/message-type-agnostic and byte-certified
- Adding new codecs/message types never changes the wire vectors

CONVENTIONS:
- Encode: serialize payload → fragment into ≤124B chunks → wrap each in DeModFrame
- Decode: collect frames by packet_id → reassemble payload → validate CRC per frame
- Use CTRL (type 3) frames for audio; DATA (type 0) frames for game/text/SSTV
- Descriptor flags: bit0 RELIABLE, bit1 ORDERED, bit2 END_TICK (adapter-specific)
"""

SYSTEM_CERTIFY = """\
You are an expert DCF certification engineer. You write verification code that ensures \
cross-language byte-identical behavior of DCF codecs and adapters.

CERTIFICATION PATTERN:
- Golden vectors: JSON array of {input, expected_output} test cases
- Encode vectors: input fields → expected 17-byte buffer
- Syndrome vectors: input buffer → expected decoded fields or error
- All languages must pass all 246 vectors (109 encode + 137 syndrome)
- Regenerate vectors from reference (Python), diff against committed

CONVENTIONS:
- Load vectors from Documentation/golden_vectors.json (or audio_vectors.json, etc.)
- For each vector: encode/decode with your implementation → compare to expected
- Print PASS/FAIL per vector, exit non-zero on any failure
- Use exact byte comparison (no floating-point tolerance)
- CI runs on every push/PR to main
"""

# ── Mode: wire ──────────────────────────────────────────────────────────────

def build_wire_examples() -> list[dict]:
    records = []
    spec = load_spec(DOC_DIR / "WIRE_QUANTUM_SPEC.md")
    vectors = load_vectors(DOC_DIR / "golden_vectors.json")
    
    if not spec:
        print("Warning: WIRE_QUANTUM_SPEC.md not found", file=sys.stderr)
        return records
    
    for lang, path in CODEC_FILES.items():
        code = load_code(path)
        if not code:
            continue
        
        user_parts = [
            f"Implement the DCF wire codec in {lang}.",
            f"\nSpecification:\n{spec}",
        ]
        if vectors:
            sample = {
                "encode_basis": vectors.get("encode_basis", [])[:10],
                "syndrome_basis": vectors.get("syndrome_basis", [])[:10],
            }
            user_parts.append(f"\nGolden vectors (sample):\n{json.dumps(sample, indent=2)}")
        user_parts.append("\nProvide the complete implementation with encode/decode functions.")
        
        records.append({"messages": [
            {"role": "system", "content": SYSTEM_WIRE},
            {"role": "user", "content": "\n".join(user_parts)},
            {"role": "assistant", "content": code},
        ]})
    
    return records


# ── Mode: adapter ───────────────────────────────────────────────────────────

def build_adapter_examples() -> list[dict]:
    records = []
    wire_spec = load_spec(DOC_DIR / "WIRE_QUANTUM_SPEC.md")
    
    for adapter_name, files in ADAPTER_FILES.items():
        spec = load_spec(files["spec"])
        if not spec:
            continue
        
        vectors_path = files.get("vectors")
        vectors = load_vectors(vectors_path) if vectors_path else None
        
        for lang, path in files.items():
            if lang in ("spec", "vectors"):
                continue
            code = load_code(path)
            if not code:
                continue
            
            user_parts = [
                f"Implement the DCF {adapter_name} adapter in {lang}.",
            ]
            if wire_spec:
                user_parts.append(f"\nWire quantum spec (for context):\n{wire_spec[:2000]}...")
            user_parts.append(f"\nAdapter specification:\n{spec}")
            if vectors:
                sample = {
                    "encode_basis": vectors.get("encode_basis", [])[:5],
                    "syndrome_basis": vectors.get("syndrome_basis", [])[:5],
                }
                user_parts.append(f"\nGolden vectors (sample):\n{json.dumps(sample, indent=2)}")
            user_parts.append("\nProvide the complete implementation with encode/decode functions.")
            
            records.append({"messages": [
                {"role": "system", "content": SYSTEM_ADAPTER},
                {"role": "user", "content": "\n".join(user_parts)},
                {"role": "assistant", "content": code},
            ]})
    
    return records


# ── Mode: certify ───────────────────────────────────────────────────────────

def build_certify_examples() -> list[dict]:
    records = []
    vectors = load_vectors(DOC_DIR / "golden_vectors.json")
    
    if not vectors:
        print("Warning: golden_vectors.json not found", file=sys.stderr)
        return records
    
    for lang, path in CERTIFY_FILES.items():
        code = load_code(path)
        if not code:
            continue
        
        user_parts = [
            f"Write certification/verification code in {lang} for the DCF wire codec.",
            f"\nGolden vectors (sample):\n{json.dumps({'encode_basis': vectors.get('encode_basis', [])[:20], 'syndrome_basis': vectors.get('syndrome_basis', [])[:20]}, indent=2)}",
            "\nThe code should:",
            "1. Load the vectors from a JSON file",
            "2. For each encode vector: encode the input fields → compare to expected buffer",
            "3. For each syndrome vector: decode the input buffer → compare to expected fields",
            "4. Print PASS/FAIL per vector, exit non-zero on any failure",
            "\nProvide the complete implementation.",
        ]
        
        records.append({"messages": [
            {"role": "system", "content": SYSTEM_CERTIFY},
            {"role": "user", "content": "\n".join(user_parts)},
            {"role": "assistant", "content": code},
        ]})
    
    return records


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Build JSONL training dataset from Punctim")
    ap.add_argument("--mode", choices=["wire", "adapter", "certify"], default="wire")
    ap.add_argument("--out", type=Path, default=Path("punctim_train.jsonl"))
    ap.add_argument("--min-chars", type=int, default=0, help="Skip examples where assistant content < N chars")
    args = ap.parse_args()

    builders = {"wire": build_wire_examples, "adapter": build_adapter_examples, "certify": build_certify_examples}
    records = builders[args.mode]()

    if args.min_chars:
        before = len(records)
        records = [r for r in records if len(r["messages"][-1]["content"]) >= args.min_chars]
        print(f"Filtered {before - len(records)} examples < {args.min_chars} chars", file=sys.stderr)

    write_jsonl(records, args.out)


if __name__ == "__main__":
    main()
