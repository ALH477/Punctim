# DCF ↔ Matrix bridge — chat with an LLM agent over the DeModFrame wire quantum

Set up so your **phone** (running [Element](https://element.io), a Matrix client)
talks to an **LLM agent** on your **laptop**, with every message crossing a DCF
mesh as the 17-byte `DeModFrame` quantum — over a VPN that gives the two devices
stable, private addresses.

```
 ┌─────────────┐        VPN (WireGuard / Tailscale — provides the encryption)
 │   Phone     │  ───────────────────────────────────────────────────────────┐
 │  Element    │                                                              │
 └─────────────┘                                                              ▼
                                                              ┌──────────────────────────┐
                                                              │         Laptop           │
   m.room.message  ── CS API ──►  Matrix homeserver  ──AS──►  │  bridge.py               │
        ▲                          (Synapse)                  │    └ DcfTextNode (UDP)    │
        │                                                     └──────────┬───────────────┘
        │                                                                │ 17-byte DeModFrame
        │                                                                │ DATA frames (SuperPacked)
        │                                                                ▼
        └──── CS API send ◄── bridge on_message ◄──── UDP mesh ──── mesh_mcp.py (the agent)
                                                                     └ LLM agent via MCP tools
```

The agent never speaks Matrix; it only speaks `DeModFrame` through the MCP tools.
The wire quantum is the entire contract between the chat world and the agent.

> **Agent-to-agent:** the same `mesh_mcp.py` also lets **two agents talk to each
> other** over DeModFrame (no Matrix, no human) — point two instances at each
> other on a shared channel. See [`AGENT_TO_AGENT.md`](AGENT_TO_AGENT.md)
> (`a2a.mcp.example.json` for config, `a2a_runner.py` for a hands-off demo, or
> `a2a_interactive.py` for a guided setup-and-run test). To *see* the mesh live,
> run `mesh_viz.py` (or `nix run …#mesh-viz`) — a web dashboard of the agents,
> channels, and message flow.

## What's in here

| File | What it is | Deps |
|------|-----------|------|
| `dcf_text.py` | Text adapter over `DeModFrame` (chat → DATA frames). Mirrors the certified DCF-Audio L2 scheme. | stdlib |
| `../python/MCP/superpack.py` | **SuperPack**: 32-byte container for two 17-byte frames (lossless, joint-CRC). | stdlib |
| `dcf_node.py` | UDP endpoint: send/recv text as frames, optional SuperPack batching. | stdlib |
| `bridge.py` | Matrix **application service** ↔ mesh. | `aiohttp` |
| `mesh_mcp.py` | **MCP server** the LLM agent connects to (`mesh_inbox`, `mesh_send`, …). | `mcp` |
| `a2a.py` | Umbrella CLI: `config` (per-agent MCP configs), `send`/`recv` (non-MCP bridge), delegators. | stdlib |
| `a2a_config.py` | Multi-agent config generator (Claude/OpenClaw/OpenCode/Goose/Cursor/Cline/Continue). See `../Documentation/AGENT_CLIENTS.md`. | stdlib |
| `a2a_cli.py` | `a2a send` / `a2a recv` over `DcfTextNode` — join the mesh without MCP (Aider, scripts). | stdlib |
| `a2a_endpoint.py` | HTTP/SSE transport (host/port/mode) resolution for `mesh_mcp.py`. | stdlib |
| `agents/openclaw/` | OpenClaw ClawHub skill (`dcf-mesh/`) + native-channel design note. | — |
| `tests/` | Wire-level certification (text adapter, SuperPack, UDP loopback, CLI/config). | `pytest` |

The wire code is pure stdlib — only the bridge and the MCP server pull deps:

```sh
pip install -r requirements.txt
python3 dcf_text.py              # adapter self-test
python3 ../python/MCP/superpack.py
python3 dcf_node.py              # UDP loopback self-test
python3 -m pytest tests/ -q
```

## Setup

### 1. VPN — give laptop + phone private addresses (and the encryption)

DCF is **encryption-free by design** (EAR/ITAR export compliance — see
`Documentation/Specs/export_compliance.markdown`). Confidentiality is the
underlay's job, which is exactly right here: the VPN tunnel encrypts everything,
and plaintext `DeModFrame`s ride safely inside it.

Easiest is **Tailscale** (install on laptop and phone, same tailnet) — each gets a
`100.x.y.z` address. Or **WireGuard** with a manual config. Note the laptop's VPN
IP; call it `LAPTOP_VPN_IP`.

### 2. Matrix homeserver on the laptop

Run [Synapse](https://element-hq.github.io/synapse/) bound to the VPN interface.
Quick Docker route:

```sh
docker run -it --rm -v $PWD/synapse:/data \
  -e SYNAPSE_SERVER_NAME=home.lan -e SYNAPSE_REPORT_STATS=no \
  matrixdotorg/synapse:latest generate
# edit synapse/homeserver.yaml: listeners bind to 0.0.0.0:8008 (reachable on the VPN)
docker run -d --name synapse -v $PWD/synapse:/data -p 8008:8008 matrixdotorg/synapse:latest
```

Create your human account and a room, then note the **room ID** (`!abc:home.lan`,
visible in Element under room settings):

```sh
docker exec -it synapse register_new_matrix_user -c /data/homeserver.yaml http://localhost:8008
```

### 3. Register the appservice

```sh
cp registration.example.yaml synapse/dcf-registration.yaml
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # do this twice
```

Put the two tokens into `dcf-registration.yaml` (`as_token`, `hs_token`), add to
`homeserver.yaml`:

```yaml
app_service_config_files:
  - /data/dcf-registration.yaml
```

Restart Synapse. **Invite `@dcf_bot:home.lan` to your room** so the bridge can post.

### 4. Configure and run the bridge

```sh
cp config.example.json config.json     # set homeserver_url=http://LAPTOP_VPN_IP:8008,
                                        # the same tokens, bot_user_id, room_id
python3 bridge.py config.json
```

The bridge listens for appservice transactions on `:9000` and runs the mesh node
on UDP `:7777`, with the agent at `127.0.0.1:7788` as its peer.

### 5. Run the LLM agent (MCP)

The agent is any MCP-capable client (e.g. Claude Code / Claude Desktop) pointed at
`mesh_mcp.py`. Stdio config:

```json
{ "mcpServers": {
    "dcf-mesh": { "command": "python3",
                  "args": ["/home/you/Punctim/matrix-bridge/mesh_mcp.py"],
                  "env": { "DCF_BRIDGE_PORT": "7777", "DCF_AGENT_UDP_PORT": "7788",
                           "DCF_CHANNEL": "agent" } } } }
```

Then tell the agent to loop: **call `mesh_inbox`; if there's a message, answer it
with `mesh_send`.** That's the whole agent — read mesh, think, reply on mesh.
`mesh_status` and `superpack_explain` are there for diagnostics.

### 6. Phone

Install Element, sign in to `http://LAPTOP_VPN_IP:8008` with your human account,
open the room, and type. Your message goes phone → Synapse → bridge → mesh →
agent; the reply comes back the same path and shows up as `@dcf_bot`.

## Wire details

A chat message becomes `DeModFrame` **DATA** frames (type 0), fragmented like
DCF-Audio but with a 10-bit fragment index (up to 4092 bytes/message). The
fragment bookkeeping lives in `seq = packet_id[15:10] | frag_idx[9:0]`; `frag_idx
0` is a descriptor carrying the UTF-8 length. `dst` is the rendezvous channel
(`crc16(passphrase)`), matching the repo's frequency-channel convention.

**SuperPack** halves header overhead when frames go out in pairs: two 17-byte
frames → one 32-byte container (saves 2 B/pair *and* upgrades to a joint CRC over
both quanta). It's lossless — each inner frame is reconstructed bit-exact and
still passes the full quantum check — so the 246-vector wire certificate is
untouched. Spec: [`../Documentation/SUPERPACK_SPEC.md`](../Documentation/SUPERPACK_SPEC.md).

## Notes / limits

- The bridge and agent both speak this DATA text adapter; the Rust SDK's generic
  `send_message` uses `ProtoMessage`, not this adapter, so cross-talk with a raw
  Rust node would need a matching DATA adapter there (follow-up work).
- DCF is connectionless/unreliable UDP — fine on a LAN/VPN; lossy links may drop
  fragments (a message just won't complete). Add ACK/retransmit if you need it.
- No encryption in DCF on purpose — **always run this inside the VPN.**
