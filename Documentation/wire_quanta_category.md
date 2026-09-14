# The DeMoD Wire Quantum: A Categorical Formalization

**DeMoD LLC · DCF mono repo**
Companion to the Pontryagin-duality / Fourier treatment of the handshakeless
channel. This document supplies the **codec / algebraic** half: it proves that
the 17-byte `DeModFrame` is *cemented* — uniquely determined, uniquely decodable,
and uniquely realized across every language SDK — and reduces "cemented" from
rhetoric to a checkable universal property with a finite certificate.

All claims here are machine-anchored. The constants `0x29B1`, `0x4EC3`, `0xA963`
and the frame `D31012340001FFFFDEADBEEFAB12CDA963` are produced by
`verify_laws.py` and re-checked by `wirelab_mcp.py --selftest`, the SBCL run in
`punctim-hotfix.lisp`, and the Lua self-test in `wirelab.lua`. Notation is
plain Unicode; ⊕ is GF(2) addition (XOR), B = GF(2), Bⁿ the n-bit words.

---

## §0. Scope and how this composes with the spectral picture

The spectral document treats a frame as a point of a compact abelian group —
header in ℤ/136ℤ-style coordinates, payload in ℤ/32ℤ — and reads transmission
through Pontryagin duality and the Fourier transform, with *naturality* of the
dual pairing across media as the central structural fact. That picture is about
**how the channel carries the group**.

This document is dual in spirit and disjoint in content. It treats a frame as an
**object assembled by a codec** and asks what makes that assembly canonical:

- §1–§3: the frame as a **retract** of raw bytes (`encode`/`decode` as a
  section/retraction pair) and validity as an **equalizer**.
- §4: traffic as a **free monoid** of quanta — unique decipherability.
- §5: the spec as a **finite limit**; every SDK as a **cone**; interoperability
  as the **unique mediating isomorphism**. *This is the definition of "cemented."*
- §6: the **finite certificate** — affineness over GF(2) collapses
  "agree on all 2¹⁰⁸ frames" to "agree on 109 + 137 vectors."
- §7: a worked failure — the shipped Punctim CRC bug as a *broken equalizer*,
  SBCL-verified before and after.
- §8: where the two documents meet (commuting functors; RTT as enrichment).

The bridge in one line: the spectral document fixes the *group* the wire carries;
this one fixes the *map* that names points of that group, and proves the map is
forced. Together they pin both the carrier and the coordinates.

---

## §1. The frame functor and the wire object

Fix the version-1 layout (big-endian), 17 bytes = 136 bits:

```
[0]      sync      = 0xD3                      (8 bits, constant)
[1]      ver|type  = (1<<4) | type             (ver nibble constant, type∈2⁴)
[2..3]   seq        u16                         (16 bits)
[4..5]   src        u16                         (16 bits)
[6..7]   dst        u16                         (16 bits)
[8..11]  payload    4 bytes                     (32 bits)
[12..14] ts_us      u24                         (24 bits)
[15..16] crc16      CRC-16/CCITT-FALSE([0..14]) (16 bits, derived)
```

**Field object.** The *free* (unconstrained) field data is the product set

  Φ = T × S × A × A × P × U,

with T = 2⁴ (type nibble), S = A = 2¹⁶ (seq, and src/dst share the address set
A), P = 2³² (payload), U = 2²⁴ (timestamp). So |Φ| = 2⁴⁺¹⁶⁺¹⁶⁺¹⁶⁺³²⁺²⁴ = **2¹⁰⁸**.
The sync byte and version nibble are *not* free coordinates; they are constants
the codec writes, which is exactly why they belong to validity (§2), not to Φ.

**Wire object.** W = B¹⁷ = GF(2)¹³⁶, the raw 17-byte words, |W| = 2¹³⁶.

**Encoding.** `encode : Φ → W` places fields, writes sync/version, and appends
the CRC. **Decoding** `decode : W ⇀ Φ` is partial: it checks length, sync,
version nibble, and CRC, then reads fields.

> **Proposition 1.1 (encode is affine over GF(2)).**
> Writing Φ ≅ GF(2)¹⁰⁸ by concatenating field bits, `encode` is an affine map
> GF(2)¹⁰⁸ → GF(2)¹³⁶, i.e. encode(x) = M·x ⊕ c for a fixed matrix M and vector c.

