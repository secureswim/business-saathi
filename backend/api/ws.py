"""One websocket channel. Both frontends subscribe; /ops consumes everything.

Every event is persisted to the `events` table, which is what makes a run
replayable after the fact and what a post-demo debrief reads.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi import WebSocket

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.data import repository as repo  # noqa: E402

_sockets: set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def register(ws: WebSocket) -> None:
    _sockets.add(ws)


def unregister(ws: WebSocket) -> None:
    _sockets.discard(ws)


def connection_count() -> int:
    return len(_sockets)


async def _safe_send(ws: WebSocket, payload: str) -> None:
    try:
        await ws.send_text(payload)
    except Exception:      # noqa: BLE001
        _sockets.discard(ws)


def broadcast(evt: dict) -> None:
    """Callable from sync code running in a worker thread."""
    try:
        repo.record_event(evt.get("query_id"), evt.get("type", "unknown"), evt)
    except Exception:      # noqa: BLE001
        pass
    if _loop is None or not _sockets:
        return
    payload = json.dumps(evt, default=str)
    for ws in list(_sockets):
        asyncio.run_coroutine_threadsafe(_safe_send(ws, payload), _loop)
