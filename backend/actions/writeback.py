"""The write-back that closes the loop.

Order matters and is deliberate: SQLite first, Cognee second. The ledger is
authoritative, so a Cognee outage degrades retrieval quality but never loses
an outcome -- the counters still move on screen and /ops shows the graph as
degraded.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.data import repository as repo  # noqa: E402
from backend.graph.store import get_store  # noqa: E402


def cohort_snapshot(merchant_id: str, situation_kind: str,
                    action_type: str | None = None) -> dict:
    """What the graph currently knows. Read before and after, so the change is exact."""
    store = get_store()
    peers = store.peers(merchant_id)["value"]
    pb = store.peer_playbook(situation_kind, peers["peer_ids"])["value"]
    best = pb.get("best")
    if action_type:
        match = next((o for o in pb.get("options", []) if o["action"] == action_type), None)
        best = match or best
    return {
        "merchant_id": merchant_id,
        "cohort_key": peers.get("cohort_key"),
        "cohort_size": peers["cohort_size"],
        "situation_kind": situation_kind,
        "action_type": (best or {}).get("action", action_type),
        "tried": (best or {}).get("tried", 0),
        "worked": (best or {}).get("worked", 0),
        "success_rate": (best or {}).get("success_rate"),
        "median_delta": (best or {}).get("median_delta"),
    }


def write_outcome(run, cohort_key: str) -> dict:
    """Ledger, then aggregate, then graph. Returns what changed."""
    store = get_store()
    o = run.outcome
    result = store.record_outcome(
        action_id=run.action_id,
        metric="evening_gmv" if "evening" in run.type else "daily_gmv",
        before=o.before, after=o.after, verdict=o.verdict,
        measured_on=repo.db.today().isoformat() if hasattr(repo, "db") else "",
        cohort_key=cohort_key, situation_kind=run.condition, action_type=run.type)
    return result
