"""Endpoints: happy paths, validation, error codes, and the event sequence."""
import json
import base64

import pytest
from fastapi.testclient import TestClient

import config
from backend.api.main import app

client = TestClient(app)


def test_health_and_config():
    h = client.get("/api/health").json()
    assert h["ok"] is True
    c = client.get("/api/config").json()
    assert c["adapters"]["campaign_api"] == "simulated"
    assert c["min_cohort_size"] == config.MIN_COHORT_SIZE


def test_callback_tunnel_rejects_public_requests_and_accepts_n8n_secret():
    tunneled = {"host": config.TUNNEL_HOST_HEADER}
    assert client.get("/api/health", headers=tunneled).status_code == 403
    allowed = {**tunneled, "X-Saathi-Secret": config.INTERNAL_SECRET}
    assert client.get("/api/health", headers=allowed).status_code == 200


def test_hosted_demo_password_covers_pages_api_and_websocket(monkeypatch):
    monkeypatch.setattr(config, "SITE_PASSWORD", "test-demo-password")
    private = TestClient(app)
    assert private.get("/api/health").status_code == 200
    assert private.get("/").status_code == 401
    assert private.get("/ops").status_code == 401
    assert private.post("/api/admin/reset").status_code == 401
    assert private.get("/api/merchants").status_code == 401
    assert private.get("/static/ops.js").status_code == 401
    assert private.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401
    with pytest.raises(Exception):
        with private.websocket_connect("/ws"):
            pass

    credentials = base64.b64encode(b"judge:test-demo-password").decode()
    response = private.get("/", headers={"Authorization": f"Basic {credentials}"})
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert private.get("/ops").status_code == 200
    assert private.get("/api/merchants").status_code == 200
    with private.websocket_connect("/ws") as socket:
        socket.send_text("ping")

    callback = {"X-Saathi-Secret": config.INTERNAL_SECRET}
    response = private.post("/api/internal/validate-action", json={"run_id": "missing"},
                            headers=callback)
    assert response.status_code == 404


def test_query_happy_path():
    r = client.post("/api/query", json={"merchant_id": "M001",
                                        "text": "sales kyun kam hain"})
    assert r.status_code == 200
    d = r.json()
    assert d["intent"] == "sales_diagnosis"
    assert d["answer"]["validator"]["passed"] is True
    assert d["evidence"]


def test_query_validation_errors():
    assert client.post("/api/query", json={"text": ""}).status_code == 400
    assert client.post("/api/query", json={"text": "x" * 501}).status_code == 400
    assert client.post("/api/query", json={"merchant_id": "NOPE",
                                           "text": "hi"}).status_code == 404


def test_unknown_run_is_404_and_double_decide_is_409():
    assert client.post("/api/action/nope/approve").status_code == 404
    d = client.post("/api/query", json={"merchant_id": "M001",
                                        "text": "offer bana de"}).json()
    run_id = d["run_id"]
    assert client.post(f"/api/action/{run_id}/reject").status_code == 200
    assert client.post(f"/api/action/{run_id}/approve").status_code == 409


def test_internal_routes_require_the_secret():
    r = client.post("/api/internal/validate-action", json={"run_id": "x"})
    assert r.status_code == 403
    r = client.post("/api/internal/validate-action", json={"run_id": "x"},
                    headers={"X-Saathi-Secret": config.INTERNAL_SECRET})
    assert r.status_code == 404      # authorised, run simply does not exist


def test_simulate_campaign_is_idempotent_and_labelled():
    h = {"X-Saathi-Secret": config.INTERNAL_SECRET}
    a = client.post("/api/internal/simulate-campaign", json={"run_id": "zz"}, headers=h).json()
    b = client.post("/api/internal/simulate-campaign", json={"run_id": "zz"}, headers=h).json()
    assert a["simulated"] is True
    assert b.get("replayed") is True
    assert a["campaign_id"] == b["campaign_id"]


def test_context_stores_and_reruns():
    r = client.post("/api/context", json={
        "merchant_id": "M001", "kind": "stock_estimate", "subject": "cold drink",
        "utterance": "bees pachees bottle",
        "rerun": "cold drink ka stock dekh kar agle hafte ke liye kya prepare karun"}).json()
    assert r["stored"] and r["value_num"] == 22.5
    assert "aapne bataya" in r["recomputed"]["answer"]["hinglish"]


def test_merchants_and_graph_reads():
    m = client.get("/api/merchants").json()
    assert len(m["merchants"]) > 100
    g = client.get("/api/graph/M001").json()
    assert g["cohort_size"] >= config.MIN_COHORT_SIZE
    assert all("components" in p for p in g["peers"])


