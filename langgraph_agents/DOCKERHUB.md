# dcf-agent

LangGraph multi-agent LLM system for the Punctim DCF framework.

![License: LGPL v3](https://img.shields.io/badge/License-LGPLv3-blue.svg)
![Encryption-free](https://img.shields.io/badge/Encryption-EAR%2FITAR%20compliant-green)

## What this is

A Docker image that runs LLM-powered agents communicating over the Punctim DCF mesh in realtime using the Model Context Protocol (MCP). Built with LangGraph state graphs, pluggable LLM backends, and a universal API system.

**Encryption-free for export control purposes** — agents communicate over the same plaintext DCF transport. No cryptographic layers bundled.

## Quick start

```bash
# Start the HTTP API server (default)
docker run -p 8000:8000 alh477/dcf-agent

# List available LLM backends
docker run alh477/dcf-agent backends

# One-shot chat with echo backend
docker run alh477/dcf-agent chat "hello mesh" --backend echo

# Interactive TUI
docker run -it alh477/dcf-agent tui

# MCP server (stdio transport)
docker run -i alh477/dcf-agent mcp
```

## Subcommands

| Command | Description |
|---------|-------------|
| `serve` | HTTP API server (default, port 8000) |
| `tui` | Interactive Textual TUI |
| `mcp` | MCP server over stdio |
| `chat` | One-shot message → response |
| `agents` | List configured agents |
| `backends` | List available LLM backends |
| `status` | Query mesh endpoint status |

## HTTP API endpoints

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

### Chat API example

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "hello", "backend": "echo", "graph": "base"}'
```

## LLM backends

| Backend | Provider | Model |
|---------|----------|-------|
| `echo` | (local) | Mirrors input — for testing |
| `grok` | xAI | grok-beta |
| `glm5p2` | Fireworks | accounts/fireworks/models/glm-5p2 |

### Universal provider presets

Any OpenAI-compatible API can be registered at runtime via config:

| Provider | Default model | API key env |
|----------|---------------|-------------|
| `fireworks` | glm-5p2 | `FIREWORKS_API_KEY` |
| `openai` | gpt-4o | `OPENAI_API_KEY` |
| `anthropic` | claude-sonnet-4 | `ANTHROPIC_API_KEY` |
| `grok` | grok-beta | `XAI_API_KEY` |
| `ollama` | llama3 | (none — local) |
| `deepseek` | deepseek-chat | `DEEPSEEK_API_KEY` |
| `openrouter` | openai/gpt-4o | `OPENROUTER_API_KEY` |

Pass API keys via environment variables:

```bash
docker run -p 8000:8000 -e FIREWORKS_API_KEY=fw_... alh477/dcf-agent
```

## MCP tools

Any MCP client (Hermes, Claude, etc.) can connect to the stdio MCP server:

| Tool | Description |
|------|-------------|
| `agent_chat` | Send message to agent, get response |
| `agent_list` | List configured agents |
| `backend_list` | List LLM backends + providers |
| `provider_list` | List provider presets |
| `mesh_send` | Send text onto DCF mesh |
| `mesh_recv` | Receive messages from DCF mesh |
| `mesh_status` | Query mesh endpoint status |

## Docker Compose

```yaml
services:
  dcf-agent:
    image: alh477/dcf-agent:latest
    ports:
      - "8000:8000"
    environment:
      - FIREWORKS_API_KEY=fw_...
      # - OPENAI_API_KEY=sk-...
      # - XAI_API_KEY=...
```

## Architecture

```
mesh_recv → receive → route → process (LLM backend) → respond → mesh_send
                                                      ↑
                                         coordinator routes by prefix:
                                         echo:       → echo specialist
                                         summarize:  → summarizer specialist
                                         (default)   → echo
```

- **LangGraph state graphs**: receive → route → process → respond
- **Pluggable LLM backends**: echo, grok, glm5p2, or any OpenAI-compatible API
- **MCP tool wrappers**: mesh_send, mesh_recv, mesh_status as LangChain tools
- **Streaming bridge**: UTF-8-safe chunking for DCF-Text frame limits
- **Coordinator graph**: prefix-based routing to specialist subgraphs

## Export compliance

Encryption-free by design. Safe for EAR/ITAR-regulated environments where cryptographic layers are restricted. Any security layer is external and connected through IPC barriers.

## Source

- **Repo**: [https://github.com/ALH477/Punctim](https://github.com/ALH477/Punctim)
- **Agent source**: `langgraph_agents/` directory
- **License**: LGPL-3.0-only
- **Built with**: Nix + DockerTools (hermetic, reproducible)

## Related images

- [`alh477/punctim`](https://hub.docker.com/r/alh477/punctim) — Lisp SDK node with agent DSL integration
- [`alh477/dcf-go`](https://hub.docker.com/r/alh477/dcf-go) — Go mesh node
- [`alh477/dcf-rs`](https://hub.docker.com/r/alh477/dcf-rs) — Rust mesh node
- [`alh477/dcf-python`](https://hub.docker.com/r/alh477/dcf-python) — Python mesh node
- [`alh477/dcf-c`](https://hub.docker.com/r/alh477/dcf-c) — C mesh node

---

Developed by [DeMoD LLC](https://demod.ltd) · Contact: alh477@demod.ltd