*Proof.* Field placement is a fixed permutation-with-padding of input bits into
output positions — GF(2)-linear. The sync byte and version nibble are constants,
contributing to c. The CRC of bytes [0..14] is computed by the CCITT recurrence
crcᵢ₊₁ = (crcᵢ ⊕ bᵢ·2⁸) shifted with conditional XOR by the polynomial; each step
is GF(2)-affine in the running state, and the nonzero init 0xFFFF makes the whole
map affine rather than linear. Affine ∘ linear = affine, and the CRC bits are an
affine function of [0..14], themselves affine in x. Hence encode is affine. ∎

`verify_laws.py` checks the affine signature of the CRC empirically:
crc(x⊕y) = crc(x) ⊕ crc(y) ⊕ crc(0) on 500 random pairs, with crc(0¹⁵) = **0x4EC3**.

---

## §2. Validity as an equalizer

Let the **syndrome map** σ : W → GF(2)¹⁶ be

  σ(w) = CRC([w₀..w₁₄]) ⊕ (w₁₅‖w₁₆),

the XOR of the recomputed CRC with the stored CRC. By Prop 1.1, σ is **affine**.

Let the **structural constraints** be the two affine conditions
sync(w) = 0xD3 and ver(w) = 1, each an affine equation κⱼ(w) = constant.

> **Definition 2.1 (Valid).** `Valid ⊆ W` is the simultaneous solution set
> { w : σ(w) = 0, sync(w) = 0xD3, ver(w) = 1 }.

Categorically, `Valid` is a **finite limit**: the pullback of the constant maps
along (σ, sync, ver). Equivalently it is the **equalizer** of the pair
(check, const) : W ⇉ GF(2)¹⁶ × B⁸ × B⁴ where `check` computes (σ, sync, ver) and
`const` returns (0, 0xD3, 1). This is the precise sense in which "a valid frame"
is not a predicate bolted on after the fact but a *universal* sub-object of W.

---

## §3. The frame is a cemented retract (Theorem 1)

> **Theorem 1 (Cemented codec).**
> (a) `encode : Φ → W` is a split monomorphism (injective with a left inverse).
> (b) Its corestriction `encode : Φ → Valid` is a **bijection**.
> (c) `decode` restricted to `Valid` is the **two-sided inverse** of (b); on
>     W∖Valid, `decode` is undefined. Thus Φ is a **retract** of W:
>     decode ∘ encode = id_Φ, and encode ∘ decode = id on Valid.
> (d) Any decoder agreeing with the spec's accept/reject behavior and field
>     readout equals `decode` on all of W — the retraction is **unique**.

*Proof.*
(a) Distinct field tuples differ in at least one placed bit, and placement is
injective into [0..14]∪payload positions, so encode is injective. decode∘encode
reads back exactly the placed fields ⇒ id_Φ; that is the left inverse.

(b) encode lands in `Valid`: it writes sync = 0xD3, ver = 1, and sets the CRC
bytes so σ = 0 by construction. It is injective by (a). For surjectivity, take
w ∈ Valid. Read the 108 field bits to get x ∈ Φ. Then encode(x) agrees with w on
all field positions, on sync and version (both fixed by w ∈ Valid), and on the
CRC: encode(x) recomputes CRC([0..14]); since w’s [0..14] equal encode(x)’s
[0..14] and σ(w) = 0 forces w’s stored CRC to equal CRC of its own [0..14],
the CRC bytes match too. So encode(x) = w.

(c) Immediate from (b): a bijection onto Valid composed with its inverse. decode
rejects everything outside Valid by Definition 2.1.

(d) Suppose D : W ⇀ Φ matches the spec: it accepts exactly Valid and returns the
documented field bits. On Valid, "documented field bits" *is* the inverse of the
bijection in (b), so D = decode there. Off Valid both are undefined. ∎

**Reading.** "Atomic" is now a theorem, not a hope: Φ sits inside W as a retract
whose image is *exactly* the valid words, and the decoder that realizes the
retraction is unique. There is nothing to standardize twice.

`verify_laws.py` exercises Theorem 1 directly: decode∘encode = id on 2032 frames
(random + every corner of each field), and encode∘decode = id on 500 valid words.

---

## §4. Traffic is a free monoid of quanta (Theorem 2)

Let Φ* be finite sequences of frames (the free monoid on Φ, concatenation as ∘,
empty sequence as unit) and B* the free monoid of byte strings. Extend the codec:
`encode* : Φ* → B*` concatenates 17-byte encodings.