def test_websocket_accepts_a_connection():
    with client.websocket_connect("/ws") as sock:
        assert sock is not None


def test_the_event_sequence_is_emitted_and_persisted():
    """Every event is written to `events`, which is what makes /ops replayable."""
    d = client.post("/api/query", json={"merchant_id": "M001",
                                        "text": "sales kyun kam hain"}).json()
    events = client.get(f"/api/evidence/{d['query_id']}").json()["events"]
    seen = [e["type"] for e in events]
    for expected in ("query_started", "intent_detected", "context_loaded",
                     "tool_started", "tool_result", "evidence_complete",
                     "reasoning_started", "validation_result", "response_ready"):
        assert expected in seen, (expected, seen)
    # the pipeline is emitted in order
    assert seen.index("query_started") < seen.index("intent_detected")
    assert seen.index("evidence_complete") < seen.index("response_ready")


def test_pages_render():
    for path in ("/", "/ops", "/static/tokens.css", "/static/ops.js",
                 "/static/merchant.js"):
        assert client.get(path).status_code == 200


# --------------------------------------------------------------------- voice
# These two endpoints exist so the merchant page can use Sarvam without the
# page ever discovering whether Sarvam is on.
#
# The adapter is chosen by .env, which changes from machine to machine and over
# the course of a demo. A test that assumed "browser" passed while the flag was
# off and failed the moment Sarvam was switched on -- testing the operator's
# configuration rather than the code. So: force the adapter you mean to test,
# and assert separately the contract that must hold under EITHER adapter.
@pytest.fixture
def browser_voice(monkeypatch):
    from backend.voice.adapter import BrowserVoice
    monkeypatch.setattr("backend.api.public.get_voice", lambda: BrowserVoice())


def test_tts_reports_browser_handling_without_sarvam(browser_voice):
    r = client.post("/api/tts", json={"text": "namaste"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["handled_by"] == "browser"


def test_tts_rejects_empty_text_without_failing_the_request():
    d = client.post("/api/tts", json={"text": "   "}).json()
    assert d["ok"] is False and d["error"] == "empty_text"


def test_stt_reports_browser_handling_without_sarvam(browser_voice):
    r = client.post("/api/stt", files={"audio": ("a.wav", b"RIFFfake", "audio/wav")})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["handled_by"] == "browser"


def test_voice_endpoints_never_return_an_error_status_whatever_the_adapter():
    """The page falls back on the BODY, not the status code.

    Whether Sarvam is on or off, a 4xx/5xx here would make merchant.js treat a
    routine fallback as a broken request. This is the invariant that actually
    protects the merchant, and it must hold with the real adapter too.
    """
    for response in (client.post("/api/tts", json={"text": "namaste"}),
                     client.post("/api/stt",
                                 files={"audio": ("a.wav", b"RIFFfake", "audio/wav")})):
        assert response.status_code == 200, response.text
        assert isinstance(response.json().get("ok"), bool)


def test_stt_rejects_empty_audio():
    d = client.post("/api/stt", files={"audio": ("a.wav", b"", "audio/wav")}).json()
    assert d["ok"] is False and d["error"] == "no_audio"


def test_stt_does_not_answer_the_question_itself(browser_voice):
    """/api/stt returns text only.

    The page routes that text through submit(), so "haan" after a proposal is
    read as an approval rather than as a new question. If this endpoint ever
    starts returning an `answer`, that branch is silently bypassed.
    """
    d = client.post("/api/stt", files={"audio": ("a.wav", b"RIFFfake", "audio/wav")}).json()
    assert "answer" not in d and "run_id" not in d


def test_every_event_the_api_emits_is_a_registered_event():
    """events.py is a closed contract; the ops canvas only renders what's in it.

    /api/tts once emitted `tts_complete`, which is not a registered name, so
    the assert in event() turned a working Sarvam call into a 500 AFTER the
    audio had already been paid for and generated.
    """
    import re
    from pathlib import Path
    from backend.models.events import ALL_EVENTS

    api_dir = Path(__file__).resolve().parents[1] / "backend" / "api"
    emitted = set()
    for path in api_dir.glob("*.py"):
        # the lookbehind skips FastAPI's own @app.on_event("startup")
        emitted |= set(re.findall(r'(?<!on_)event\(\s*["\']([a-z_]+)["\']',
                                  path.read_text()))
    unknown = emitted - set(ALL_EVENTS)
    assert not unknown, f"unregistered event name(s) emitted by the API: {unknown}"
