"""Orchestrator interface and the local (fake) implementation.

The local state machine implements exactly the transitions n8n drives, emits
exactly the same events, and is the default -- so this path is exercised every
day of development rather than being emergency code discovered on stage.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.actions import guardrails, machine, writeback  # noqa: E402
from backend.analytics import outcome as outcome_engine  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402
from backend.graph.store import get_store  # noqa: E402
from backend.models.action import Outcome  # noqa: E402
from backend.models.events import event  # noqa: E402


class Orchestrator(ABC):
    name = "abstract"

    @abstractmethod
    def execute(self, run, emit) -> object: ...

    @abstractmethod
    def measure(self, run, emit, force: str | None = None) -> object: ...


class LocalOrchestrator(Orchestrator):
    """The reference implementation. Identical transitions to workflow 1 and 3."""
    name = "state_machine"

    def execute(self, run, emit):
        emit(event("workflow_started", None, run_id=run.run_id,
                   orchestrator=self.name, workflow="action_execution"))
        run.orchestrator = self.name

        # node: validate
        run.advance("validating", "checking parameters against cohort-supported bounds")
        emit(event("workflow_node", None, run_id=run.run_id, node="validate",
                   status="running", attempt=1))
        check = guardrails.validate(run, machine.live_runs())
        if not check["ok"]:
            run.advance("failed", check["reason"])
            emit(event("workflow_node", None, run_id=run.run_id, node="validate",
                       status="failed", attempt=1, detail=check["reason"]))
            return run
        emit(event("workflow_node", None, run_id=run.run_id, node="validate",
                   status="success", attempt=1, detail=check["reason"]))

        # node: create campaign -- SIMULATED external call
        emit(event("workflow_node", None, run_id=run.run_id, node="create campaign",
                   status="success", attempt=1, simulated=True,
                   detail="simulated Paytm campaign; no real campaign is created"))

        # node: log action in the ledger and the graph
        today = db.today()
        end = today + timedelta(days=int(run.params.get("days", 3)) - 1)
        run.action_id = get_store().record_action(
            run.merchant_id, run.type, run.params, run.situation_id,
            today.isoformat(), end.isoformat(), run.run_id)
        emit(event("workflow_node", None, run_id=run.run_id, node="record action",
                   status="success", attempt=1, detail=f"action {run.action_id}"))

        run.advance("running", f"campaign live {today} to {end}, "
                               f"logged as action {run.action_id}")
        return run

    def measure(self, run, emit, force: str | None = None):
        emit(event("workflow_started", None, run_id=run.run_id,
                   orchestrator=self.name, workflow="outcome_learning"))
        run.advance("measuring", "writing the campaign window and measuring the result")
        emit(event("workflow_node", None, run_id=run.run_id, node="measure",
                   status="running", attempt=1, simulated=True))

        store = get_store()
        peers = store.peers(run.merchant_id)["value"]
        cohort_key = peers.get("cohort_key", "")
        pb = store.peer_playbook(run.condition, peers["peer_ids"])["value"]
        rate = None
        if pb.get("best"):
            rate = pb["best"]["success_rate"] / 100.0

        before_snapshot = writeback.cohort_snapshot(run.merchant_id, run.condition, run.type)

        res = outcome_engine.measure(run.merchant_id, run.params, run.run_id, run.type,
                                     cohort_success_rate=rate, force=force)["value"]
        if res["before"] <= 0:
            run.advance("outcome_unavailable", "no baseline to measure against")
            emit(event("workflow_node", None, run_id=run.run_id, node="measure",
                       status="failed", attempt=1, detail="no baseline"))
            return run

        run.outcome = Outcome(before=res["before"], after=res["after"],
                              delta_pct=res["delta_pct"], verdict=res["verdict"],
                              simulated=True, forced=res["forced"])
        emit(event("workflow_node", None, run_id=run.run_id, node="measure",
                   status="success", attempt=1, simulated=True,
                   detail=f"{res['verdict']} {res['delta_pct']:+.1f}%"))
        emit(event("outcome_measured", None, run_id=run.run_id, **res))

        # node: graph write-back
        emit(event("workflow_node", None, run_id=run.run_id, node="graph writeback",
                   status="running", attempt=1))
        wb = writeback.write_outcome(run, cohort_key)
        emit(event("workflow_node", None, run_id=run.run_id, node="graph writeback",
                   status="success", attempt=1,
                   detail=wb.get("detail", "written to the ledger")))
        emit(event("graph_writeback", None, run_id=run.run_id, store=wb["store"],
                   outcome_rows=wb.get("outcome_rows", 0),
                   pattern_rows=wb.get("pattern_rows", 0),
                   cards_submitted=wb.get("cards_submitted", 0),
                   detail=wb.get("detail", ""),
                   cohort_key=cohort_key, degraded=wb.get("degraded", False)))

        after_snapshot = writeback.cohort_snapshot(run.merchant_id, run.condition, run.type)
        run.advance("learned", f"outcome {res['verdict']} ({res['delta_pct']:+.1f}%) "
                               f"written back to the graph")
        emit(event("learning_complete", None, run_id=run.run_id, cohort_key=cohort_key,
                   situation_kind=run.condition, action_type=run.type,
                   before=before_snapshot, after=after_snapshot))
        return run


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        if config.USE_REAL_N8N:
            from backend.actions.n8n_orchestrator import N8nOrchestrator
            _orchestrator = N8nOrchestrator()
        else:
            _orchestrator = LocalOrchestrator()
    return _orchestrator


def reset_orchestrator() -> None:
    global _orchestrator
    _orchestrator = None
