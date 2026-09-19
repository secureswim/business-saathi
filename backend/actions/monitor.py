"""Proactive monitoring: the workflow that runs without being asked.

Four conditions must ALL hold before Saathi interrupts someone at work. A
sustained divergence that is market-wide produces NO alert -- telling a
merchant their sales are down when everyone's sales are down is noise, and
worse, it is advice they cannot act on.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.analytics import anomaly, trend  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402
from backend.graph.store import get_store  # noqa: E402
from backend.reasoning.templates import band_hi, rs, _action_hi  # noqa: E402


def candidates(limit: int = 20) -> list[str]:
    rows = db.q("SELECT id FROM merchants WHERE avg_daily > 0 ORDER BY id LIMIT ?", (limit,))
    return [r["id"] for r in rows if repo.days_of_history(r["id"]) >= 14]


def evaluate(merchant_id: str) -> dict:
    """Returns the four conditions and whether all of them hold."""
    store = get_store()
    t = trend.sales_trend(merchant_id)["value"]
    sustained = anomaly.sustained_deviation(
        merchant_id, config.ALERT_MIN_SUSTAINED_DAYS, -config.ALERT_DEVIATION_PCT)["value"]
    peers = store.peers(merchant_id)["value"]

    conditions = {
        "deviation": {"ok": abs(t["change_pct"]) >= config.ALERT_DEVIATION_PCT,
                      "value": t["change_pct"],
                      "threshold": config.ALERT_DEVIATION_PCT},
        "sustained": {"ok": sustained["sustained"] or t["change_pct"] >= config.ALERT_DEVIATION_PCT,
                      "value": sustained["windows_pct"],
                      "threshold": f"{config.ALERT_MIN_SUSTAINED_DAYS} days"},
        "cohort": {"ok": peers["cohort_size"] >= config.MIN_COHORT_SIZE,
                   "value": peers["cohort_size"], "threshold": config.MIN_COHORT_SIZE},
        "recency": {"ok": not repo.alerted_recently(merchant_id, config.ALERT_SUPPRESSION_HOURS),
                    "value": "no alert in the suppression window",
                    "threshold": f"{config.ALERT_SUPPRESSION_HOURS}h"},
    }

    peer_relative = None
    if conditions["cohort"]["ok"]:
        a = anomaly.peer_relative_anomaly(merchant_id, peers["peer_ids"])["value"]
        if a.get("conclusive"):
            peer_relative = a
    is_surge = t["change_pct"] > 0
    verdict_ok = bool(peer_relative and (
        peer_relative["verdict"] == "specific_to_merchant"
        or (is_surge and peer_relative["verdict"] == "outperforming")))
    conditions["peer_relative"] = {
        "ok": verdict_ok,
        "value": (peer_relative or {}).get("verdict", "inconclusive"),
        "threshold": "specific to this merchant",
    }

    return {"merchant_id": merchant_id, "conditions": conditions,
            "all_met": all(c["ok"] for c in conditions.values()),
            "trend": t, "peers": peers, "peer_relative": peer_relative,
            "alert_type": "surge" if is_surge else "decline"}


def build_alert(ev: dict) -> dict:
    store = get_store()
    t = ev["trend"]
    m = repo.merchant_row(ev["merchant_id"])
    kind = "demand_surge" if ev["alert_type"] == "surge" else (
        "evening_decline" if t["worst_band"] in ("16-19", "19-22") else "sales_decline")
    pb = store.peer_playbook(kind, ev["peers"]["peer_ids"])["value"]
    best = pb.get("best")

    if ev["alert_type"] == "surge":
        hi = (f"Bhai, aapki sales pichhle kuch din se normal se {abs(t['change_pct'])}% "
              f"upar ja rahi hain. Tayyari badha lijiye?")
        en = (f"{m['name']}: +{t['change_pct']}% versus baseline, "
              f"peer-relative {ev['peer_relative']['verdict']}.")
    else:
        hi = (f"Bhai, aapki {band_hi(t['worst_band'])} ki sales pichhle kuch din se "
              f"normal se {abs(t['change_pct'])}% neeche ja rahi hain. "
              f"Aapke jaise {ev['peers']['cohort_size']} merchants flat hain. "
              f"Dekhun kya ho raha hai?")
        en = (f"{m['name']}: {t['change_pct']}% versus baseline, concentrated "
              f"{t['worst_band']}, peer-relative {ev['peer_relative']['verdict']}.")
    if best:
        hi += f" {best['worked']} merchants ne {_action_hi(best['action'])} se recover kiya tha."

    proposal = None
    if best:
        params = best.get("common_params") or {}
        proposal = {"type": best["action"],
                    "params": {"discount_rs": int(params.get("discount_rs", 10)),
                               "window": params.get("window", "18-21"),
                               "days": int(params.get("days", 3))},
                    "condition": kind,
                    "evidence_summary": f"{best['worked']} of {best['tried']} similar merchants"}

    return {"merchant_id": ev["merchant_id"], "merchant_name": m["name"],
            "alert_type": ev["alert_type"], "severity": t["change_pct"],
            "band": t["worst_band"], "situation_kind": kind,
            "conditions": ev["conditions"], "hinglish": hi, "english": en,
            "proposal": proposal}


def scan(merchant_ids: list[str] | None = None, limit: int = 20,
         persist: bool = True) -> list[dict]:
    ids = merchant_ids if merchant_ids is not None else candidates(limit)
    alerts = []
    for mid in ids:
        try:
            ev = evaluate(mid)
        except Exception:      # noqa: BLE001
            continue
        if not ev["all_met"]:
            continue
        alert = build_alert(ev)
        if persist:
            repo.insert_alert(mid, alert["alert_type"], alert["severity"], alert)
        alerts.append(alert)
    return alerts
