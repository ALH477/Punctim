# Copyright (C) 2026 DeMoD LLC
# SPDX-License-Identifier: LGPL-3.0-only
"""HTTP API server exposing the agent system externally.

REST endpoints:
  GET  /health          — health check
  GET  /agents          — list configured agents
  GET  /backends        — list available LLM backends
  GET  /providers       — list available provider presets
  POST /chat            — send a message to an agent, get response
  POST /chat/stream     — send a message, get chunked response (SSE)
  POST /mesh/send       — proxy mesh_send
  POST /mesh/recv       — proxy mesh_recv
  GET  /mesh/status     — proxy mesh_status
  WS   /ws              — WebSocket chat (send msg, receive response)

Encryption-free for export control purposes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent_graph import build_agent_graph
from .coordinator import build_coordinator_graph
from .llm_node import BACKENDS, list_backends, list_providers, register_from_config
from .streaming_bridge import chunk_response, DCF_TEXT_MAX

logger = logging.getLogger("dcf-agent-api")


# ── request/response models ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., description="Message text to send")
    backend: str = Field(default="echo", description="LLM backend name")
    graph: str = Field(default="base", description="Graph type: base or coordinator")
    channel: str = Field(default="api", description="DCF channel name")
    sender: str = Field(default="api-client", description="Sender identity")


class ChatResponse(BaseModel):
    response: str
    backend: str
    graph: str
    chunks: int = 1


class MeshSendRequest(BaseModel):
    text: str
    channel: str = ""


class MeshRecvRequest(BaseModel):
    timeout_s: float = 5.0


# ── app factory ───────────────────────────────────────────────────────────────

def create_app(config_path: str = "agents.jsonc", mesh_url: str = "") -> FastAPI:
    """Build the FastAPI app with config-loaded providers."""
    from .__main__ import load_config

    app = FastAPI(
        title="Punctim Agent API",
        description="LLM agent system over DCF mesh — encryption-free for export control",
        version="0.1.0",
    )

    # Load config and register providers
    try:
        config = load_config(config_path)
        providers = config.get("providers", [])
        if providers:
            register_from_config(providers)
        agents = config.get("agents", [])
        default_mesh_url = mesh_url or config.get("mesh_server", {}).get(
            "url", "http://127.0.0.1:8765"
        )
    except Exception as e:
        logger.warning(f"Config load failed ({e}), using defaults")
        agents = []
        default_mesh_url = mesh_url or "http://127.0.0.1:8765"

    app.state.config_path = config_path
    app.state.agents = agents
    app.state.mesh_url = default_mesh_url

    # ── endpoints ─────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "backends": list_backends(), "providers": list_providers()}

    @app.get("/agents")
    async def get_agents() -> list[dict]:
        return app.state.agents

    @app.get("/backends")
    async def get_backends() -> dict:
        return {"backends": list_backends(), "providers": list_providers()}

    @app.get("/providers")
    async def get_providers() -> list[str]:
        return list_providers()

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        """One-shot chat: send a message, get response."""
        if req.graph == "coordinator":
            graph = build_coordinator_graph()
        else:
            graph = build_agent_graph(llm_backend=req.backend)

        result = graph.invoke({
            "messages": [{"role": "user", "content": req.message}],
            "channel": req.channel,
            "sender": req.sender,
            "routing_decision": "",
            "response": "",
            "metadata": {},
        })
        response_text = result.get("response", "")
        chunks = chunk_response(response_text)
        return ChatResponse(
            response=response_text,
            backend=req.backend,
            graph=req.graph,
            chunks=len(chunks),
        )

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest):
        """Stream chat response as SSE chunks (DCF-Text sized)."""
        if req.graph == "coordinator":
            graph = build_coordinator_graph()
        else:
            graph = build_agent_graph(llm_backend=req.backend)

        result = graph.invoke({
            "messages": [{"role": "user", "content": req.message}],
            "channel": req.channel,
            "sender": req.sender,
            "routing_decision": "",
            "response": "",
            "metadata": {},
        })
        response_text = result.get("response", "")
        chunks = chunk_response(response_text)

        async def generate():
            for i, chunk in enumerate(chunks):
                data = json.dumps({"chunk": i, "total": len(chunks), "text": chunk})
                yield f"data: {data}\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/mesh/send")
    async def mesh_send(req: MeshSendRequest) -> dict:
        """Proxy mesh_send to the MCP server."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{app.state.mesh_url}/tools/mesh_send",
                json={"text": req.text, "channel": req.channel},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()

    @app.post("/mesh/recv")
    async def mesh_recv(req: MeshRecvRequest) -> dict:
        """Proxy mesh_recv to the MCP server."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{app.state.mesh_url}/tools/mesh_recv",
                json={"timeout_s": req.timeout_s},
                timeout=req.timeout_s + 5.0,
            )
            resp.raise_for_status()
            return resp.json()

    @app.get("/mesh/status")
    async def mesh_status() -> dict:
        """Proxy mesh_status to the MCP server."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{app.state.mesh_url}/tools/mesh_status",
                json={},
                timeout=5.0,
            )
            resp.raise_for_status()
            return resp.json().get("result", {})

    @app.websocket("/ws")
    async def ws_chat(websocket: WebSocket) -> None:
        """WebSocket chat: receive messages, send responses."""
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_json()
                msg = data.get("message", "")
                backend = data.get("backend", "echo")
                graph_type = data.get("graph", "base")

                if graph_type == "coordinator":
                    graph = build_coordinator_graph()
                else:
                    graph = build_agent_graph(llm_backend=backend)

                result = graph.invoke({
                    "messages": [{"role": "user", "content": msg}],
                    "channel": data.get("channel", "ws"),
                    "sender": data.get("sender", "ws-client"),
                    "routing_decision": "",
                    "response": "",
                    "metadata": {},
                })
                response = result.get("response", "")
                chunks = chunk_response(response)
                for i, chunk in enumerate(chunks):
                    await websocket.send_json({
                        "chunk": i,
                        "total": len(chunks),
                        "text": chunk,
                    })
        except WebSocketDisconnect:
            pass

    return app


def run_server(host: str = "0.0.0.0", port: int = 8000, config_path: str = "agents.jsonc") -> None:
    """Run the API server with uvicorn."""
    import uvicorn
    app = create_app(config_path=config_path)
    uvicorn.run(app, host=host, port=port)