> **Lemma 2.1 (Self-synchronization bound).** Model an adversary-free channel as
> i.i.d. uniform bytes. The probability a fixed 17-byte window is accepted by
> `decode` is exactly
>   P[sync]·P[ver | sync]·P[σ = 0 | sync, ver] = 2⁻⁸ · 2⁻⁴ · 2⁻¹⁶ = **2⁻²⁸**.

*Proof.* The three constraints are affine and **independent** in coordinates:
sync fixes byte 0 (2⁻⁸); the version nibble fixes 4 bits of byte 1 (2⁻⁴);
conditioned on the first 15 bytes, σ = 0 fixes the 16 CRC bits to one value out
of 2¹⁶ (2⁻¹⁶), since the stored CRC is uniform and independent of [0..14] under
the i.i.d. model. Multiply. ∎

So a random window false-locks with probability 2⁻²⁸ ≈ 3.7×10⁻⁹; expected bytes
to a spurious frame ≈ 2²⁸. This is the quantitative content of "the sync byte +
fixed length + CRC self-synchronize."

> **Theorem 2 (Quanta / unique decipherability).**
> (a) `encode*` is an injective monoid homomorphism: Φ* embeds in B* as a
>     **uniform code** (every codeword has length 17).
> (b) On any *aligned* stream (a concatenation of whole frames), factorization
>     into frame quanta is **unique**: there is at most one way to cut an aligned
>     byte string into valid 17-byte frames, and `decode*` recovers it.

*Proof.* (a) A uniform-length code over an alphabet is automatically a prefix
code, hence uniquely decodable (no codeword is a prefix of another since all
share one length); injectivity of encode (Thm 1a) lifts to injectivity of the
concatenation. (b) Aligned cuts occur only at multiples of 17, so the partition
is forced; each block is decoded by the unique decoder of Thm 1d. ∎

**Caveat (honest).** On an *unaligned* raw stream, decoding is a partial map with
resync semantics — a valid frame could in principle begin mid-block. Lemma 2.1
bounds how often that misfires (2⁻²⁸ per offset) but does not make it impossible.
Uniqueness in (b) is a statement about aligned streams; resync is a probabilistic
guarantee, and the document is explicit about which is which. `verify_laws.py`
checks (b) on a 64-frame aligned stream.

---

## §5. "Cemented" = a universal property (Theorem 3)

This is the heart of the contribution: a definition of *cemented* that an SDK can
be tested against.

**The spec as a limit.** Present the frame specification as the diagram 𝒟 whose
cone apex is a set X equipped with:
- field projections π_T, π_seq, π_src, π_dst, π_pay, π_ts onto T, S, A, A, P, U;
- a structure map into W (the serialization);
- commuting constraints: the serialization satisfies sync = 0xD3, ver = 1, σ = 0,
  and re-reading bytes through the π's reproduces the projections.

> **Definition 5.1.** The **specification object** Spec is the limit lim 𝒟 in Set.
> By §1–§3 this limit exists and Spec ≅ Φ ≅ Valid, with the limiting cone given by
> the field reads and the inclusion Valid ↪ W.

**An SDK as a cone.** A language implementation L (C, Lisp, Haskell, Rust, Python,
x86 ASM) provides a set X_L (its in-memory frame type), field accessors, and an
`encode_L`/`decode_L` pair. *L conforms* iff (X_L, accessors, encode_L) is a cone
over 𝒟 — i.e. its accessors land in the right field sets and its serialization
meets sync/ver/σ and round-trips.

> **Theorem 3 (Cementing).**
> If L conforms, there is a **unique** mediating map u_L : X_L → Spec commuting
> with every projection and with serialization. If additionally encode_L is
> injective and onto Valid (L is *faithful*), u_L is an **isomorphism**.
> Consequently any two faithful conforming SDKs L, L′ satisfy a unique
> isomorphism φ = u_{L′}⁻¹ ∘ u_L : X_L ≅ X_{L′} that commutes with all field
> reads and with the wire — they are *the same codec up to canonical iso*.

*Proof.* Universal property of the limit: a cone over 𝒟 factors uniquely through
lim 𝒟 = Spec, giving u_L. Faithfulness makes encode_L a bijection onto Valid
(Thm 1b applied to L), and Spec ≅ Valid, so u_L is a bijection respecting the
cone, i.e. an iso. Compose two such isos over the shared apex Spec; uniqueness of
each mediating map gives uniqueness of φ. ∎

