# Copyright (C) 2026 DeMoD LLC
# SPDX-License-Identifier: LGPL-3.0-only
"""CLI for the Punctim LangGraph agent system.

Subcommands:
  agents    — list configured agents from agents.jsonc
  backends  — list available LLM backends
  chat      — one-shot: send a message, print the response
  run       — start the mesh poll loop (long-lived)
  status    — query mesh endpoint status via MCP
  tui       — launch the interactive TUI

Usage:
  dcf-agent chat --backend echo "hello mesh"
  dcf-agent run --config agents.jsonc
  dcf-agent status --mesh-url http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("dcf-agent")


# ── helpers ──────────────────────────────────────────────────────────────────

def _try_rich_print():
    """Return rich console if available, else None."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        from rich.markdown import Markdown
        return Console(), Table, Panel, Markdown
    except ImportError:
        return None, None, None, None


def _sierpinski(depth: int = 4, char: str = "▲") -> str:
    """Generate a Sierpinski triangle as a string.

    depth=1 → 1 triangle, depth=4 → 81 chars wide.
    Uses the classic substitution: each ▲ spawns ▲▲▲, gaps fill with spaces.
    """
    if depth < 1:
        depth = 1
    if depth > 7:
        depth = 7  # cap to avoid terminal explosion

    # Start with a single upward triangle
    triangle = [char]
    for _ in range(depth):
        # Each line: duplicate the current pattern (left + right with gap)
        spaces = " " * len(triangle[0])
        new_triangle = []
        for line in triangle:
            new_triangle.append(line + " " + line)
        for line in triangle:
            new_triangle.append(line + spaces + line)
        triangle = new_triangle

    # Pad each line to center the triangle
    width = len(triangle[-1])
    result = []
    for line in triangle:
        pad = (width - len(line)) // 2
        result.append(" " * pad + line)
    return "\n".join(result)


def _load_config(path: str) -> dict:
    """Load agents.jsonc (strips comments, allows trailing commas)."""
    import re
    raw = Path(path).read_text()
    text = re.sub(r'(?m)^\s*//.*$', '', raw)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return json.loads(text)


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_agents(args: argparse.Namespace) -> int:
    """List configured agents from the JSONC config."""
    config = _load_config(args.config)
    agents = config.get("agents", [])

    console, Table, *_ = _try_rich_print()
    if console and Table:
        table = Table(title="Configured Agents")
        table.add_column("Name", style="cyan")
        table.add_column("Graph", style="green")
        table.add_column("Backend", style="yellow")
        table.add_column("Channel", style="magenta")
        table.add_column("Mesh URL", style="dim")
        for a in agents:
            table.add_row(
                a.get("name", "?"),
                a.get("graph", "base"),
                a.get("llm_backend", "echo"),
                a.get("channel", "agent"),
                a.get("mesh_url", "(none)"),
            )
        console.print(table)
    else:
        print(f"{'Name':<20} {'Graph':<14} {'Backend':<10} {'Channel':<10} {'Mesh URL'}")
        print("-" * 70)
        for a in agents:
            print(f"{a.get('name','?'):<20} {a.get('graph','base'):<14} "
                  f"{a.get('llm_backend','echo'):<10} {a.get('channel','agent'):<10} "
                  f"{a.get('mesh_url','(none)')}")

    mesh_srv = config.get("mesh_server")
    if mesh_srv:
        print(f"\nMesh server: {mesh_srv.get('command','?')} {' '.join(mesh_srv.get('args',[]))}")
    return 0


