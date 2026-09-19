"""Conversation state: what was asked, what was answered, what was looked up.

This replaces four in-process summaries that only the planner ever saw. The
agent now receives the actual thread, so "aur kal?" and "60 bottles" and "haan"
resolve against what was just said instead of re-running the previous question
with a fact glued on -- which is why follow-ups used to repeat themselves.

Two deliberate limits:

  * only a COMPACT summary of each tool result is kept (the tool name and its
    headline values), never the full evidence. Conversation memory must never
    become a second source of business truth; the ledger is re-read every turn.
  * turns live in SQLite next to everything else, so a restart mid-demo does
    not lose the thread, and /ops can show it.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.data import db  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    id              INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    merchant_id     TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    tools           TEXT NOT NULL,
    open_ask        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv ON conversation_turns(conversation_id, id);
"""

# Values worth carrying forward from a tool result. Anything else is noise in
# the prompt and pushes the useful part out of the window.
HEADLINE_KEYS = (
    "period", "total", "txns", "avg_ticket", "per_day", "change_pct", "direction",
    "current_daily", "baseline_daily", "worst_band", "worst_band_pct", "headline",
    "verdict", "buffer_expected", "buffer_stressed", "purchase", "scope",
    "bills_total", "safe_after_days", "cohort_size", "success_rate", "tried",
    "worked", "median_delta", "action", "days_of_cover", "runs_out_on",
    "units_per_day", "quantity", "subject", "unit", "result", "label",
    "total_expected", "total_low", "total_high", "peer_median_change_pct",
    "gap_pct", "failure_rate", "safest_bucket", "compared_to", "difference_per_day",
)

_ensured = False


def _ensure() -> None:
    global _ensured
    if _ensured:
        return
    con = db.connect()
    con.executescript(SCHEMA)
    con.commit()
    _ensured = True


def new_id() -> str:
    return "c_" + uuid.uuid4().hex[:10]


def summarise(evidence) -> list[dict]:
    """One compact line per tool: what was called and what came back."""
    out = []
    for ev in evidence:
        value = ev.value if hasattr(ev, "value") else ev.get("value", {})
        tool = ev.tool if hasattr(ev, "tool") else ev.get("tool")
        available = ev.available if hasattr(ev, "available") else ev.get("available", True)
        if not available:
            out.append({"tool": tool, "available": False,
                        "reason": (value or {}).get("reason", "")[:120]})
            continue
        headline = {k: v for k, v in (value or {}).items()
                    if k in HEADLINE_KEYS and not isinstance(v, (list, dict))}
        out.append({"tool": tool, "available": True, "values": headline})
    return out


def remember(conversation_id: str | None, merchant_id: str, question: str,
             answer: str, evidence=None, open_ask: str | None = None) -> None:
    if not conversation_id:
        return
    _ensure()
    db.write(
        "INSERT INTO conversation_turns (conversation_id, merchant_id, question, "
        "answer, tools, open_ask, created_at) VALUES (?,?,?,?,?,?,?)",
        (conversation_id, merchant_id, question[:600], answer[:600],
         json.dumps(summarise(evidence or []), ensure_ascii=False),
         open_ask, datetime.utcnow().isoformat()))


def recent(conversation_id: str | None, limit: int | None = None) -> list[dict]:
    """Oldest first, so it reads as a transcript."""
    if not conversation_id:
        return []
    _ensure()
    limit = limit or config.CONVERSATION_TURNS
    rows = db.q("SELECT * FROM conversation_turns WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?", (conversation_id, limit))
    turns = []
    for r in reversed(rows):
        try:
            tools = json.loads(r["tools"])
        except Exception:                             # noqa: BLE001
            tools = []
        turns.append({"question": r["question"], "answer": r["answer"],
                      "tools": tools, "open_ask": r["open_ask"]})
    return turns


def open_ask(conversation_id: str | None) -> str | None:
    """The question Saathi last asked and has not had answered."""
    turns = recent(conversation_id, limit=1)
    return turns[0]["open_ask"] if turns else None


def clear(conversation_id: str | None = None) -> None:
    _ensure()
    if conversation_id:
        db.write("DELETE FROM conversation_turns WHERE conversation_id=?",
                 (conversation_id,))
    else:
        db.write("DELETE FROM conversation_turns", ())
