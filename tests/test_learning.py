"""CRITICAL. Action -> outcome -> write-back -> a different future answer."""
import config
from backend.actions import machine
from backend.actions.orchestrator import LocalOrchestrator
from backend.actions.writeback import cohort_snapshot
from backend.reasoning import engine

import pytest

PEER = config.PEER_MERCHANT


@pytest.fixture(autouse=True)
def _clean_registry():
    machine.clear()      # no stale running campaign from another test
    yield
    machine.clear()


def _run_once(force="recovered"):
    r = engine.ask(config.DEMO_MERCHANT, "offer bana de")
    assert r["action_proposal"], "no proposal was produced"
    run = machine.propose(config.DEMO_MERCHANT, r["action_proposal"])
    o = LocalOrchestrator()
    run = o.execute(run, lambda e: None)
    assert run.state == "running", run.history
    return o.measure(run, lambda e: None, force=force)


def test_outcome_changes_a_future_answer():
    before = cohort_snapshot(PEER, "evening_decline", "evening_offer")
    run = _run_once("recovered")
    assert run.state == "learned"
    after = cohort_snapshot(PEER, "evening_decline", "evening_offer")
    assert after["tried"] == before["tried"] + 1
    assert after["worked"] == before["worked"] + 1
    assert after["success_rate"] != before["success_rate"]


def test_a_failure_lowers_the_success_rate():
    before = cohort_snapshot(PEER, "evening_decline", "evening_offer")
    run = _run_once("no_change")
    after = cohort_snapshot(PEER, "evening_decline", "evening_offer")
    assert after["tried"] == before["tried"] + 1
    assert after["worked"] == before["worked"]
    assert after["success_rate"] < before["success_rate"]


def test_the_action_has_a_situation_so_the_graph_can_match_it():
    r = engine.ask(config.DEMO_MERCHANT, "offer bana de")
    run = machine.propose(config.DEMO_MERCHANT, r["action_proposal"])
    assert run.situation_id is not None


def test_every_hackathon_outcome_is_labelled_simulated():
    run = _run_once("recovered")
    assert run.outcome.simulated is True
