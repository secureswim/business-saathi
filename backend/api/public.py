"""Public routes: what the two frontends call."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.actions import machine  # noqa: E402
from backend.actions.guardrails import validate as validate_guardrails  # noqa: E402
from backend.actions.orchestrator import get_orchestrator  # noqa: E402
from backend.api.ws import broadcast  # noqa: E402
from backend.data import context as ctx, db, repository as repo  # noqa: E402
from backend.graph.store import get_store  # noqa: E402
from backend.models.events import event  # noqa: E402
from backend.reasoning import engine  # noqa: E402
from backend.voice.adapter import get_voice  # noqa: E402

router = APIRouter()


# --------------------------------------------------------------------- query
@router.post("/api/query")
async def query(request: Request):
    body = await request.json()
    merchant_id = (body.get("merchant_id") or config.DEMO_MERCHANT).strip()
    text = (body.get("text") or "").strip()
    conversation_id = str(body.get("conversation_id") or "")[:128] or None

    if not text:
        return JSONResponse({"error": "empty_question"}, status_code=400)
    if len(text) > 500:
        return JSONResponse({"error": "question_too_long"}, status_code=400)
    try:
        repo.merchant_row(merchant_id)
    except KeyError:
        return JSONResponse({"error": "unknown_merchant"}, status_code=404)

    machine.expire_stale()
    result = await asyncio.to_thread(engine.ask, merchant_id, text, broadcast,
                                     body.get("params"), body.get("query_id"),
                                     body.get("source", "text"),
                                     conversation_id)

    if result.get("action_proposal"):
        run = machine.propose(merchant_id, result["action_proposal"])
        result["run_id"] = run.run_id
        result["action_proposal"]["run_id"] = run.run_id
        broadcast(event("action_proposed", result["query_id"], run_id=run.run_id,
                        type=run.type, params=run.params,
                        evidence_summary=run.evidence_summary,
                        guardrails_passed=validate_guardrails(run, machine.live_runs())["ok"]))
    return result


# --------------------------------------------------------------------- voice
@router.post("/api/voice")
async def voice(audio: UploadFile = File(...), merchant_id: str = Form(config.DEMO_MERCHANT)):
    raw = await audio.read()
    if not raw:
        return JSONResponse({"error": "no_audio"}, status_code=400)
    if len(raw) > 2 * 1024 * 1024:
        return JSONResponse({"error": "audio_too_large"}, status_code=413)

    stt = await asyncio.to_thread(get_voice().transcribe, raw)
    if not stt.get("ok"):
        return JSONResponse({"error": "transcription_failed", "detail": stt},
                            status_code=422)

    broadcast(event("stt_complete", None, transcript=stt["text"],
                    engine=stt.get("engine"), confidence=stt.get("confidence"),
                    ms=stt.get("ms")))
    result = await asyncio.to_thread(engine.ask, merchant_id, stt["text"], broadcast,
                                     None, None, "voice")
    result["transcript"] = stt
    if result.get("action_proposal"):
        run = machine.propose(merchant_id, result["action_proposal"])
        result["run_id"] = run.run_id
        result["action_proposal"]["run_id"] = run.run_id
    return result


@router.post("/api/stt")
async def stt_only(audio: UploadFile = File(...)):
    """Transcribe and return the text. Nothing else.

    /api/voice transcribes AND answers in one call, which is wrong for the
    merchant page: "haan" after a proposal is an approval, not a question, and
    the page's YES/NO handling and pending-followup state live in submit().
    So the page asks for text here and feeds it through exactly the same path
    a typed question takes. One code path for questions, whatever the source.
    """
    raw = await audio.read()
    if not raw:
        return {"ok": False, "error": "no_audio"}
    if len(raw) > 2 * 1024 * 1024:
        return {"ok": False, "error": "audio_too_large"}

    v = get_voice()
    if not v.server_side:
        return {"ok": False, "handled_by": "browser", "engine": v.name}

    result = await asyncio.to_thread(v.transcribe, raw, None)
    if result.get("ok"):
        broadcast(event("stt_complete", None, transcript=result.get("text"),
                        engine=result.get("engine"), model=result.get("model"),
                        translated=result.get("translated"),
                        confidence=result.get("confidence"), ms=result.get("ms")))
    return result


@router.post("/api/tts")
async def tts(request: Request):
    """Speak a line server-side.

    The merchant page calls this only when /api/config reports
    voice_server_side, and falls back to the browser's own speechSynthesis on
    any non-ok answer -- so a Sarvam outage costs the merchant nothing but a
    change of voice. That is also why a failure here returns 200 with ok=false
    rather than an error status: the page should fall back quietly, not treat
    it as a broken request.
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}
    if len(text) > 1500:
        text = text[:1500]

    v = get_voice()
    if not v.server_side:
        return {"ok": False, "handled_by": "browser", "engine": v.name}

    result = await asyncio.to_thread(v.speak, text, body.get("language"))
    if result.get("ok"):
        # `tts_started` is the twelfth stage of the pipeline /ops renders, and
        # nothing was emitting it until now. Audio is returned and about to
        # play, which is exactly the moment that stage marks. Do NOT invent a
        # new event name here -- events.py is a closed contract and the ops
        # canvas only knows these.
        broadcast(event("tts_started", None, engine=result.get("engine"),
                        model=result.get("model"), speaker=result.get("speaker"),
                        chars=len(text)))
    return result


