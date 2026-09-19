"""State machine, guardrails, idempotency."""
import config
from backend.actions import guardrails, machine
from backend.actions.orchestrator import LocalOrchestrator
from backend.models.action import ActionRun

PROPOSAL = {"type": "evening_offer",
            "params": {"discount_rs": 10, "window": "18-21", "days": 3},
            "condition": "evening_decline", "evidence_summary": "5 of 6"}


def test_proposal_starts_in_proposed():
    run = machine.propose(config.DEMO_MERCHANT, PROPOSAL)
    assert run.state == "proposed" and run.history[-1]["state"] == "proposed"


def test_rejection_executes_nothing():
    run = machine.propose(config.DEMO_MERCHANT, PROPOSAL)
    run = machine.reject(run.run_id)
    assert run.state == "rejected" and run.action_id is None


def test_guardrail_rejects_an_out_of_range_discount():
    run = ActionRun(merchant_id=config.DEMO_MERCHANT, type="evening_offer",
                    params={"discount_rs": 60, "days": 3}, condition="evening_decline",
                    evidence_summary="")
    check = guardrails.validate(run, [])
    assert not check["ok"]
    assert check["revised_params"]["discount_rs"] == 20      # clamped, not refused


def test_guardrail_rejects_an_overlapping_campaign():
    a = machine.propose(config.DEMO_MERCHANT, PROPOSAL)
    LocalOrchestrator().execute(a, lambda e: None)
    b = machine.propose(config.DEMO_MERCHANT, PROPOSAL)
    check = guardrails.validate(b, machine.live_runs())
    assert not check["ok"]
    assert any(v["param"] == "overlap" for v in check["violations"])


def test_deep_discount_is_outside_the_supported_range():
    run = ActionRun(merchant_id=config.DEMO_MERCHANT, type="deep_discount",
                    params={"discount_pct": 30, "days": 5},
                    condition="sales_decline", evidence_summary="")
    check = guardrails.validate(run, [])
    assert not check["ok"]
    assert check["revised_params"]["discount_pct"] == 20
