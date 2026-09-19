"""Short-lived in-process conversation context for LLM follow-up resolution.

Only compact summaries are retained. Transaction evidence stays in the ledger
and is re-read for every question, so conversation memory can never become a
second source of business truth.
"""
from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock

_turns: dict[str, deque] = defaultdict(lambda: deque(maxlen=4))
_lock = Lock()


def recent(conversation_id: str | None) -> list[dict]:
    if not conversation_id:
        return []
    with _lock:
        return list(_turns.get(conversation_id, ()))


def remember(conversation_id: str | None, question: str, intent: str,
             answer: str, evidence_tools: list[str]) -> None:
    if not conversation_id:
        return
    turn = {
        "question": question[:500],
        "intent": intent,
        "answer_summary": answer[:500],
        "evidence_tools": evidence_tools[:12],
    }
    with _lock:
        _turns[conversation_id].append(turn)


def clear() -> None:
    with _lock:
        _turns.clear()
