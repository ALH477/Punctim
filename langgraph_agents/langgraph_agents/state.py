# Copyright (C) 2026 DeMoD LLC
# SPDX-License-Identifier: LGPL-3.0-only
"""Shared state schema for all LangGraph agent graphs in Punctim."""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """State flowing through every agent graph.

    Fields:
        messages: LangGraph message accumulator (add_messages reducer).
        channel: DCF channel name (rendezvous dst).
        sender: hex node id of message origin.
        routing_decision: which specialist handles this.
        response: outbound text to send back.
        metadata: extensible per-agent data.
    """
    messages: Annotated[list, add_messages]
    channel: str
    sender: str
    routing_decision: str
    response: str
    metadata: dict[str, Any]
