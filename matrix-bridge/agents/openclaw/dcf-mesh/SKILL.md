---
name: dcf-mesh
description: Talk to other AI agents directly over the DeMoD Communication Framework (DCF) mesh — a handshakeless, encryption-free 17-byte wire protocol. No Matrix server, no human relay; agents converse over UDP/VPN.
homepage: https://github.com/ALH477/Punctim
license: LGPL-3.0
---

# DCF mesh — agent-to-agent over the wire

This skill is an **MCP server** (`matrix-bridge/mesh_mcp.py`) that puts your OpenClaw
agent on a **DeModFrame mesh**, where it can talk **directly to another MCP-capable
agent** (Claude Code, OpenCode, another OpenClaw, …). Every message crosses the wire as
the same certified 17-byte `DeModFrame` the rest of Punctim is built on — there is no
new wire format, no encryption (confidentiality is your VPN's job), and no central server.

## Mental model: "standing agents, talk on demand, no loops"

You do **not** poll. You **block** in `mesh_recv` — parked, costing nothing — until your
peer actually sends. When woken, you think, reply with `mesh_send`, and block again. That's
the whole philosophy: check exactly when there is something to check.

```
 You (OpenClaw)                                   Peer agent
  └ mesh_mcp.py ── DeModFrame DATA frames / UDP[/VPN] ── mesh_mcp.py ┘
     same DCF_CHANNEL  ◀──────── rendezvous ────────▶  distinct node ids
```

## Tools

| Tool | What it does |
|------|--------------|
| `mesh_status()` | node id, name, channel, peers, UDP port — call first to confirm the link is wired. |
| `mesh_send(text)` | fragment `text` and unicast it to the peer(s). |
| `mesh_recv(timeout_s=30)` | **block** until a message arrives, then drain extras. The ping-pong primitive. |
| `mesh_inbox(max_messages)` | non-blocking drain of anything already queued. |
| `superpack_explain(a, b)` | diagnostic: show two frames packed into one 32-byte SuperPack. |

## Recipe

1. **Confirm the link.** Call `mesh_status` and check your peer appears under `peers` and you
   share the same `channel`.
2. **Turn-taking (important).** `mesh_recv` blocks, so if *both* sides start by receiving they
   deadlock. Exactly one agent is the **initiator** and sends first:
   - *Initiator:* `mesh_send("…opening message…")`, then loop `mesh_recv → think → mesh_send`.
   - *Responder:* start by `mesh_recv`, then loop `think → mesh_send → mesh_recv`.
3. **Converse.** Keep each turn focused. Messages cap at 4092 bytes — chunk longer output across
   several `mesh_send` calls.

## Channels are a rendezvous, not a connection

There is no "connect". Two agents "meet" by agreeing on a channel *string* (e.g. `duet`), which
both hash to the same 16-bit `dst`. Give each agent a **distinct** node id (`DCF_AGENT_NODE_ID`)
so you can tell who said what. See `matrix-bridge/AGENT_TO_AGENT.md` for the full design.

## Install

Drop the bundled `mcporter.json` into `~/.openclaw/workspace/config/mcporter.json` (merge the
`mcpServers` entry), or regenerate it for your machine:

```sh
python3 matrix-bridge/a2a.py config --agent openclaw                    # stdio (own identity)
python3 matrix-bridge/a2a.py config --agent openclaw --transport http   # shared mesh-http service
```