> **Definition 5.2 (Fully cemented).** The wire is **fully cemented** when
> (i) the version nibble is frozen (the limit 𝒟 is fixed, not a moving target),
> and (ii) every shipped SDK is a *faithful* cone over 𝒟. By Theorem 3 this is
> equivalent to: all SDKs are canonically isomorphic codecs, with the isomorphisms
> uniquely determined and mutually compatible. "Cemented" is then not a claim about
> any one implementation but a property of the *whole family* — there is one frame,
> realized one way, and the realizations cannot drift without breaking the cone.

This is the payoff: *cemented* is now falsifiable. §7 exhibits a real SDK that
**fails** to be a cone, and §6 gives the finite test that decides faithfulness.

---

## §6. The finite certificate (Theorem 4)

A universal property is only useful operationally if conformance is checkable.
Affineness makes it a finite check.

> **Theorem 4 (Finite certification).**
> Let L be any implementation whose encoder is **bit-placement + CRC** (hence
> affine, Prop 1.1) and whose acceptance test is "structural constants hold and
> σ = 0" (hence an affine syndrome). Then:
> (a) `encode_L = encode` on **all 2¹⁰⁸ frames** iff they agree on the **109
>     encode-basis vectors**: the zero input and the 108 unit inputs e_i.
> (b) `σ_L = σ` on **all 2¹³⁶ words** (so L classifies validity identically) iff
>     they agree on the **137 syndrome-basis vectors**: the zero word and the 136
>     single-bit words.
> Hence agreement on **109 + 137 = 246 vectors** ⇒ L is a faithful cone ⇒ (Thm 3)
> L is canonically isomorphic to the reference. The 246 vectors are a finite
> **proof object** for "cemented."

*Proof.* An affine map f(x) = M·x ⊕ c on GF(2)ⁿ is determined by f(0) = c and
f(e_i) = M·e_i ⊕ c (the i-th column of M, XOR c). Two affine maps agreeing on
{0, e₁, …, eₙ} agree on every x = ⊕_{i∈I} e_i because
f(⊕e_i) = (⊕ M e_i) ⊕ c = ⊕(f(e_i) ⊕ c) ⊕ c, fixed by the basis values. Apply
with n = 108 for encode (a) and n = 136 for σ (b). Faithfulness: matching σ
everywhere fixes Valid; matching encode everywhere fixes the bijection Φ → Valid;
together they make encode_L a faithful cone (Thm 1b). ∎

**Scope, stated honestly.** Theorem 4 certifies implementations *in the affine
class* — i.e. real bit-placement-plus-CRC codecs, which is what every DCF SDK is.
It is **not** a universal oracle: a pathological decoder doing nonlinear work
(e.g. a lookup table that special-cases one frame) is outside the hypothesis and
must be checked by other means. For the actual SDK family the hypothesis holds,
so the 246 vectors are decisive. This is a deliberate, declared boundary — the
certificate is a theorem about a class, not a magic universal test.

**Anchors (machine-checked).**

| quantity | value | produced by |
|---|---|---|
| CRC-16/CCITT-FALSE("123456789") | `0x29B1` | reference check value |
| CRC(0¹⁵) = affine offset c | `0x4EC3` | `verify_laws.py` |
| CRC(exampleFrame body [0..14]) | `0xA963` | cross-language anchor |
| exampleFrame (17 B) | `D31012340001FFFFDEADBEEFAB12CDA963` | Haskell `FrameSpec` ≡ Python ≡ Lua ≡ fixed Lisp |

The certificate ships as `golden_vectors.json` (109 encode + 137 syndrome
vectors). `wirelab_mcp.py`'s `certify` tool runs it; pass another SDK's emitted
vectors to certify that SDK.

---

## §7. Worked failure: the Punctim CRC as a broken equalizer

The shipped `crc16-ccitt` in `lisp/src/punctim.lisp` had an inner `let*` that
shadowed the running `crc`, so every iteration's update was discarded and the
function **returned `#xFFFF` for all input**. This is not just "a bug"; in the
language of §2 it is a *collapse of the syndrome map*.

> **Observation 7.1.** With crc ≡ const, the Lisp syndrome becomes
> σ_Lisp(w) = const ⊕ (w₁₅‖w₁₆), which is **independent of bytes [0..14]**.
> Its zero set `Valid_Lisp` = { w : sync = 0xD3, ver = 1, (w₁₅‖w₁₆) = const } has
> size 2¹³⁶⁻⁸⁻⁴⁻¹⁶ = **2¹⁰⁸ … wait — only the 28 structural+CRC bits are fixed**:
> |Valid_Lisp| = 2¹³⁶⁻²⁸ = 2¹⁰⁸ words *with the CRC bits pinned to one constant*,
> versus the true `Valid` which pins the CRC bits to a **data-dependent** value.

