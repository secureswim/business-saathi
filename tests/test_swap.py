"""Fake and real implementations must agree on shape and on cohort membership.

When this is green, switching an adapter is a config edit made with confidence
rather than a gamble. The Cognee half is skipped unless SAATHI_REAL_COGNEE=1.
"""
import pytest

import config
from backend.actions import machine
from backend.actions.orchestrator import LocalOrchestrator
from backend.graph.sqlite_store import SqliteGraph
from backend.reasoning import validator
from backend.reasoning.templates import TemplateReasoner
from backend.reasoning import engine
from backend.voice.adapter import BrowserVoice, SarvamVoice

QUESTIONS = ["sales kyun kam hain", "mere jaise shops mein kya chal raha hai",
             "agle hafte ke liye kya prepare karun", "30% discount doon?",
             "offer bana de", "paisa theek rahega", "business kaisa chal raha hai"]

EVIDENCE_KEYS = {"tool", "value", "basis", "source", "tier", "available", "ask",
                 "ask_subject"}


def test_evidence_envelope_is_stable_across_every_question():
    for q in QUESTIONS:
        r = engine.ask(config.DEMO_MERCHANT, q)
        for ev in r["evidence"]:
            assert set(ev) == EVIDENCE_KEYS, (q, ev["tool"])
            assert ev["source"] in ("own_data", "graph", "finance", "merchant_input",
                                    "integration", "router")
            assert ev["tier"] in ("A", "B", "C")


def test_the_offline_reasoner_passes_the_grounding_bar():
    """The offline path is held to exactly the same bar as the agent. It is
    allowed to be simpler; it is not allowed to be wrong."""
    r = engine.ask(config.DEMO_MERCHANT, "sales kyun kam hain")
    evidence = r["evidence"]
    out = TemplateReasoner().synthesize("sales_diagnosis", "sales kyun kam hain", evidence)
    ok, unbacked = validator.validate(out["hinglish"], evidence)
    assert ok, unbacked


def test_voice_adapters_share_a_contract():
    for v in (BrowserVoice(), SarvamVoice()):
        assert hasattr(v, "transcribe") and hasattr(v, "speak")
        assert isinstance(v.server_side, bool)


def test_orchestrators_produce_the_same_transitions():
    machine.clear()
    r = engine.ask(config.DEMO_MERCHANT, "offer bana de")
    run = machine.propose(config.DEMO_MERCHANT, r["action_proposal"])
    run = LocalOrchestrator().execute(run, lambda e: None)
    assert [h["state"] for h in run.history] == ["proposed", "validating", "running"]
    machine.clear()


@pytest.mark.skipif(not (config.USE_REAL_COGNEE and config.cognee_ready()),
                    reason="Cognee is off or not configured")
def test_cognee_returns_the_same_cohort_as_the_reference():
    from backend.graph.cognee_store import CogneeGraph
    a = SqliteGraph().peers(config.DEMO_MERCHANT)["value"]
    b = CogneeGraph().peers(config.DEMO_MERCHANT)["value"]
    assert a["peer_ids"] == b["peer_ids"]
    assert a["cohort_size"] == b["cohort_size"]


@pytest.mark.skipif(not (config.USE_REAL_COGNEE and config.cognee_ready()),
                    reason="Cognee is off or not configured")
def test_cognee_playbook_counts_match_the_ledger():
    from backend.graph.cognee_store import CogneeGraph
    ids = SqliteGraph().peers(config.DEMO_MERCHANT)["value"]["peer_ids"]
    a = SqliteGraph().peer_playbook("evening_decline", ids)["value"]["best"]
    b = CogneeGraph().peer_playbook("evening_decline", ids)["value"]["best"]
    assert (a["tried"], a["worked"]) == (b["tried"], b["worked"])
