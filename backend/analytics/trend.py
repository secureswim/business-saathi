"""Business health and sales trend. Pure functions over the ledger.

Every function returns {"value": ..., "basis": ...}. `basis` names the rows,
windows and method used, and the grounding validator checks spoken figures
against `value`. No LLM, no graph, no randomness.

Closed days (no rows) are EXCLUDED from means rather than counted as zero --
a closed day is not a bad day, and treating it as one manufactures declines
that did not happen.
"""
from __future__ import annotations

import statistics
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.data import db, repository as repo  # noqa: E402

BANDS = [(7, 9), (10, 12), (13, 15), (16, 18), (19, 21)]


def _band_label(lo: int, hi: int) -> str:
    return f"{lo}-{hi + 1}"


def sales_trend(merchant_id: str, window_days: int = 7, baseline_days: int = 30) -> dict:
    today = db.today()
    cur_start, cur_end = today - timedelta(days=window_days), today - timedelta(days=1)
    base_end = cur_start - timedelta(days=1)
    base_start = base_end - timedelta(days=baseline_days - 1)

    cur_amt, cur_txn, cur_days = repo.window_totals(merchant_id, cur_start, cur_end)
    base_amt, base_txn, base_days = repo.window_totals(merchant_id, base_start, base_end)
    cur_daily = cur_amt / cur_days if cur_days else 0.0
    base_daily = base_amt / base_days if base_days else 0.0
    change = (cur_daily - base_daily) / base_daily * 100.0 if base_daily else 0.0

    bands, worst = [], None
    for lo, hi in BANDS:
        c, _, _ = repo.window_totals(merchant_id, cur_start, cur_end, (lo, hi))
        b, _, _ = repo.window_totals(merchant_id, base_start, base_end, (lo, hi))
        cd = c / cur_days if cur_days else 0.0
        bd = b / base_days if base_days else 0.0
        pct = (cd - bd) / bd * 100.0 if bd else 0.0
        bands.append({"band": _band_label(lo, hi), "now": round(cd, 1),
                      "baseline": round(bd, 1), "delta_pct": round(pct, 1)})
        if worst is None or (cd - bd) < worst["abs"]:
            worst = {"band": _band_label(lo, hi), "abs": cd - bd, "pct": pct}

    return {
        "value": {
            "current_window_days": window_days,
            "baseline_window_days": baseline_days,
            "change_pct": round(change, 1),
            "current_daily": round(cur_daily, 0),
            "baseline_daily": round(base_daily, 0),
            "direction": "down" if change < -2 else ("up" if change > 2 else "flat"),
            "worst_band": worst["band"] if worst else None,
            "worst_band_pct": round(worst["pct"], 1) if worst else None,
            "current_txns_per_day": round(cur_txn / cur_days, 1) if cur_days else 0.0,
        },
        "basis": {
            "source": "analytics.sales_trend",
            "current_window": [cur_start.isoformat(), cur_end.isoformat()],
            "baseline_window": [base_start.isoformat(), base_end.isoformat()],
            "days_observed": [cur_days, base_days],
            "method": "daily mean over observed days; closed days excluded",
            "bands": bands,
        },
    }


def business_health(merchant_id: str, window_days: int = 7) -> dict:
    t = sales_trend(merchant_id, window_days)["value"]
    today = db.today()
    cur_start, cur_end = today - timedelta(days=window_days), today - timedelta(days=1)
    base_end = cur_start - timedelta(days=1)
    base_start = base_end - timedelta(days=29)

    ticket_now = repo.avg_ticket(merchant_id, cur_start, cur_end)
    ticket_base = repo.avg_ticket(merchant_id, base_start, base_end)
    ticket_change = ((ticket_now - ticket_base) / ticket_base * 100.0
                     if ticket_now and ticket_base else None)

    daily = [amt for _, amt, _ in repo.daily_totals(merchant_id, base_start, cur_end)]
    cv = (statistics.pstdev(daily) / statistics.fmean(daily) * 100.0
          if len(daily) > 2 and statistics.fmean(daily) else 0.0)
    pay = repo.payment_health(merchant_id, base_start)

    if t["direction"] == "down" and t["change_pct"] < -12:
        headline = "needs_attention"
    elif t["direction"] == "up" and t["change_pct"] > 10:
        headline = "growing"
    elif cv > 45:
        headline = "erratic"
    else:
        headline = "steady"

    return {
        "value": {
            "headline": headline,
            "change_pct": t["change_pct"],
            "direction": t["direction"],
            "current_daily": t["current_daily"],
            "baseline_daily": t["baseline_daily"],
            "txns_per_day": t["current_txns_per_day"],
            "avg_ticket": round(ticket_now, 0) if ticket_now else None,
            "avg_ticket_change_pct": round(ticket_change, 1) if ticket_change is not None else None,
            "volatility_pct": round(cv, 1),
        },
        "basis": {
            "source": "analytics.business_health",
            "current_window": [cur_start.isoformat(), cur_end.isoformat()],
            "baseline_window": [base_start.isoformat(), base_end.isoformat()],
            "volatility": "coefficient of variation of daily totals over 37 days",
            "payment_health": pay,
        },
    }


def volatility(merchant_id: str, days: int = 30) -> dict:
    today = db.today()
    start = today - timedelta(days=days)
    daily = [amt for _, amt, _ in repo.daily_totals(merchant_id, start, today)]
    if len(daily) < 5:
        return {"value": {"available": False, "reason": "not enough observed days"},
                "basis": {"source": "analytics.volatility", "days_observed": len(daily)}}
    mean = statistics.fmean(daily)
    cv = statistics.pstdev(daily) / mean * 100.0 if mean else 0.0
    return {
        "value": {"available": True, "cv_pct": round(cv, 1),
                  "level": "high" if cv > 45 else ("moderate" if cv > 25 else "low"),
                  "mean_daily": round(mean, 0)},
        "basis": {"source": "analytics.volatility", "days_observed": len(daily),
                  "window": [start.isoformat(), today.isoformat()]},
    }