def cmd_backends(args: argparse.Namespace) -> int:
    """List available LLM backends."""
    from langgraph_agents.llm_node import BACKENDS

    console, Table, *_ = _try_rich_print()
    if console and Table:
        table = Table(title="Available LLM Backends")
        table.add_column("Name", style="cyan")
        table.add_column("Function", style="dim")
        for name, fn in sorted(BACKENDS.items()):
            table.add_row(name, f"{fn.__module__}.{fn.__name__}")
        console.print(table)
    else:
        print("Available LLM backends:")
        for name, fn in sorted(BACKENDS.items()):
            print(f"  {name:<12} ({fn.__module__}.{fn.__name__})")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """One-shot: send a message through a graph, print the response."""
    from langgraph_agents.agent_graph import build_agent_graph
    from langgraph_agents.coordinator import build_coordinator_graph

    if args.graph == "coordinator":
        graph = build_coordinator_graph()
    else:
        graph = build_agent_graph(llm_backend=args.backend)

    result = graph.invoke({
        "messages": [{"role": "user", "content": args.message}],
        "channel": args.channel,
        "sender": args.sender,
        "routing_decision": "",
        "response": "",
        "metadata": {},
    })

    response = result.get("response", "")
    console, _, Panel, _ = _try_rich_print()
    if console and Panel:
        console.print(Panel(response, title=f"[{args.backend}] Response", border_style="green"))
    else:
        print(response)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Start the mesh poll loop for all configured agents."""
    from langgraph_agents.__main__ import main as run_main
    asyncio.run(run_main(args.config))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Query mesh endpoint status via MCP."""
    import httpx

    console, _, Panel, _ = _try_rich_print()
    try:
        resp = httpx.post(
            f"{args.mesh_url}/tools/mesh_status",
            json={},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json().get("result", {})
    except Exception as e:
        msg = f"[red]Mesh status failed:[/red] {e}" if console else f"Mesh status failed: {e}"
        if console:
            console.print(msg)
        else:
            print(msg)
        return 1

    if console and Panel:
        console.print(Panel(
            json.dumps(data, indent=2),
            title=f"Mesh Status — {args.mesh_url}",
            border_style="cyan",
        ))
    else:
        print(json.dumps(data, indent=2))
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    """Launch the interactive TUI."""
    from langgraph_agents.tui import AgentTUI
    app = AgentTUI(config_path=args.config, mesh_url=args.mesh_url)
    app.run()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the HTTP API server."""
    from langgraph_agents.api_server import run_server
    run_server(host=args.host, port=args.port, config_path=args.config)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Start the MCP server (stdio transport)."""
    import asyncio
    from langgraph_agents.mcp_server import run_stdio
    asyncio.run(run_stdio(config_path=args.config))
    return 0


# ── entry point ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dcf-agent",
        description="Punctim LangGraph agent CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # agents
    p = sub.add_parser("agents", help="List configured agents")
    p.add_argument("--config", default="agents.jsonc", help="Path to agents.jsonc")
    p.set_defaults(func=cmd_agents)

    # backends
    p = sub.add_parser("backends", help="List available LLM backends")
    p.set_defaults(func=cmd_backends)

    # chat
    p = sub.add_parser("chat", help="One-shot: send a message, get response")
    p.add_argument("message", help="Message text to send")
    p.add_argument("--backend", default="echo", help="LLM backend (echo, grok, glm5p2)")
    p.add_argument("--graph", default="base", choices=["base", "coordinator"],
                    help="Graph type (base or coordinator)")
    p.add_argument("--channel", default="cli", help="DCF channel name")
    p.add_argument("--sender", default="cli-user", help="Sender identity")
    p.set_defaults(func=cmd_chat)

    # run
    p = sub.add_parser("run", help="Start the mesh poll loop")
    p.add_argument("--config", default="agents.jsonc", help="Path to agents.jsonc")
    p.set_defaults(func=cmd_run)

    # status
    p = sub.add_parser("status", help="Query mesh endpoint status")
    p.add_argument("--mesh-url", default="http://127.0.0.1:8765",
                    help="Mesh MCP server URL")
    p.set_defaults(func=cmd_status)

    # tui
    p = sub.add_parser("tui", help="Launch interactive TUI")
    p.add_argument("--config", default="agents.jsonc", help="Path to agents.jsonc")
    p.add_argument("--mesh-url", default="http://127.0.0.1:8765",
                    help="Mesh MCP server URL")
    p.set_defaults(func=cmd_tui)

    # serve (HTTP API server)
    p = sub.add_parser("serve", help="Start the HTTP API server")
    p.add_argument("--host", default="0.0.0.0", help="Bind address")
    p.add_argument("--port", type=int, default=8000, help="Port")
    p.add_argument("--config", default="agents.jsonc", help="Path to agents.jsonc")
    p.set_defaults(func=cmd_serve)

    # mcp (MCP server over stdio)
    p = sub.add_parser("mcp", help="Start the MCP server (stdio transport)")
    p.add_argument("--config", default="agents.jsonc", help="Path to agents.jsonc")
    p.set_defaults(func=cmd_mcp)

    return parser


def _print_banner() -> None:
    """Print the Sierpinski greeting banner."""
    tri = _sierpinski(depth=3, char="▲")
    console, _, Panel, _ = _try_rich_print()
    if console and Panel:
        console.print(Panel(
            f"[cyan]{tri}[/cyan]\n[dim]Punctim LangGraph Agent System v0.1.0[/dim]",
            border_style="cyan",
            padding=(0, 2),
        ))
    else:
        print(tri)
        print("Punctim LangGraph Agent System v0.1.0")
    print()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    _print_banner()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
