"""Action outcome measurement.

Step 2 below is SIMULATED and labelled everywhere it appears: the campaign days
are synthesised into the ledger because there is no real world in which a
campaign ran. Steps 1, 3 and 4 are genuine computation over the ledger -- they
read rows, not the factor that generated them.
"""
from __future__ import annotations

import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.data import db, repository as repo  # noqa: E402

VERDICTS_GOOD = {"recovered", "sustained", "captured_festive"}


def _window_hours(params: dict) -> tuple[int, int]:
    win = params.get("window", "18-21")
    try:
        lo, hi = (int(x) for x in win.split("-"))
        return lo, hi
    except Exception:      # noqa: BLE001
        return 18, 21


def measure(merchant_id: str, params: dict, run_id: str, action_type: str,
            cohort_success_rate: float | None = None,
            force: str | None = None) -> dict:
    """Synthesise the campaign days (simulated), then measure what the rows say."""
    today = db.today()
    days = int(params.get("days", 3))
    lo, hi = _window_hours(params)
    metric = "evening_gmv" if "evening" in action_type else "daily_gmv"

    # 1. establish `before` from the 12 days preceding the action
    before_amt, _, before_days = repo.window_totals(
        merchant_id, today - timedelta(days=12), today - timedelta(days=1), (lo, hi))
    before = before_amt / before_days if before_days else 0.0

    # 2. SIMULATED: write the campaign days. The uplift is sampled from the
    #    cohort's observed outcome distribution, seeded per run so a reset and
    #    replay is reproducible.
    rate = cohort_success_rate if cohort_success_rate is not None else 0.70
    rng = random.Random(f"{run_id}:{merchant_id}")
    if force == "recovered":
        success = True
    elif force in ("no_change", "worse"):
        success = False
    else:
        success = rng.random() < rate
    factor = rng.uniform(1.20, 1.40) if success else rng.uniform(0.94, 1.06)

    n_hours = max(1, hi - lo + 1)
    per_hour = before / n_hours          # `before` is a per-DAY total for this window
    # The synthesised rows must carry this merchant's OWN average ticket. A
    # hardcoded 42 is a chai-stall ticket, and using it for a pharmacy invented
    # five times the transaction count the shop actually does.
    ticket = repo.recent_avg_ticket(merchant_id) or 42.0
    rows = []
    for i in range(days):
        d = today + timedelta(days=i)
        for hour in range(lo, hi + 1):
            amt = per_hour * factor * rng.uniform(0.92, 1.08)
            rows.append((merchant_id, d.isoformat(), hour,
                         max(1, int(round(amt / ticket))), round(amt, 2)))
    repo.insert_txn_hourly(rows)

    # 3. measure `after` from the rows just written, same query shape as `before`
    after_amt, _, after_days = repo.window_totals(
        merchant_id, today, today + timedelta(days=days - 1), (lo, hi))
    after = after_amt / after_days if after_days else 0.0

    # 4. classify
    delta = (after - before) / before * 100.0 if before else 0.0
    if delta > 8:
        verdict = "recovered" if "evening" in action_type else "sustained"
    elif delta > -5:
        verdict = "no_change"
    else:
        verdict = "worse"

    return {
        "value": {"before": round(before, 2), "after": round(after, 2),
                  "delta_pct": round(delta, 1), "verdict": verdict, "metric": metric,
                  "simulated": True, "forced": force is not None,
                  "days_measured": after_days},
        "basis": {"source": "analytics.outcome.measure",
                  "before_window": [(today - timedelta(days=12)).isoformat(),
                                    (today - timedelta(days=1)).isoformat()],
                  "after_window": [today.isoformat(),
                                   (today + timedelta(days=days - 1)).isoformat()],
                  "hours": [lo, hi],
                  "simulation": "campaign days synthesised; uplift sampled from the "
                                "cohort's observed outcome distribution",
                  "cohort_success_rate": rate,
                  "classification": "recovered >+8% | no_change -5..+8% | worse <-5%"},
    }


def action_history(merchant_id: str, limit: int = 3) -> dict:
    rows = repo.action_history(merchant_id, limit)
    if not rows:
        return {"value": {"available": False, "reason": "no prior actions"},
                "basis": {"source": "analytics.outcome.action_history"}}
    return {
        "value": {"available": True, "count": len(rows),
                  "actions": [{"type": r["type"], "params": r["params"],
                               "started_on": r["started_on"], "verdict": r["verdict"],
                               "delta_pct": r["delta_pct"],
                               "simulated": bool(r["simulated"]) if r["simulated"] is not None else None}
                              for r in rows]},
        "basis": {"source": "analytics.outcome.action_history", "limit": limit},
    }
