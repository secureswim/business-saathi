"""Adapter liveness: what is configured, and what is actually answering.

There is a difference between those two, and on stage it is the whole ball
game. The /ops header used to print `graph: cognee` because a flag was set and
a key was present -- not because Cognee had answered anything. A judge reading
that chip while every figure came out of SQLite is being told something untrue
by the demo itself, which is worse than not claiming Cognee at all.

So each adapter reports three separate things:

    configured  the flag is on and credentials exist
    reachable   it answered a probe just now, with a latency
    serving     it contributed to the last real answer

An adapter can be configured and reachable and still not serving -- Cognee
retrieval is deliberately non-blocking, so the first question of a session is
answered from the ledger while retrieval is still in flight. That is a correct
design decision and a dishonest chip, unless the chip says so.

Probes are cached and short. Nothing here may ever delay a merchant.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

PROBE_TIMEOUT = 4.0
PROBE_TTL = 30.0

_cache: dict[str, dict] = {}
_lock = threading.Lock()

# What the last answer actually used, written by the engine.
_serving: dict[str, str] = {}


def note_serving(adapter: str, detail: str) -> None:
    """Record which implementation really answered. Called per query."""
    with _lock:
        _serving[adapter] = detail


def serving() -> dict[str, str]:
    with _lock:
        return dict(_serving)


def _cached(name: str):
    entry = _cache.get(name)
    if entry and time.time() - entry["at"] < PROBE_TTL:
        return entry
    return None


def _store(name: str, **fields) -> dict:
    entry = {"at": time.time(), **fields}
    _cache[name] = entry
    return entry


# ------------------------------------------------------------------ probes
def probe_cognee(force: bool = False) -> dict:
    if not (config.USE_REAL_COGNEE and config.cognee_ready()):
        return {"configured": False, "reachable": False,
                "detail": "SAATHI_REAL_COGNEE off or credentials missing"}
    cached = _cached("cognee")
    if cached and not force:
        return cached
    t0 = time.time()
    try:
        from backend.data import db
        from backend.graph import cards
        from backend.graph.cognee_store import CogneeClient
        datasets = CogneeClient().list_datasets()
        names = [d.get("name") for d in datasets if isinstance(d, dict)]
        # Reachable is not the same as current. Cognee cannot tell you whether
        # what it holds matches the database you are demoing from, and an
        # ingest only ever appends -- so the fingerprint is the only way to
        # know the graph is not answering from a previous generation.
        current = cards.fingerprint()
        ingested = db.meta_get("cognee_fingerprint")
        stale = ingested != current
        detail = f"{len(datasets)} dataset(s)"
        if ingested is None:
            detail += "; never ingested from this database"
        elif stale:
            detail += f"; STALE — holds {ingested}, data is {current}"
        return _store("cognee", configured=True, reachable=True,
                      ms=int((time.time() - t0) * 1000),
                      detail=detail,
                      stale=stale,
                      fingerprint_data=current, fingerprint_ingested=ingested,
                      dataset_present=config.COGNEE_DATASET in names)
    except Exception as exc:                          # noqa: BLE001
        return _store("cognee", configured=True, reachable=False,
                      ms=int((time.time() - t0) * 1000),
                      detail=f"{type(exc).__name__}: {str(exc)[:160]}")


def probe_n8n(force: bool = False) -> dict:
    if not config.USE_REAL_N8N:
        return {"configured": False, "reachable": False,
                "detail": "SAATHI_REAL_N8N off"}
    cached = _cached("n8n")
    if cached and not force:
        return cached
    t0 = time.time()
    try:
        import httpx
        r = httpx.get(f"{config.N8N_BASE_URL}/api/v1/workflows",
                      headers={"X-N8N-API-KEY": config.N8N_API_KEY},
                      timeout=PROBE_TIMEOUT)
        if r.status_code >= 400:
            return _store("n8n", configured=True, reachable=False,
                          ms=int((time.time() - t0) * 1000),
                          detail=f"HTTP {r.status_code}")
        data = r.json().get("data", [])
        active = [w.get("name") for w in data if w.get("active")]
        return _store("n8n", configured=True, reachable=True,
                      ms=int((time.time() - t0) * 1000),
                      detail=f"{len(active)} of {len(data)} workflow(s) active",
                      active_workflows=active,
                      # n8n Cloud cannot reach a laptop directly; without a
                      # public callback URL its workflows run and then fail to
                      # report back, which looks like a hang on stage.
                      callback_url=config.PUBLIC_API_URL or None,
                      callback_configured=bool(config.PUBLIC_API_URL))
    except Exception as exc:                          # noqa: BLE001
        return _store("n8n", configured=True, reachable=False,
                      ms=int((time.time() - t0) * 1000),
                      detail=f"{type(exc).__name__}: {str(exc)[:160]}")


def probe_llm(force: bool = False) -> dict:
    """Not actively probed: a probe costs a token spend on every /ops load.

    So this reports configuration, and `serving` upgrades it to live once an
    answer has actually come back from a provider. scripts/check_integrations.py
    does the real tool-calling probe.
    """
    from backend.reasoning import providers
    order = providers.order()
    return {"configured": bool(config.USE_REAL_LLM and order),
            "reachable": None,                 # unknown until an answer lands
            "detail": " -> ".join(order) if order else "no provider configured",
            "provider_order": order}


def probe_sarvam() -> dict:
    """Also not actively probed: an STT probe needs an audio payload and a TTS
    probe spends credit. /api/stt and /api/tts report per call, and `serving`
    picks that up."""
    ready = config.USE_REAL_SARVAM and config.sarvam_ready()
    return {"configured": bool(ready), "reachable": None if ready else False,
            "detail": (f"{config.SARVAM_STT_MODEL} / {config.SARVAM_TTS_MODEL}"
                       if ready else "browser speech")}


def snapshot(force: bool = False) -> dict:
    """Everything /ops needs to colour its adapter chips honestly."""
    live = serving()
    adapters = {
        "graph": {**probe_cognee(force), "declared": config.adapters()["graph"],
                  "serving": live.get("graph", "unknown")},
        "orchestrator": {**probe_n8n(force),
                         "declared": config.adapters()["orchestrator"],
                         "serving": live.get("orchestrator", "unknown")},
        "reasoner": {**probe_llm(force), "declared": config.adapters()["reasoner"],
                     "serving": live.get("reasoner", "unknown")},
        "voice": {**probe_sarvam(), "declared": config.adapters()["voice"],
                  "serving": live.get("voice", "unknown")},
        "campaign_api": {"configured": False, "reachable": False,
                         "declared": "simulated", "serving": "simulated",
                         "detail": "no real campaign API exists; every execution "
                                   "is simulated and labelled"},
    }
    for name, a in adapters.items():
        a["state"] = _state(name, a)
    return {"adapters": adapters, "checked_at": time.time()}


def _state(name: str, a: dict) -> str:
    """live | degraded | configured | fallback | simulated

    `degraded` is the one worth having: configured, credentials present, and
    NOT answering. That is the state the old chip rendered as a confident
    green `cognee`. `configured` is the honest middle: set up, not probed, and
    nothing has come through it yet this session.
    """
    if name == "campaign_api":
        return "simulated"
    if not a.get("configured"):
        return "fallback"
    serving_now = a.get("serving") or "unknown"
    declared = a.get("declared") or ""
    if a.get("reachable") is False:
        return "degraded"
    if a.get("stale"):
        # reachable, and answering from a previous generation of the ledger
        return "degraded"
    if serving_now != "unknown":
        return "live" if declared in serving_now else "degraded"
    return "live" if a.get("reachable") else "configured"
