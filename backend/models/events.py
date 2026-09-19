"""WebSocket event names and constructors. The third shared contract.

/ops renders every one of these; the merchant route listens only to a subset.
Every event is persisted to the `events` table so a run can be replayed.
"""
from __future__ import annotations

import time
from typing import Any

# --- the twelve-stage pipeline trace rendered in column 1 of /ops -----------
PIPELINE_STAGES = [
    "query_started", "stt_complete", "intent_detected", "context_loaded",
    "tool_started", "graph_retrieval", "evidence_complete", "reasoning_started",
    "validation_result", "response_ready", "tts_started", "learning_complete",
]

# --- events the merchant route cares about ---------------------------------
MERCHANT_EVENTS = {"response_ready", "action_proposed", "workflow_started",
                   "outcome_measured", "proactive_alert", "reset"}

ALL_EVENTS = [
    "query_started", "stt_complete", "intent_detected", "context_loaded",
    "tool_started", "tool_result", "graph_retrieval", "evidence_complete",
    "reasoning_started", "validation_result", "response_ready", "tts_started",
    "action_proposed", "approval_received", "workflow_started", "workflow_node",
    "outcome_measured", "graph_writeback", "learning_complete",
    "proactive_alert", "context_stored", "reset",
]


def event(type_: str, query_id: str | None = None, **fields: Any) -> dict:
    assert type_ in ALL_EVENTS, f"unknown event {type_}"
    return {"type": type_, "query_id": query_id, "ts": round(time.time(), 3), **fields}
