"""Guardrails, checked before anything executes.

A violation is never silent: the merchant hears why, and gets a revised
proposal. This is the product talking a merchant out of a bad idea, and it is
a demo beat.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.analytics import money  # noqa: E402
from backend.data import repository as repo  # noqa: E402

BOUNDS = {
    "discount_rs": (5, 20),
    "discount_pct": (0, 20),     # the 25%+ bucket fails ~83% of the time in the data
    "days": (1, 7),
    "increase_rs": (0, 15),
    "increase_pct": (0, 60),
}


def validate(run, live_runs: list) -> dict:
    """Returns {ok, reason, violations[], revised_params}."""
    violations, revised = [], dict(run.params)

    for key, (lo, hi) in BOUNDS.items():
        if key not in run.params:
            continue
        val = run.params[key]
        if not isinstance(val, (int, float)):
            continue
        if val < lo or val > hi:
            violations.append({
                "param": key, "value": val, "bound": [lo, hi],
                "reason": f"{key}={val} is outside the evidence-supported range {lo}-{hi}",
            })
            revised[key] = min(max(val, lo), hi)

    # no overlapping campaign: two concurrent actions make the outcome unattributable
    overlapping = [r for r in live_runs
                   if r.merchant_id == run.merchant_id
                   and r.run_id != run.run_id
                   and r.state in ("running", "measuring", "validating")]
    if overlapping:
        violations.append({
            "param": "overlap", "value": overlapping[0].run_id, "bound": None,
            "reason": "another campaign is already running for this merchant",
        })

    # money check, only when obligations are actually known (tier B)
    money_note = None
    if repo.has_any_obligations(run.merchant_id):
        cost = float(run.params.get("cost_estimate", 0) or 0)
        if cost > 0:
            chk = money.purchase_check(run.merchant_id, cost)["value"]
            if chk.get("scope") == "net_position" and chk.get("verdict") == "unsafe":
                violations.append({
                    "param": "cost_estimate", "value": cost, "bound": None,
                    "reason": "stressed buffer goes negative with this spend",
                })
            money_note = chk

    ok = not violations
    return {
        "ok": ok,
        "reason": ("parameters within guardrails, no overlapping campaign" if ok
                   else "; ".join(v["reason"] for v in violations)),
        "violations": violations,
        "revised_params": revised if violations else None,
        "money_check": money_note,
        "bounds": BOUNDS,
        "min_cohort_size": config.MIN_COHORT_SIZE,
    }
