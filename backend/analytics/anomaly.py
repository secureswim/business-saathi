"""Peer-relative anomaly and situation detection.

`detect_situation` is the bridge between analytics and the graph: it turns raw
numbers into a typed condition with a severity and a band. That row is the key
everything collective is looked up by -- without it, "what worked for merchants
like me" has no *in this situation* to attach to.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.analytics.trend import sales_trend, volatility  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402

MIN_PEERS_FOR_COMPARISON = 3


def peer_relative_anomaly(merchant_id: str, peer_ids: list[str],
                          window_days: int = 7) -> dict:
    """The merchant's change minus the cohort's median change, in sd units.

    This is the capability no single-merchant system can offer, and the reason
    the graph exists. Below three usable peers it returns inconclusive and the
    synthesis layer omits the comparison rather than hedging it.
    """
    me = sales_trend(merchant_id, window_days)["value"]["change_pct"]
    changes = [sales_trend(p, window_days)["value"]["change_pct"]
               for p in peer_ids if p != merchant_id]

    if len(changes) < MIN_PEERS_FOR_COMPARISON:
        return {"value": {"conclusive": False, "reason": "cohort too small to compare"},
                "basis": {"source": "analytics.peer_relative_anomaly",
                          "peers_used": len(changes),
                          "minimum": MIN_PEERS_FOR_COMPARISON}}

    median = statistics.median(changes)
    spread = statistics.pstdev(changes) or 1.0
    z = (me - median) / spread
    if z < -1.2:
        verdict = "specific_to_merchant"
    elif z > 1.2:
        verdict = "outperforming"
    else:
        verdict = "market_wide"

    return {
        "value": {"conclusive": True, "your_change_pct": round(me, 1),
                  "peer_median_change_pct": round(median, 1),
                  "gap_pct": round(me - median, 1), "z": round(z, 2),
                  "verdict": verdict, "peers_used": len(changes)},
        "basis": {"source": "analytics.peer_relative_anomaly",
                  "peers_used": len(changes), "window_days": window_days,
                  "peer_changes_pct": [round(c, 1) for c in changes],
                  "method": "(own change - cohort median) / cohort sd"},
    }


def detect_situation(merchant_id: str, peer_ids: list[str] | None = None,
                     persist: bool = False) -> dict:
    """Classify the merchant's current condition into a typed situation."""
    t = sales_trend(merchant_id)["value"]
    vol = volatility(merchant_id)["value"]
    peer_rel = "unknown"
    anomaly = None
    if peer_ids:
        anomaly = peer_relative_anomaly(merchant_id, peer_ids)["value"]
        if anomaly.get("conclusive"):
            peer_rel = anomaly["verdict"]

    change = t["change_pct"]
    band = t["worst_band"]
    evening = band in ("16-19", "19-22")

    if change <= -10 and evening:
        kind, severity = "evening_decline", change
    elif change <= -10:
        kind, severity = "sales_decline", change
    elif change >= 12:
        kind, severity = "demand_surge", change
    elif vol.get("available") and vol.get("level") == "high":
        kind, severity = "volatility", vol["cv_pct"]
    else:
        kind, severity = "flat", change

    situation_id = None
    if persist and kind != "flat":
        situation_id = repo.insert_situation(
            merchant_id, kind, db.today().isoformat(), severity, band, peer_rel,
            {"trend": t, "anomaly": anomaly})

    return {
        "value": {"kind": kind, "severity": round(severity, 1), "band": band,
                  "peer_relative": peer_rel, "situation_id": situation_id,
                  "detected_on": db.today().isoformat()},
        "basis": {"source": "analytics.detect_situation",
                  "trend_basis": "analytics.sales_trend",
                  "rules": "decline <=-10 (+evening band) | surge >=+12 | high volatility | flat",
                  "peer_relative_from": "analytics.peer_relative_anomaly" if peer_ids else None},
    }


def sustained_deviation(merchant_id: str, min_days: int = 3,
                        threshold_pct: float = -12.0) -> dict:
    """Has the deviation held for N days? One bad day is weather, not a pattern."""
    windows = [sales_trend(merchant_id, window_days=d)["value"]["change_pct"]
               for d in (min_days, min_days + 2, min_days + 4)]
    sustained = all(c <= threshold_pct for c in windows)
    return {
        "value": {"sustained": sustained, "windows_pct": [round(c, 1) for c in windows],
                  "threshold_pct": threshold_pct, "min_days": min_days},
        "basis": {"source": "analytics.sustained_deviation",
                  "method": f"change vs baseline over {min_days}, {min_days+2}, {min_days+4} days"},
    }
