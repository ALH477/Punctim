# LangGraph Multi-Agent System

LLM-powered agents that communicate over the Punctim DCF mesh in realtime using the Model Context Protocol (MCP).

![License: LGPL v3](https://img.shields.io/badge/License-LGPLv3-blue.svg)
![Encryption-free](https://img.shields.io/badge/Encryption-EAR%2FITAR%20compliant-green)

## Overview

Agents are built as LangGraph state graphs (receive → route → process → respond) with pluggable LLM backends, MCP tool wrappers, a universal HTTP API server, and an MCP server. The system includes a Rich-powered CLI and Textual TUI with a Sierpinski triangle greeting banner.

## Export control

**Encryption-free** for export control purposes. Agents communicate over the same plaintext DCF transport — there is no separate encrypted channel. This keeps the system within EAR/ITAR export compliance boundaries. Any security layer is external to this package and connected through IPC barriers.

## Architecture

```
mesh_recv → receive → route → process (LLM backend) → respond → mesh_send
                                                      ↑
                                         coordinator routes by prefix:
                                         echo:       → echo specialist
                                         summarize:  → summarizer specialist
                                         (default)   → echo
```

- **State graph**: LangGraph `StateGraph` with `AgentState` (messages, channel, sender, routing_decision, response, metadata).
- **LLM backends**: `echo` (test), `grok` (xAI), `glm5p2` (Fireworks GLM-5p2). Any OpenAI-compatible endpoint can be added.
- **Universal backend**: `UniversalBackend` class — one interface for any OpenAI-compatible API. 7 provider presets: Fireworks, OpenAI, Anthropic, Grok, Ollama, DeepSeek, OpenRouter.
- **MCP tools**: `MeshSendTool`, `MeshRecvTool`, `MeshStatusTool` — LangChain tool wrappers that call the MCP server over HTTP.
- **Streaming bridge**: chunks LLM responses to fit DCF-Text's per-message cap with UTF-8 boundary safety (never splits mid-character).
- **Coordinator graph**: prefix-based routing to specialist subgraphs. Add new specialists by registering a process function.

## CLI

```bash
# List configured agents
dcf-agent agents --config agents.jsonc

# List available LLM backends
dcf-agent backends

# One-shot: send a message, get response
dcf-agent chat --backend echo "hello mesh"
dcf-agent chat --graph coordinator "echo: test routing"

# Start the mesh poll loop (long-lived)
dcf-agent run --config agents.jsonc

# Query mesh endpoint status
dcf-agent status --mesh-url http://127.0.0.1:8765

# Start the HTTP API server
dcf-agent serve --host 0.0.0.0 --port 8000

# Start the MCP server (stdio)
dcf-agent mcp
```

Every CLI command prints a Sierpinski triangle greeting banner.

## TUI

```bash
dcf-agent tui --config agents.jsonc
```

Interactive Textual-based TUI with:
- Agent config sidebar (left)
- Message log (main panel, scrolling)
- Input bar — type a message, press Enter to process
- `Ctrl+B` — cycle LLM backend
- `Ctrl+G` — cycle graph type (base / coordinator)
- `Ctrl+S` — query mesh status
- `Ctrl+T` — toggle Sierpinski triangle display
- `Ctrl+C` — quit

## HTTP API server

```bash
dcf-agent serve --port 8000
```

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/agents` | List configured agents |
| GET | `/backends` | List LLM backends + providers |
| GET | `/providers` | List provider presets |
| POST | `/chat` | One-shot chat (JSON body) |
| POST | `/chat/stream` | SSE chunked response |
| POST | `/mesh/send` | Proxy mesh_send |
| POST | `/mesh/recv` | Proxy mesh_recv |
| GET | `/mesh/status` | Proxy mesh_status |
| WS | `/ws` | WebSocket chat |

## MCP server

```bash
dcf-agent mcp
```

Exposes 7 MCP tools over stdio for any MCP client (Hermes, Claude, etc.):

| Tool | Description |
|------|-------------|
| `agent_chat` | Send message to agent, get response |
| `agent_list` | List configured agents |
| `backend_list` | List LLM backends + providers |
| `provider_list` | List provider presets |
| `mesh_send` | Send text onto DCF mesh |
| `mesh_recv` | Receive messages from DCF mesh |
| `mesh_status` | Query mesh endpoint status |

## Universal LLM backends

| Backend | Provider | Model |
|---------|----------|-------|
| `echo` | (local) | Mirrors input — for testing |
| `grok` | xAI | grok-beta |
| `glm5p2` | Fireworks | accounts/fireworks/models/glm-5p2 |

### Provider presets

| Provider | Default model | API key env |
|----------|---------------|-------------|
| `fireworks` | glm-5p2 | `FIREWORKS_API_KEY` |
| `openai` | gpt-4o | `OPENAI_API_KEY` |
| `anthropic` | claude-sonnet-4 | `ANTHROPIC_API_KEY` |
| `grok` | grok-beta | `XAI_API_KEY` |
| `ollama` | llama3 | (none — local) |
| `deepseek` | deepseek-chat | `DEEPSEEK_API_KEY` |
| `openrouter` | openai/gpt-4o | `OPENROUTER_API_KEY` |

Register custom providers at runtime via `agents.jsonc`:

```jsonc
"providers": [
  {"name": "my-backend", "provider": "fireworks", "model": "accounts/fireworks/models/glm-5p2"},
  {"name": "local-llama", "provider": "ollama", "model": "llama3"}
]
```

## Nix integration

```bash
# Run the CLI, TUI, API server, or MCP server directly
nix run .#agent -- backends
nix run .#agent-tui
nix run .#agent-serve
nix run .#agent-mcp

# Dev shell with everything on PATH
nix develop .#agents
dcf-agent backends
dcf-agent tui
dcf-agent serve
dcf-agent mcp

# Docker
docker run -p 8000:8000 alh477/dcf-agent
```

## Configuration (`agents.jsonc`)

```jsonc
{
  "agents": [
    {
      "name": "echo-agent",
      "graph": "base",
      "llm_backend": "echo",
      "channel": "echo",
      "mesh_url": "http://127.0.0.1:8765"
    },
    {
      "name": "glm5p2-agent",
      "graph": "coordinator",
      "llm_backend": "glm5p2",
      "channel": "glm",
      "mesh_url": "http://127.0.0.1:8765"
    }
  ],
  "providers": [
    {"name": "fireworks-glm5p2", "provider": "fireworks", "model": "accounts/fireworks/models/glm-5p2"},
    {"name": "ollama-local", "provider": "ollama", "model": "llama3"}
  ]
}
```

## Lisp DSL integration

The Lisp SDK (`lisp/src/punctim.lisp`) has native agent functions that call the API server over HTTP. No external HTTP library needed — the implementation uses usocket + flexi-streams (already in the dependency closure).

```lisp
(dcf-agent-health)                         ;; check API server
(dcf-agent-backends)                       ;; list LLM backends
(dcf-agent-providers)                      ;; list provider presets
(dcf-agent-chat "hello" :backend "echo")   ;; one-shot chat
(dcf-agent-chat "echo: test" :graph "coordinator")
(dcf-agent-chat "hello" :backend "glm5p2") ;; GLM-5p2 via Fireworks
(dcf-agent-set-url "http://192.168.1.50:8000")  ;; point at remote API
```

CLI subcommands:
```bash
punctim agent-health
punctim agent-backends
punctim agent-providers
punctim agent-chat "hello"
```

## Use cases

- **Disconnected/edge deployments**: agents communicate over UDP, acoustic, SDR, or any DCF transport — no internet required.
- **Multi-agent coordination**: a coordinator routes messages to specialists. Add translators, analysts, coders — they plug into the graph.
- **Pluggable LLM backends**: echo for testing, Grok via xAI, GLM-5p2 via Fireworks. Any OpenAI-compatible endpoint works.
- **Realtime mesh integration**: agents poll the mesh via MCP, process through a LangGraph state machine, and respond back. The streaming bridge chunks responses to fit DCF-Text's frame limit.
- **Export-compliant**: encryption-free by design. Safe for EAR/ITAR-regulated environments where cryptographic layers are restricted.

## Tests

```bash
cd langgraph_agents && pytest -v    # 60 tests
```

CI: the `langgraph-agents` job in `.github/workflows/ci.yml` runs the full test suite on Python 3.13 for every push/PR to `main`.

## Docker

```bash
docker run -p 8000:8000 alh477/dcf-agent              # HTTP API server
docker run -it alh477/dcf-agent tui                    # interactive TUI
docker run alh477/dcf-agent backends                   # list LLM backends
docker run alh477/dcf-agent chat "hello" --backend echo
docker run -i alh477/dcf-agent mcp                     # MCP server (stdio)
```

See [`DOCKERHUB.md`](DOCKERHUB.md) for the full DockerHub README.
