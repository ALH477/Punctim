---
sd_hide_title: true
---

```{raw} html
<div class="dcf-hero">
  <p class="dcf-eyebrow">DeMoD Communication Framework</p>
  <h1 class="dcf-title">One 17-byte quantum.<br>Every language. <span>No handshake, no crypto.</span></h1>
  <p class="dcf-sub">A handshakeless, encryption-free, export-compliant mesh protocol — implemented
  byte-identically across C, Rust, Python, Lua, Haskell &amp; Lisp and pinned by a shared
  246-vector golden certificate. Everything on the wire is an adapter over one tiny frame.</p>
  <div class="dcf-cta">
    <a class="dcf-btn" href="WIRE_QUANTUM_SPEC.html">Read the wire spec →</a>
    <a class="dcf-btn ghost" href="_include/agent-to-agent.html">Agents over the mesh</a>
    <a class="dcf-btn ghost" href="https://github.com/ALH477/Punctim">GitHub</a>
  </div>
  <pre class="dcf-frame">sync(0xD3) │ flags[ver│type] │ seq │ src │ dst │ payload(4B) │ ts24 │ crc16</pre>
</div>
```

## Explore

````{grid} 1 1 2 3
:gutter: 3

```{grid-item-card} 🧩 The Wire Quantum
:link: WIRE_QUANTUM_SPEC
:link-type: doc
The 17-byte `DeModFrame` — the one invariant every adapter rides on. Sync byte,
version nibble, CRC-16/CCITT-FALSE.
```

```{grid-item-card} 🕸 Agent-to-Agent
:link: _include/agent-to-agent
:link-type: doc
Two AI agents talking directly over the mesh — no server, no human, no loops. A
full teaching guide.
```

```{grid-item-card} 🎧 DCF-Audio
:link: DCF_AUDIO_SPEC
:link-type: doc
Collaborative real-time audio as a certified adapter over the quantum.
```

```{grid-item-card} 🎮 DCF-Game
:link: DCF_GAME_SPEC
:link-type: doc
A certified low-latency multiplayer adapter over the same wire.
```

```{grid-item-card} 🎚 DCF-Snake
:link: DCF_SNAKE_SPEC
:link-type: doc
A synchronized studio audio snake over cat5e — quanta record + PCM cue planes to a mixer.
```

```{grid-item-card} 📦 SuperPack
:link: SUPERPACK_SPEC
:link-type: doc
Two 17-byte frames losslessly packed into 32 — without touching the certificate.
```

```{grid-item-card} 🏗 Architecture
:link: _include/architecture
:link-type: doc
How the polyglot monorepo fits together, layer by layer.
```
````

```{toctree}
:hidden:
:caption: Start here

_include/readme
_include/agent-to-agent
_include/architecture
```

```{toctree}
:hidden:
:caption: Specifications

WIRE_QUANTUM_SPEC
wire_quanta_category
DCF_AUDIO_SPEC
DCF_GAME_SPEC
DCF_SNAKE_SPEC
SUPERPACK_SPEC
DCF_FIELD_USE
```

```{toctree}
:hidden:
:caption: Project

DCF_CODE_REVIEW
_include/contributing
```
