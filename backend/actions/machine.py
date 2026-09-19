"""The action state machine and the run registry.

Transitions are identical whether n8n or the local orchestrator drives them --
that is the point of the Orchestrator adapter. The registry is in memory
because runs do not need to survive a restart during a demo; the ledger does,
and it is in SQLite.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.analytics import anomaly  # noqa: E402
from backend.data import db  # noqa: E402
from backend.graph.store import get_store  # noqa: E402
from backend.models.action import ActionRun  # noqa: E402

PROPOSAL_TTL_SECONDS = 15    # silence drops a pending proposal

_runs: dict[str, ActionRun] = {}
_created: dict[str, datetime] = {}


def propose(merchant_id: str, proposal: dict, situation_id: int | None = None) -> ActionRun:
    if situation_id is None:
        # The situation is the join key the graph reasons about: without a typed
        # row, this action can never be matched to "what worked in this situation".
        try:
            peers = get_store().peers(merchant_id)["value"]["peer_ids"]
            detected = anomaly.detect_situation(merchant_id, peers, persist=True)["value"]
            situation_id = detected.get("situation_id")
        except Exception:      # noqa: BLE001
            situation_id = None

    run = ActionRun(
        merchant_id=merchant_id,
        type=proposal["type"],
        params=dict(proposal["params"]),
        condition=proposal.get("condition", "sales_decline"),
        evidence_summary=proposal.get("evidence_summary", ""),
        situation_id=situation_id,
        created_at=db.today().isoformat(),
    )
    run.advance("proposed", "awaiting the merchant's spoken approval")
    _runs[run.run_id] = run
    _created[run.run_id] = datetime.utcnow()
    return run


def get(run_id: str) -> ActionRun | None:
    return _runs.get(run_id)


def all_runs() -> list[ActionRun]:
    return sorted(_runs.values(), key=lambda r: _created.get(r.run_id, datetime.min),
                  reverse=True)


def live_runs() -> list[ActionRun]:
    return [r for r in _runs.values() if not r.is_terminal]


def expire_stale() -> list[ActionRun]:
    """A proposal left unanswered is dropped, never executed later by surprise."""
    out = []
    now = datetime.utcnow()
    for run in list(_runs.values()):
        if run.state == "proposed" and (
                now - _created.get(run.run_id, now)).total_seconds() > PROPOSAL_TTL_SECONDS:
            run.advance("expired", "no response within 15 seconds")
            out.append(run)
    return out


def reject(run_id: str) -> ActionRun:
    run = _runs[run_id]
    run.advance("rejected", "merchant declined; nothing was executed")
    return run


def clear() -> None:
    _runs.clear()
    _created.clear()