# ------------------------------------------------------------------- context
@router.post("/api/context")
async def store_context(request: Request):
    body = await request.json()
    merchant_id = body.get("merchant_id") or config.DEMO_MERCHANT
    kind = body.get("kind") or "stock_estimate"
    utterance = (body.get("utterance") or "").strip()
    if not utterance:
        return JSONResponse({"error": "empty_utterance"}, status_code=400)

    stored = await asyncio.to_thread(
        ctx.store, merchant_id, kind, utterance, body.get("subject"),
        body.get("value_num"), body.get("value_text"), body.get("unit"))
    broadcast(event("context_stored", None, merchant_id=merchant_id, fact=stored))

    recomputed = None
    if body.get("rerun"):
        recomputed = await asyncio.to_thread(engine.ask, merchant_id, body["rerun"],
                                             broadcast, None, None, "context")
    return {"stored": True, **stored, "recomputed": recomputed}


# ------------------------------------------------------------------- actions
@router.post("/api/action/{run_id}/approve")
async def approve(run_id: str):
    run = machine.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown_run"}, status_code=404)
    if run.state != "proposed":
        return JSONResponse({"error": "already_decided", "state": run.state},
                            status_code=409)

    broadcast(event("approval_received", None, run_id=run_id, decision="approved",
                    via="voice"))
    check = validate_guardrails(run, machine.live_runs())
    if not check["ok"] and check.get("revised_params"):
        run.params = check["revised_params"]
        run.advance("proposed", f"parameters revised: {check['reason']}")
        broadcast(event("action_proposed", None, run_id=run_id, type=run.type,
                        params=run.params, evidence_summary=run.evidence_summary,
                        guardrails_passed=False, revised=True, reason=check["reason"]))
        return {"revised": True, "reason": check["reason"], "run": run.to_dict()}

    run = await asyncio.to_thread(get_orchestrator().execute, run, broadcast)
    return run.to_dict()


@router.post("/api/action/{run_id}/reject")
async def reject(run_id: str):
    run = machine.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown_run"}, status_code=404)
    run = machine.reject(run_id)
    broadcast(event("approval_received", None, run_id=run_id, decision="rejected",
                    via="voice"))
    return run.to_dict()


@router.get("/api/runs")
def runs():
    return {"runs": [r.to_dict() for r in machine.all_runs()]}


# --------------------------------------------------------------------- reads
@router.get("/api/merchants")
def merchants():
    rows = repo.all_merchants()
    return {
        "today": db.today().isoformat(),
        "demo_merchant": config.DEMO_MERCHANT,
        "coldstart_merchant": config.COLDSTART_MERCHANT,
        "peer_merchant": config.PEER_MERCHANT,
        "merchants": [{"id": r["id"], "name": r["name"], "category": r["category"],
                       "locality": r["locality"], "locality_type": r["locality_type"],
                       "volume_band": r["volume_band"],
                       "has_obligations": bool(r["has_obligations"]),
                       "has_stock_feed": bool(r["has_stock_feed"])} for r in rows],
    }


@router.get("/api/merchant/{merchant_id}")
def merchant(merchant_id: str):
    try:
        ctxt = repo.merchant_context(merchant_id)
    except KeyError:
        return JSONResponse({"error": "unknown_merchant"}, status_code=404)
    return {"context": ctxt.to_dict(),
            "recent_situations": repo.recent_situations(merchant_id, 30)[:5],
            "recent_actions": repo.action_history(merchant_id, 3),
            "live_facts": ctx.live_for(merchant_id)}


@router.get("/api/evidence/{query_id}")
def evidence(query_id: str):
    events = repo.events_for(query_id)
    if not events:
        return JSONResponse({"error": "unknown_query"}, status_code=404)
    return {"query_id": query_id, "events": events}


@router.get("/api/graph/{merchant_id}")
def graph(merchant_id: str):
    try:
        repo.merchant_row(merchant_id)
    except KeyError:
        return JSONResponse({"error": "unknown_merchant"}, status_code=404)
    store = get_store()
    peers = store.peers(merchant_id)
    key = peers["value"].get("cohort_key", "")
    return {"merchant": peers["value"]["merchant"],
            "cohort_key": key,
            "cohort_size": peers["value"]["cohort_size"],
            "peers": peers["basis"]["peers"],
            "extended": peers["basis"]["extended"],
            "learned_patterns": repo.learned_patterns_for(key)}


@router.get("/api/cohort/{merchant_id}")
def cohort(merchant_id: str, situation_kind: str = "evening_decline",
           action_type: str = "evening_offer"):
    from backend.actions.writeback import cohort_snapshot
    return cohort_snapshot(merchant_id, situation_kind, action_type)


@router.get("/api/alerts")
def alerts():
    return {"alerts": repo.recent_alerts(20)}


@router.get("/api/config")
def read_config():
    v = get_voice()
    return {"adapters": config.adapters(),
            "min_cohort_size": config.MIN_COHORT_SIZE,
            "similarity_weights": config.SIMILARITY_WEIGHTS,
            "similarity_threshold": config.SIMILARITY_THRESHOLD,
            "extended_threshold": config.EXTENDED_THRESHOLD,
            "alert_deviation_pct": config.ALERT_DEVIATION_PCT,
            "sim_clock_seconds_per_day": config.SIM_CLOCK_SECONDS_PER_DAY,
            "voice_server_side": v.server_side}


@router.get("/api/health")
def health():
    checks = {"db": False, "graph_store": False, "orchestrator": False,
              "reasoner": False, "voice": False}
    try:
        repo.merchant_row(config.DEMO_MERCHANT)
        checks["db"] = True
    except Exception:      # noqa: BLE001
        pass
    try:
        checks["graph_store"] = get_store().name is not None
    except Exception:      # noqa: BLE001
        pass
    checks["orchestrator"] = get_orchestrator().name is not None
    from backend.reasoning.llm import get_reasoner
    checks["reasoner"] = get_reasoner().name is not None
    checks["voice"] = get_voice().name is not None
    return {"ok": all(checks.values()), **checks, "adapters": config.adapters()}
