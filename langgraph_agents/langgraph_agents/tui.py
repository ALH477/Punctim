# Copyright (C) 2026 DeMoD LLC
# SPDX-License-Identifier: LGPL-3.0-only
"""Interactive TUI for the Punctim LangGraph agent system.

Built with Textual. Features:
  - Agent config panel (left sidebar)
  - Message log (main panel, scrolling)
  - Input bar (bottom) — type a message, press Enter to process
  - Backend selector — cycle through available LLM backends
  - Graph selector — switch between base and coordinator graphs
  - Status bar — shows current backend, graph, channel

Controls:
  Enter       Send message
  Tab         Cycle focus
  Ctrl+B      Cycle backend
  Ctrl+G      Cycle graph type
  Ctrl+S      Query mesh status
  Ctrl+C      Quit
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)
from textual.reactive import reactive


def sierpinski(depth: int = 3, char: str = "▲") -> str:
    """Generate a small Sierpinski triangle for the TUI banner."""
    if depth < 1:
        depth = 1
    triangle = [char]
    for _ in range(depth):
        spaces = " " * len(triangle[0])
        new_triangle = []
        for line in triangle:
            new_triangle.append(line + " " + line)
        for line in triangle:
            new_triangle.append(line + spaces + line)
        triangle = new_triangle
    width = len(triangle[-1])
    result = []
    for line in triangle:
        pad = (width - len(line)) // 2
        result.append(" " * pad + line)
    return "\n".join(result)


class AgentSidebar(Static):
    """Sidebar showing configured agents."""

    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.config_path = config_path

    def compose(self) -> ComposeResult:
        try:
            raw = Path(self.config_path).read_text()
            text = re.sub(r'(?m)^\s*//.*$', '', raw)
            text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
            text = re.sub(r',\s*([}\]])', r'\1', text)
            config = json.loads(text)
        except Exception:
            yield Label("[red]Failed to load config[/red]")
            return

        yield Label("[bold cyan]Agents[/bold cyan]")
        for agent in config.get("agents", []):
            name = agent.get("name", "?")
            backend = agent.get("llm_backend", "echo")
            channel = agent.get("channel", "agent")
            graph_type = agent.get("graph", "base")
            yield Label(
                f"  [green]{name}[/green]\n"
                f"  [dim]graph={graph_type} backend={backend}\n"
                f"  channel={channel}[/dim]\n"
            )

        mesh_srv = config.get("mesh_server")
        if mesh_srv:
            yield Label("[bold magenta]Mesh Server[/bold magenta]")
            yield Label(f"  [dim]{mesh_srv.get('command','?')}[/dim]\n")


class StatusBar(Static):
    """Bottom status bar showing current TUI state."""

    backend = reactive("echo")
    graph_type = reactive("base")
    channel = reactive("cli")

    def render(self) -> str:
        return (
            f" backend=[yellow]{self.backend}[/yellow] "
            f"graph=[green]{self.graph_type}[/green] "
            f"channel=[magenta]{self.channel}[/magenta] "
        )


class AgentTUI(App):
    """Main TUI application."""

    CSS = """
    Screen {
        layout: horizontal;
    }
    #sidebar {
        width: 32;
        dock: left;
        padding: 0 1;
        border-right: solid $primary;
        background: $surface;
        overflow-y: auto;
    }
    #main {
        width: 1fr;
    }
    #msg-log {
        height: 1fr;
        border: solid $accent;
        padding: 0 1;
    }
    #input-bar {
        height: 3;
        dock: bottom;
        padding: 0 1;
    }
    #input-bar Input {
        width: 1fr;
    }
    #status {
        height: 1;
        dock: bottom;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+b", "cycle_backend", "Backend"),
        Binding("ctrl+g", "cycle_graph", "Graph"),
        Binding("ctrl+s", "mesh_status", "Status"),
        Binding("ctrl+t", "toggle_sierpinski", "Sierpinski"),
    ]

    show_banner = reactive(True)

    backends_list: list[str] = []
    backends_idx: int = 0
    graphs_list: list[str] = ["base", "coordinator"]
    graphs_idx: int = 0

    def __init__(self, config_path: str = "agents.jsonc", mesh_url: str = "http://127.0.0.1:8765") -> None:
        super().__init__()
        self.config_path = config_path
        self.mesh_url = mesh_url

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield AgentSidebar(self.config_path)
        with Vertical(id="main"):
            yield RichLog(id="msg-log", wrap=True, markup=True)
            with Horizontal(id="input-bar"):
                yield Input(placeholder="Type a message and press Enter...", id="msg-input")
            yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Punctim Agent TUI"
        self.sub_title = f"{self.config_path}"

        # Discover available backends
        try:
            from langgraph_agents.llm_node import BACKENDS
            self.backends_list = sorted(BACKENDS.keys())
        except ImportError:
            self.backends_list = ["echo"]

        status = self.query_one("#status", StatusBar)
        status.backend = self.backends_list[self.backends_idx]
        status.graph_type = self.graphs_list[self.graphs_idx]

        # Sierpinski banner
        if self.show_banner:
            tri = sierpinski(depth=3)
            for line in tri.split("\n"):
                self._log_raw(f"[cyan]{line}[/cyan]")
            self._log("system", "Punctim LangGraph Agent TUI — ready.")

        self._log("system", f"Backends: {', '.join(self.backends_list)}")
        self._log("system", f"Mesh URL: {self.mesh_url}")
        self._log("system", "Ctrl+B backend | Ctrl+G graph | Ctrl+S status | Ctrl+T sierpinski | Ctrl+C quit")

        # Focus the input
        self.query_one("#msg-input", Input).focus()

    def _log_raw(self, text: str) -> None:
        """Write raw markup to the message log without a tag prefix."""
        log = self.query_one("#msg-log", RichLog)
        log.write(text)

    def _log(self, tag: str, text: str) -> None:
        """Write a tagged line to the message log."""
        log = self.query_one("#msg-log", RichLog)
        colors = {
            "system": "dim cyan",
            "user": "bold white",
            "agent": "bold green",
            "error": "bold red",
            "status": "yellow",
        }
        color = colors.get(tag, "white")
        log.write(f"[{color}][{tag}][/{color}] {text}")

    # ── message processing ───────────────────────────────────────────────────

    @work(thread=True)
    def process_message(self, text: str) -> None:
        """Process a message through the selected graph+backend in a worker thread."""
        backend = self.backends_list[self.backends_idx]
        graph_type = self.graphs_list[self.graphs_idx]

        self._log("user", text)

        try:
            if graph_type == "coordinator":
                from langgraph_agents.coordinator import build_coordinator_graph
                graph = build_coordinator_graph()
            else:
                from langgraph_agents.agent_graph import build_agent_graph
                graph = build_agent_graph(llm_backend=backend)

            result = graph.invoke({
                "messages": [{"role": "user", "content": text}],
                "channel": "tui",
                "sender": "tui-user",
                "routing_decision": "",
                "response": "",
                "metadata": {},
            })

            response = result.get("response", "")
            if response:
                self._log("agent", response)
            else:
                self._log("system", "(empty response)")
        except Exception as e:
            self._log("error", str(e))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter in the input field."""
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.process_message(text)

    # ── keybindings ──────────────────────────────────────────────────────────

    def action_cycle_backend(self) -> None:
        self.backends_idx = (self.backends_idx + 1) % len(self.backends_list)
        backend = self.backends_list[self.backends_idx]
        status = self.query_one("#status", StatusBar)
        status.backend = backend
        self._log("system", f"Backend → {backend}")

    def action_cycle_graph(self) -> None:
        self.graphs_idx = (self.graphs_idx + 1) % len(self.graphs_list)
        graph_type = self.graphs_list[self.graphs_idx]
        status = self.query_one("#status", StatusBar)
        status.graph_type = graph_type
        self._log("system", f"Graph → {graph_type}")

    def action_toggle_sierpinski(self) -> None:
        """Toggle the Sierpinski triangle display."""
        self.show_banner = not self.show_banner
        if self.show_banner:
            tri = sierpinski(depth=4)
            for line in tri.split("\n"):
                self._log_raw(f"[cyan]{line}[/cyan]")
            self._log("system", "Sierpinski on (Ctrl+T to toggle)")
        else:
            self._log("system", "Sierpinski off (Ctrl+T to toggle)")

    @work(thread=True)
    def action_mesh_status(self) -> None:
        """Query mesh status in a worker thread."""
        import httpx
        self._log("status", f"Querying {self.mesh_url}...")
        try:
            resp = httpx.post(
                f"{self.mesh_url}/tools/mesh_status",
                json={},
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json().get("result", {})
            self._log("status", json.dumps(data, indent=2))
        except Exception as e:
            self._log("error", f"Mesh status failed: {e}")
