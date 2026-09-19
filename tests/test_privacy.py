"""CRITICAL. A cohort below the floor returns nothing, and no identifier or
absolute figure ever reaches the merchant-facing layer."""
import json

import pytest

import config
from backend.graph import privacy
from backend.graph.sqlite_store import SqliteGraph
from backend.reasoning import engine

g = SqliteGraph()


def test_cohort_below_the_floor_raises():
    with pytest.raises(privacy.CohortTooSmall):
        privacy.check_cohort(["M002", "M003", "M004", "M005"])   # four, floor is five


def test_every_graph_tool_refuses_a_small_cohort():
    small = ["M002", "M003", "M004"]
    for fn in (lambda: g.peer_playbook("evening_decline", small),
               lambda: g.failed_plays("discount", small)):
        r = fn()
        assert r["value"].get("available") is False
        assert str(config.MIN_COHORT_SIZE) in json.dumps(r)


def test_synthesis_filter_strips_identifiers_and_absolute_figures():
    raw = g.peers(config.DEMO_MERCHANT)
    ids = raw["value"]["peer_ids"]
    out = privacy.enforce(raw, ids, privacy.SYNTHESIS)
    blob = json.dumps(out)
    assert "peer_ids" not in blob and "extended_ids" not in blob
    for pid in ids:
        assert f'"{pid}"' not in blob


def test_no_peer_identifier_appears_in_a_spoken_answer():
    r = engine.ask(config.DEMO_MERCHANT, "sales kyun kam hain")
    spoken = r["answer"]["hinglish"] + " " + r["answer"]["english"]
    peers = g.peers(config.DEMO_MERCHANT)["value"]["peer_ids"]
    for pid in peers:
        assert pid not in spoken


def test_evidence_sent_to_synthesis_carries_no_peer_ids():
    r = engine.ask(config.DEMO_MERCHANT, "mere jaise shops mein kya chal raha hai")
    for ev in r["evidence"]:
        if ev["tool"] == "get_peer_cohort":
            assert "peer_ids" not in ev["value"]


def test_llm_payload_omits_ops_provenance(monkeypatch):
    from backend.reasoning import llm
    from backend.models.evidence import Evidence

    captured = {}
    def fake(system, prompt, schema, max_tokens, timeout):
        captured.update(json.loads(prompt))
        return {"hinglish": "Saat similar merchants ka aggregate mila.",
                "english": "An aggregate of seven similar merchants was available.",
                "evidence_refs": ["get_peer_cohort"], "confidence": "high",
                "limitations": [], "_provider": "test"}
    monkeypatch.setattr(llm, "_llm_json", fake)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test")
    evidence = [Evidence("get_peer_cohort", {"cohort_size": 7},
                         {"peers": [{"id": "M004"}]}, "graph")]
    llm.GeminiReasoner().synthesize("peer_insight", "peers?", evidence)
    blob = json.dumps(captured)
    assert "basis" not in blob and "M004" not in blob
