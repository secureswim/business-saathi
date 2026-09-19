"""Action proposal, run and outcome. The second shared contract."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

STATES = [
    "proposed", "expired", "rejected", "validating", "failed",
    "running", "measuring", "outcome_unavailable", "learned",
]
TERMINAL = {"expired", "rejected", "learned", "outcome_unavailable"}


@dataclass
class ActionProposal:
    type: str
    params: dict[str, Any]
    condition: str                      # the situation kind this responds to
    evidence_summary: str               # "5 of 6 similar merchants recovered"
    situation_id: int | None = None
    guardrails_passed: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Outcome:
    before: float
    after: float
    delta_pct: float
    verdict: str
    simulated: bool = True
    forced: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActionRun:
    merchant_id: str
    type: str
    params: dict[str, Any]
    condition: str
    evidence_summary: str
    situation_id: int | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = "proposed"
    history: list[dict] = field(default_factory=list)
    action_id: int | None = None
    outcome: Outcome | None = None
    created_at: str = ""
    orchestrator: str = ""

    def advance(self, state: str, note: str) -> None:
        assert state in STATES, f"unknown state {state}"
        self.state = state
        self.history.append({"state": state, "note": note})

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def to_dict(self) -> dict:
        d = asdict(self)
        d["outcome"] = self.outcome.to_dict() if self.outcome else None
        return d
