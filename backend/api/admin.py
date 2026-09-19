"""Presenter controls. Built in phase 5, not the night before.

A demo you cannot reset is a demo you can give once.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.actions import machine, monitor  # noqa: E402
from backend.actions.orchestrator import get_orchestrator  # noqa: E402
from backend.api.ws import broadcast  # noqa: E402
from backend.data import db  # noqa: E402
from backend.graph import store as store_mod  # noqa: E402
from backend.models.events import event  # noqa: E402
from backend.reasoning import conversation  # noqa: E402

router = APIRouter(prefix="/api/admin")


@router.post("/reset")
def reset():
    """Restore the seed database. Under two seconds."""
    if not config.SEED_DB_PATH.exists():
        return JSONResponse(
            {"error": "no_seed", "detail": f"no seed at {config.SEED_DB_PATH}; "
                                           f"run python scripts/generate.py"},
            status_code=400)
    db.reset_connection()                       # close every handle first
    for suffix in ("-wal", "-shm"):             # then drop the stale journal
        p = Path(str(config.DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    shutil.copyfile(config.SEED_DB_PATH, config.DB_PATH)
    db.reset_connection()                       # and invalidate again post-copy
    machine.clear()
    conversation.clear()
    store_mod.reset_store()
    broadcast(event("reset", None))
    return {"ok": True, "today": db.today().isoformat()}


@router.post("/simulate-day")
def simulate_day():
    """Advance the simulated clock by one day."""
    new_today = db.today() + timedelta(days=1)
    db.write("INSERT INTO meta (key, value) VALUES ('today', ?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (new_today.isoformat(),))
    return {"ok": True, "today": new_today.isoformat()}


@router.post("/trigger-alert")
async def trigger_alert(limit: int = 20):
    """Force the monitoring scan now, instead of waiting for the schedule."""
    alerts = await asyncio.to_thread(monitor.scan, None, limit, True)
    for a in alerts:
        broadcast(event("proactive_alert", None, **a))
    return {"alerts": alerts, "count": len(alerts)}


@router.post("/measure")
async def measure(run_id: str, force: str | None = None):
    """Fast-forward the outcome window.

    `force` is a presenter override and is DISCLOSED: the outcome carries
    forced=true and /ops shows it. Do not hide this -- a judge who spots a
    rigged demo discounts everything else.
    """
    run = machine.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown_run"}, status_code=404)
    if run.state != "running":
        return JSONResponse({"error": "not_running", "state": run.state},
                            status_code=409)
    run = await asyncio.to_thread(get_orchestrator().measure, run, broadcast, force)
    return run.to_dict()


@router.post("/select-merchant")
def select_merchant(merchant_id: str):
    broadcast(event("reset", None, merchant_id=merchant_id))
    return {"ok": True, "merchant_id": merchant_id}