The two equalizers have the same cardinality but are *different sub-objects* of W:
the true `Valid` couples the last 2 bytes to the first 15; `Valid_Lisp` frees that
coupling and instead demands a fixed CRC literal. So:

- **encode_Lisp does not land in `Valid`** for general data (it writes `0xFFFF` as
  the CRC, valid only for the unique body whose true CRC is `0xFFFF`), so the Lisp
  encoder is **not a cone** over 𝒟 — Theorem 3's hypothesis fails, and there is
  **no** mediating isomorphism to the Haskell/Python/Rust codec.
- **Interop breaks asymmetrically**: a real peer rejects almost every Lisp frame
  (wrong CRC); the Lisp peer accepts corrupted frames whose first 15 bytes were
  mangled but whose last 2 bytes still read `0xFFFF`. The integrity guarantee of
  Lemma 2.1 degrades from 2⁻²⁸ to 2⁻²⁸ *against the wrong target* — it is checking
  a constant, not the data.

> **Verification (SBCL, in-container).**
> broken('123456789') = `#xFFFF` (expected `0x29B1`) — bug reproduced.
> fixed ('123456789') = `0x29B1`; fixed(exampleFrame body) = `0xA963` — cone
> restored. The fix is in `punctim-hotfix.lisp` (F1), which then asserts both
> anchors at load time and refuses to load if the codec is not a cone.

This is the formalism earning its keep: "the Lisp SDK fails to preserve the
validity equalizer, so it is not a cone over the spec and admits no mediating
iso" is a precise, true sentence that names exactly what broke and why every
other language rejected the frames.

---

## §8. Where the two documents meet (one paragraph)

The spectral/Pontryagin document and this one commute. Let Ŝ be its spectral
functor (frame ↦ point of the dual group, transmission ↦ Fourier-side action) and
let E be the codec functor of §5 (field tuple ↦ word in Valid). Both factor
through the same object W: E lands in Valid ⊆ W, and Ŝ reads its group coordinates
off W. On Valid the square `Φ —E→ Valid —Ŝ→ Ĝ` equals `Φ —(group coords)→ Ĝ`,
because the field bits E places are exactly the group coordinates Ŝ dualizes —
the codec names the very coordinates the transform acts on. Two enrichments make
this live: RTT-weighted routing is a Lawvere [0,∞]-enriched category on the peer
graph (objects = nodes, hom = measured latency, composition = triangle
inequality), and the redundancy/forwarding path is then a minimal morphism in the
free category on that graph — Dijkstra as choice of a shortest composite. The
frame quantum is the unit that travels those morphisms; the spectral document
governs *how* it rides the channel, this one governs *what it is* and *that every
SDK builds the same one*.

---

## §9. Artifacts and how to certify a new SDK

| file | role |
|---|---|
| `wirelab_core.py` | reference codec (encode/decode/σ), the affine maps of §1–§2 |
| `verify_laws.py` | executable Laws 0–6: anchors, affinity, retraction, quanta, certificate |
| `golden_vectors.json` | the 109 + 137 finite certificate of §6 (Theorem 4 proof object) |
| `wirelab_mcp.py` | MCP server: `crc16_ccitt`, `encode_frame`, `decode_frame`, `field_map`, `bitflip_audit`, `certify`, `golden_vectors` |
| `wirelab.lua` | DeMoD UI front panel; self-certifies the pure-Lua codec against the anchors on launch |
| `punctim-hotfix.lisp` | restores the Lisp codec to a cone (§7) and asserts the anchors |

**To certify a new or modified SDK (the §5 cone test, reduced to §6):**
1. Have the SDK emit, in golden order, its 109 encoded basis frames and 137
   single-bit syndromes (the same inputs `verify_laws.py` uses).
2. Run `certify` with that JSON (MCP tool, or diff against `golden_vectors.json`).
3. Zero mismatches ⇒ by Theorem 4 the SDK agrees with the reference on all 2¹⁰⁸
   frames and all 2¹³⁶ words ⇒ by Theorem 3 it is a faithful cone, canonically
   isomorphic to every other faithful SDK. That is the certificate of *cemented*.
Any mismatch points at the exact basis vector — i.e. the exact bit — that drifted,
the way the `0x29B1` anchor instantly localized the Punctim shadowing bug.
