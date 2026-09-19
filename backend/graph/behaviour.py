"""What a shop actually DOES, measured from its payments.

This module exists because of a flaw that went unnoticed for a long time: the
peer cohort was decided by the category the merchant picked on an onboarding
form, and the one component that looked at real behaviour could not tell shops
apart.

The arithmetic was unforgiving. Category carried 35% of the score and the close
-peer threshold was 0.85, so a merchant with a different label started at most
0.65 and could never qualify however similar its trading actually was. An
"adjacent" label topped out at 0.825 -- still short. The label was not a signal,
it was a gate.

And the behavioural component could not compensate, because it used cosine
similarity on two all-positive vectors. Those vectors share a large positive
mean, which dominates the dot product, so everything looks alike: across all
159 peers of the demo merchant, cosine spanned 0.58 to 1.00. A mobile
accessories shop scored 0.87 against a chai stall; another chai stall scored
0.91. Centring the vectors first -- a correlation rather than a cosine -- spans
-0.23 to 1.00 on the same data, and moves mobile accessories to 0.63 against
0.72. Same information, three times the discrimination, because the part that
carried no information was removed.

So everything here is derived from the ledger:

  * the shape of the trading day    (when money arrives, not how much)
  * the shape of the trading week
  * the observed average ticket     (in rupees, not relative to a category)
  * the observed scale              (transactions per day)
  * volatility

Two of these deserve a note. Ticket and scale are ABSOLUTE. The old volume band
was computed against each category's own mean, so it inherited the label too --
a mislabelled shop got a wrong band on top of a wrong category, and the error
compounded instead of cancelling. And the rhythm/ticket archetype below is what
the learned-pattern aggregate is now keyed on, so a wrong label can no longer
walk straight into every "worked for X of Y" figure a merchant is told.

Cached in `merchant_patterns` under 'behaviour' by scripts/recompute_patterns.py,
because computing it for 160 merchants on every question would cost the latency
budget several times over.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.data import db, repository as repo  # noqa: E402

OPEN_HOUR, CLOSE_HOUR = 7, 22
WEEKDAYS = 7
PROFILE_DAYS = 56           # eight weeks: enough to average out a bad week

# Ticket bands in rupees, measured, not inferred from a label. The boundaries
# are where Indian retail actually changes character: a tea stall, a kirana
# basket, a salon appointment, an electronics purchase.
TICKET_BANDS = [(75.0, "micro"), (200.0, "small"), (450.0, "mid")]


def ticket_band(avg_ticket: float | None) -> str:
    if not avg_ticket:
        return "unknown"
    for edge, name in TICKET_BANDS:
        if avg_ticket < edge:
            return name
    return "large"


def rhythm(hour_shape: list[float]) -> str:
    """Which part of the day carries the money. Label-free by construction."""
    if not hour_shape or len(hour_shape) < 16:
        return "unknown"
    share = lambda lo, hi: sum(hour_shape[lo - OPEN_HOUR:hi - OPEN_HOUR + 1])  # noqa: E731
    morning, midday, evening = share(7, 11), share(12, 16), share(17, 22)
    top = max(morning, midday, evening)
    if top < 0.45:
        return "spread"
    return {morning: "morning", midday: "midday", evening: "evening"}[top]


def _normalise(values: list[float]) -> list[float]:
    total = sum(values)
    return [v / total for v in values] if total else [0.0] * len(values)


def compute(merchant_id: str, days: int = PROFILE_DAYS) -> dict:
    """Measure this merchant. Reads rows; assumes nothing from the profile."""
    end = db.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)

    hours = [0.0] * (CLOSE_HOUR - OPEN_HOUR + 1)
    weekdays = [0.0] * WEEKDAYS
    total_amount = 0.0
    total_txns = 0
    seen_days: set = set()

    for day, hour, amount, txns in repo.hour_grid(merchant_id, start, end):
        index = hour - OPEN_HOUR
        if 0 <= index < len(hours):
            hours[index] += amount
        weekdays[day.weekday()] += amount
        total_amount += amount
        total_txns += txns
        seen_days.add(day)

    open_days = len(seen_days)
    if not open_days or total_amount <= 0:
        return {"available": False, "reason": "no trading history",
                "hour_shape": [], "weekday_shape": [], "avg_ticket": None,
                "txns_per_day": 0.0, "rhythm": "unknown", "ticket_band": "unknown"}

    hour_shape = _normalise(hours)
    weekday_shape = _normalise(weekdays)
    avg_ticket = round(total_amount / total_txns, 2) if total_txns else None
    txns_per_day = round(total_txns / open_days, 2)

    daily = [amount for _, amount, _ in repo.daily_totals(merchant_id, start, end)]
    cv = (statistics.pstdev(daily) / statistics.mean(daily) * 100.0
          if len(daily) > 1 and statistics.mean(daily) else 0.0)

    return {
        "available": True,
        "hour_shape": [round(v, 5) for v in hour_shape],
        "weekday_shape": [round(v, 5) for v in weekday_shape],
        "avg_ticket": avg_ticket,
        "txns_per_day": txns_per_day,
        "revenue_per_day": round(total_amount / open_days, 2),
        "volatility_pct": round(cv, 1),
        "rhythm": rhythm(hour_shape),
        "ticket_band": ticket_band(avg_ticket),
        "days_observed": open_days,
        "window_days": days,
    }


# ------------------------------------------------------------------ caching
def store(merchant_id: str, profile: dict) -> None:
    db.write("INSERT OR REPLACE INTO merchant_patterns VALUES (?,?,?,?)",
             (merchant_id, "behaviour", db.today().isoformat(),
              json.dumps(profile)))


def load(merchant_id: str) -> dict:
    """Cached profile, computed on demand if recompute_patterns has not run."""
    row = db.q1("SELECT payload FROM merchant_patterns WHERE merchant_id=? "
                "AND pattern_type='behaviour'", (merchant_id,))
    if row:
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            pass
    profile = compute(merchant_id)
    try:
        store(merchant_id, profile)
    except Exception:                                 # noqa: BLE001
        pass                    # a read path must never fail on a cache write
    return profile


def load_many(merchant_ids: list[str]) -> dict[str, dict]:
    """One query for the whole population, for cohort scoring."""
    if not merchant_ids:
        return {}
    marks = ",".join("?" * len(merchant_ids))
    rows = db.q(f"SELECT merchant_id, payload FROM merchant_patterns "
                f"WHERE pattern_type='behaviour' AND merchant_id IN ({marks})",
                tuple(merchant_ids))
    out = {}
    for r in rows:
        try:
            out[r["merchant_id"]] = json.loads(r["payload"])
        except (ValueError, TypeError):
            continue
    for mid in merchant_ids:
        if mid not in out:
            out[mid] = load(mid)
    return out


# ------------------------------------------------------- the cohort key
def cohort_key(merchant_id: str, locality_type: str | None = None,
               profile: dict | None = None) -> str:
    """The bucket the learned-pattern aggregate is keyed on.

    Behaviour first, place second, and no category at all. Previously this was
    `category|locality_type|volume_band` -- both the category and the band came
    from the onboarding label, so a shop filed under the wrong label had its
    outcomes pooled with the wrong shops, and that pooling is exactly what
    gets quoted back as "X of Y merchants like you".
    """
    profile = profile or load(merchant_id)
    if locality_type is None:
        row = repo.merchant_row(merchant_id)
        locality_type = row["locality_type"]
    return f"{profile.get('rhythm', 'unknown')}|" \
           f"{profile.get('ticket_band', 'unknown')}|{locality_type}"


def members_of(key: str) -> list[str]:
    """Every merchant currently in that behavioural bucket."""
    try:
        rhythm_name, band, locality_type = key.split("|")
    except ValueError:
        return []
    rows = db.q("SELECT id, locality_type FROM merchants WHERE locality_type=? "
                "AND avg_daily>0", (locality_type,))
    ids = [r["id"] for r in rows]
    profiles = load_many(ids)
    return [mid for mid in ids
            if profiles.get(mid, {}).get("rhythm") == rhythm_name
            and profiles.get(mid, {}).get("ticket_band") == band]
