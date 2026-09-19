"""Money. The honesty test of the whole system.

Payment INFLOW is observable (tier A). A cash POSITION is not, unless
obligations are known through an integration or the merchant stated them
(tier B/C). `cash_view` therefore returns one of two scopes and the spoken
wording changes with it. It never says "cash position" when it can only see
money coming in.
"""
from __future__ import annotations

import statistics
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.analytics.patterns import demand_forecast  # noqa: E402
from backend.data import context, db, repository as repo  # noqa: E402


def revenue_projection(merchant_id: str, days: int = 7) -> dict:
    """Money coming in. Tier A -- always available with enough history."""
    f = demand_forecast(merchant_id, days)
    if not f["value"].get("available"):
        return f
    v = f["value"]
    return {
        "value": {"available": True, "days": days,
                  "revenue_low": v["total_low"], "revenue_expected": v["total_expected"],
                  "revenue_high": v["total_high"]},
        "basis": {**f["basis"], "source": "analytics.revenue_projection"},
    }


def obligation_projection(merchant_id: str, days: int = 7) -> dict:
    """Money going out. Tier B/C -- only when rows exist. Absence is a result."""
    today = db.today()
    rows = repo.obligations(merchant_id, today, today + timedelta(days=days))
    stated = context.get(merchant_id, "upcoming_expense")

    items = [{"due_on": r["due_on"], "amount": r["amount"], "label": r["label"],
              "source": r["source"]} for r in rows]
    if stated and stated.get("value_num"):
        items.append({"due_on": stated["stated_at"][:10], "amount": stated["value_num"],
                      "label": stated.get("subject") or "merchant stated",
                      "source": "merchant_stated"})

    if not items:
        return {
            "value": {"available": False,
                      "reason": "no connected financial data and nothing stated"},
            "basis": {"source": "analytics.obligation_projection",
                      "window": [today.isoformat(), (today + timedelta(days=days)).isoformat()]},
        }
    return {
        "value": {"available": True, "days": days,
                  "total": round(sum(i["amount"] for i in items), 0), "items": items},
        "basis": {"source": "analytics.obligation_projection",
                  "window": [today.isoformat(), (today + timedelta(days=days)).isoformat()],
                  "tier": "B" if rows else "C", "count": len(items)},
    }


def cash_view(merchant_id: str, days: int = 7) -> dict:
    """Combined position when obligations are known; inflow-only when they are not."""
    rev = revenue_projection(merchant_id, days)
    if not rev["value"].get("available"):
        return rev
    ob = obligation_projection(merchant_id, days)
    rv = rev["value"]

    if not ob["value"].get("available"):
        return {
            "value": {
                "available": True, "scope": "inflow_only", "days": days,
                "obligations_available": False,
                "revenue_low": rv["revenue_low"], "revenue_expected": rv["revenue_expected"],
                "revenue_high": rv["revenue_high"],
            },
            "basis": {"source": "analytics.cash_view",
                      "note": "obligations not connected; this is incoming business only",
                      "revenue_basis": rev["basis"], "obligation_basis": ob["basis"]},
        }

    ov = ob["value"]
    return {
        "value": {
            "available": True, "scope": "net_position", "days": days,
            "obligations_available": True,
            "revenue_low": rv["revenue_low"], "revenue_expected": rv["revenue_expected"],
            "revenue_high": rv["revenue_high"],
            "obligations_due": ov["total"],
            "net_expected": round(rv["revenue_expected"] - ov["total"], 0),
            "net_low": round(rv["revenue_low"] - ov["total"], 0),
            "obligation_items": ov["items"],
        },
        "basis": {"source": "analytics.cash_view",
                  "obligation_source": ob["basis"].get("tier"),
                  "revenue_basis": rev["basis"], "obligation_basis": ob["basis"]},
    }


def purchase_check(merchant_id: str, amount: float, days: int = 7) -> dict:
    """Is spending this safe? Only answerable as a net question when obligations exist."""
    cv = cash_view(merchant_id, days)
    if not cv["value"].get("available"):
        return cv
    v = cv["value"]

    if v["scope"] == "inflow_only":
        return {
            "value": {"available": True, "scope": "inflow_only", "purchase": round(amount, 0),
                      "revenue_expected": v["revenue_expected"],
                      "covers_from_inflow": v["revenue_low"] >= amount,
                      "verdict": "unknown_without_expenses"},
            "basis": {**cv["basis"], "source": "analytics.purchase_check",
                      "note": "cannot judge safety without knowing outgoings"},
        }

    expected = v["net_expected"] - amount
    stressed = v["net_low"] - amount
    verdict = ("unsafe" if stressed < 0
               else "tight" if stressed < amount * 0.35 else "comfortable")

    safe_after = None
    if verdict == "unsafe":
        for extra in range(days + 1, 31):
            probe = cash_view(merchant_id, extra)["value"]
            if probe.get("scope") == "net_position" and probe["net_low"] - amount >= 0:
                safe_after = extra
                break

    return {
        "value": {"available": True, "scope": "net_position", "purchase": round(amount, 0),
                  "buffer_expected": round(expected, 0), "buffer_stressed": round(stressed, 0),
                  "verdict": verdict, "days": days, "safe_after_days": safe_after},
        "basis": {**cv["basis"], "source": "analytics.purchase_check",
                  "stress": "revenue at the low edge of the projection band"},
    }
