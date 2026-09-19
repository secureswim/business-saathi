"""Paytm Business Saathi — API surface.

Run:  uvicorn app.main:app --reload --port 8000
Then: http://localhost:8000
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.adapters.graph import get_graph
from app.core.financial import FinancialEngine
from app.core.intent import parse
from app.core.reasoning import ReasoningLayer
from app.core.synth import CATEGORIES, LOCALITIES
from app.workflows.engine import WorkflowEngine

WEB = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title="Paytm Business Saathi", version="1.0.0",
              description="Voice-first AI business partner built on a Merchant Knowledge Graph.")

graph = get_graph()
fin = FinancialEngine(graph.ds)
brain = ReasoningLayer(graph, fin)
flows = WorkflowEngine(graph, fin)

DEFAULT_MERCHANT = "M-001"


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


class AskBody(BaseModel):
    merchant_id: str = DEFAULT_MERCHANT
    text: str


class ApproveBody(BaseModel):
    merchant_id: str = DEFAULT_MERCHANT
    proposal: dict[str, Any]


class ColdStartBody(BaseModel):
    category: str = "food_stall"
    locality: str = "sector-62-noida"
    pattern: str = "evening_heavy"


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "graph_backend": type(graph).__name__,
        "llm_backend": type(brain.llm).__name__,
        "n8n": "remote" if flows.remote else "local",
        "merchants": len(graph.ds.merchants),
        "graph_nodes": graph.g.number_of_nodes(),
        "graph_edges": graph.g.number_of_edges(),
        "action_chains": len(graph.ds.actions),
    }


@app.get("/api/merchants")
def merchants() -> list[dict[str, Any]]:
    out = []
    for m in graph.ds.merchants:
        out.append({
            "id": m.id, "name": m.name,
            "category": CATEGORIES[m.category]["label"],
            "category_key": m.category,
            "locality": LOCALITIES[m.locality]["label"],
            "locality_key": m.locality,
            "pattern": m.pattern,
            "avg_daily_gmv": round(graph.ds.avg_daily_gmv(m.id)),
            "trend_pct": round(fin.trend(m.id).delta_pct, 1),
        })
    return out


@app.get("/api/merchant/{merchant_id}")
def merchant(merchant_id: str) -> dict[str, Any]:
    _check(merchant_id)
    m = graph.ds.merchant(merchant_id)
    t = fin.trend(merchant_id)
    slot, st = fin.worst_slot(merchant_id)
    return {
        "id": m.id, "name": m.name,
        "category": CATEGORIES[m.category]["label"],
        "locality": LOCALITIES[m.locality]["label"],
        "pattern": m.pattern,
        "opened_days_ago": m.opened_days_ago,
        "avg_ticket": round(fin.avg_ticket(merchant_id)),
        "trend": t.as_dict(),
        "worst_slot": {"slot": slot, **st.as_dict()},
        "cashflow": fin.cashflow(merchant_id).as_dict(),
        "locality_health": graph.locality_health(merchant_id),
        "next_rush": fin.next_rush(merchant_id),
        "charts": {"daily": fin.daily_series(merchant_id), "hourly": fin.hourly_profile(merchant_id)},
    }


# --------------------------------------------------------------------------
# the conversation
# --------------------------------------------------------------------------


@app.post("/api/ask")
def ask(body: AskBody) -> dict[str, Any]:
    """Merchant speech (already transcribed) in, structured answer out."""
    _check(body.merchant_id)
    if not body.text.strip():
        raise HTTPException(400, "empty query")
    intent = parse(body.text)
    answer = brain.answer_intent(body.merchant_id, intent)
    return {"query": body.text, "parsed": intent.as_dict(), "answer": answer.as_dict()}


@app.post("/api/approve")
def approve(body: ApproveBody) -> dict[str, Any]:
    """Merchant said yes. Hand it to the execution workflow."""
    _check(body.merchant_id)
    if not body.proposal.get("action_type"):
        raise HTTPException(400, "proposal missing action_type")
    run = flows.execute_offer(body.merchant_id, body.proposal)
    return run.as_dict()


# --------------------------------------------------------------------------
# graph + workflows
# --------------------------------------------------------------------------


@app.get("/api/graph/{merchant_id}")
def graph_snapshot(merchant_id: str) -> dict[str, Any]:
    _check(merchant_id)
    return graph.snapshot(merchant_id)


@app.get("/api/evidence/{merchant_id}")
def evidence(merchant_id: str) -> list[dict[str, Any]]:
    _check(merchant_id)
    return [e.as_dict() for e in graph.action_catalogue(merchant_id)]


@app.get("/api/peers/{merchant_id}")
def peers(merchant_id: str) -> list[dict[str, Any]]:
    _check(merchant_id)
    out = []
    for p in graph.peers(merchant_id):
        m = graph.ds.merchant(p.merchant_id)
        out.append({**p.as_dict(), "category": CATEGORIES[m.category]["label"],
                    "locality": LOCALITIES[m.locality]["label"]})
    return out


@app.post("/api/monitor")
def monitor(merchant_id: str | None = None) -> dict[str, Any]:
    """Fire the monitoring workflow — the one that runs without being asked."""
    runs = flows.monitor([merchant_id] if merchant_id else None)
    return {"runs": [r.as_dict() for r in runs],
            "alerts": [a for a in flows.alerts if not a["read"]]}


@app.get("/api/alerts")
def alerts(merchant_id: str | None = None) -> list[dict[str, Any]]:
    out = flows.alerts
    if merchant_id:
        out = [a for a in out if a["merchant_id"] == merchant_id]
    return out


@app.get("/api/runs")
def runs(merchant_id: str | None = None) -> list[dict[str, Any]]:
    rs = list(flows.runs.values())
    if merchant_id:
        rs = [r for r in rs if r.merchant_id == merchant_id]
    return [r.as_dict() for r in reversed(rs)]


@app.post("/api/cold-start")
def cold_start(body: ColdStartBody) -> dict[str, Any]:
    """Day 1: what a merchant with zero transaction history already knows."""
    return graph.cold_start(body.category, body.locality, body.pattern)


@app.get("/api/taxonomy")
def taxonomy() -> dict[str, Any]:
    return {
        "categories": [{"key": k, "label": v["label"]} for k, v in CATEGORIES.items()],
        "localities": [{"key": k, "label": v["label"]} for k, v in LOCALITIES.items()],
        "patterns": ["evening_heavy", "lunch_heavy", "morning_heavy", "steady"],
    }


# --------------------------------------------------------------------------
# voice (Sarvam adapter)
# --------------------------------------------------------------------------


@app.get("/api/voice/config")
def voice_config() -> dict[str, Any]:
    """The front end asks whether real Sarvam STT/TTS is wired, and falls back
    to the browser's own speech APIs when it is not. Same UX either way."""
    return {
        "stt": "sarvam" if os.environ.get("SARVAM_API_KEY") else "browser_webspeech",
        "tts": "sarvam" if os.environ.get("SARVAM_API_KEY") else "browser_speechsynthesis",
        "languages": ["hi-IN", "en-IN"],
        "note": "Set SARVAM_API_KEY to route speech through Sarvam instead of the browser.",
    }


# --------------------------------------------------------------------------


def _check(merchant_id: str) -> None:
    if not any(m.id == merchant_id for m in graph.ds.merchants):
        raise HTTPException(404, f"unknown merchant {merchant_id}")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
