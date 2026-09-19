"""Internal routes: the callbacks n8n drives.

Guarded by a shared secret so a stray n8n instance cannot drive the demo.
Every one is idempotent on run_id -- a replayed webhook does not double-write.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.actions import guardrails, machine, monitor, writeback  # noqa: E402
from backend.analytics import outcome as outcome_engine  # noqa: E402
from backend.api.ws import broadcast  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402
from backend.graph.store import get_store  # noqa: E402
from backend.models.action import Outcome  # noqa: E402
from backend.models.events import event  # noqa: E402

router = APIRouter(prefix="/api/internal")

_campaigns: dict[str, dict] = {}     # idempotency for the simulated campaign API


def _auth(secret: str | None) -> bool:
    return secret == config.INTERNAL_SECRET


def _denied():
    return JSONResponse({"error": "forbidden"}, status_code=403)


@router.post("/validate-action")
async def validate_action(request: Request, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    run = machine.get(body.get("run_id"))
    if run is None:
        return JSONResponse({"error": "unknown_run"}, status_code=404)
    return guardrails.validate(run, machine.live_runs())


@router.post("/simulate-campaign")
async def simulate_campaign(request: Request, x_saathi_secret: str = Header(None)):
    """SIMULATED external execution. There is no real Paytm campaign endpoint."""
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    run_id = body.get("run_id")
    if run_id in _campaigns:                      # idempotent on run_id
        return {**_campaigns[run_id], "replayed": True}
    result = {"campaign_id": f"SIMULATED-{run_id}", "simulated": True,
              "note": "no real campaign was created; CampaignAPI has no real "
                      "implementation in this build"}
    _campaigns[run_id] = result
    return result


@router.post("/record-action")
async def record_action(request: Request, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    run = machine.get(body.get("run_id"))
    if run is None:
        return JSONResponse({"error": "unknown_run"}, status_code=404)
    if run.action_id:                              # idempotent
        return {"action_id": run.action_id, "replayed": True}
    today = db.today()
    end = today + timedelta(days=int(run.params.get("days", 3)) - 1)
    run.action_id = get_store().record_action(
        run.merchant_id, run.type, run.params, run.situation_id,
        today.isoformat(), end.isoformat(), run.run_id)
    run.advance("running", f"campaign live, logged as action {run.action_id}")
    return {"action_id": run.action_id}


@router.post("/measure-outcome")
async def measure_outcome(request: Request, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    run = machine.get(body.get("run_id"))
    if run is None:
        return JSONResponse({"error": "unknown_run"}, status_code=404)
    if run.outcome:                                # idempotent
        return {**run.outcome.to_dict(), "replayed": True}

    store = get_store()
    peers = store.peers(run.merchant_id)["value"]
    pb = store.peer_playbook(run.condition, peers["peer_ids"])["value"]
    rate = pb["best"]["success_rate"] / 100.0 if pb.get("best") else None
    res = await asyncio.to_thread(outcome_engine.measure, run.merchant_id, run.params,
                                  run.run_id, run.type, rate, body.get("force"))
    v = res["value"]
    run.outcome = Outcome(before=v["before"], after=v["after"], delta_pct=v["delta_pct"],
                          verdict=v["verdict"], simulated=True, forced=v["forced"])
    run.advance("measuring", "outcome measured from the ledger")
    broadcast(event("outcome_measured", None, run_id=run.run_id, **v))
    return v


@router.post("/graph/writeback")
async def graph_writeback(request: Request, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    run = machine.get(body.get("run_id"))
    if run is None or run.outcome is None:
        return JSONResponse({"error": "no_outcome"}, status_code=409)
    if run.state == "learned":
        return {"replayed": True}

    store = get_store()
    cohort_key = store.peers(run.merchant_id)["value"].get("cohort_key", "")
    before = writeback.cohort_snapshot(run.merchant_id, run.condition, run.type)
    wb = writeback.write_outcome(run, cohort_key)
    after = writeback.cohort_snapshot(run.merchant_id, run.condition, run.type)
    run.advance("learned", f"outcome {run.outcome.verdict} written back to the graph")

    broadcast(event("graph_writeback", None, run_id=run.run_id, store=wb["store"],
                    outcome_rows=wb.get("outcome_rows", 0),
                   pattern_rows=wb.get("pattern_rows", 0),
                   cards_submitted=wb.get("cards_submitted", 0),
                   detail=wb.get("detail", ""),
                    cohort_key=cohort_key, degraded=wb.get("degraded", False)))
    broadcast(event("learning_complete", None, run_id=run.run_id, cohort_key=cohort_key,
                    situation_kind=run.condition, action_type=run.type,
                    before=before, after=after))
    return {"before": before, "after": after, **wb}


@router.get("/monitor/candidates")
def monitor_candidates(limit: int = 20, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    return {"merchant_ids": monitor.candidates(limit)}


@router.post("/monitor/evaluate")
async def monitor_evaluate(request: Request, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    ids = body.get("merchant_ids") or []
    out = []
    for mid in ids:
        try:
            out.append(await asyncio.to_thread(monitor.evaluate, mid))
        except Exception:      # noqa: BLE001
            continue
    return {"evaluations": out}


@router.post("/peer-playbook")
async def peer_playbook(request: Request, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    store = get_store()
    peers = store.peers(body["merchant_id"])["value"]
    return store.peer_playbook(body.get("situation_kind", "evening_decline"),
                               peers["peer_ids"])


@router.post("/alerts")
async def post_alert(request: Request, x_saathi_secret: str = Header(None)):
    if not _auth(x_saathi_secret):
        return _denied()
    alert = await request.json()
    repo.insert_alert(alert["merchant_id"], alert.get("alert_type", "decline"),
                      alert.get("severity", 0.0), alert)
    broadcast(event("proactive_alert", None, **alert))
    return {"ok": True}


@router.post("/workflow-node")
async def workflow_node(request: Request, x_saathi_secret: str = Header(None)):
    """n8n node progress, rendered live in the /ops workflow column."""
    if not _auth(x_saathi_secret):
        return _denied()
    body = await request.json()
    broadcast(event("workflow_node", None, **body))
    return {"ok": True}
