"""How two merchants are judged similar.

The weights live in config.py so they can go on a slide unchanged, and every
component score is kept so /ops can explain WHY two merchants are peers instead
of asserting that they are. "Not just business type" is literally this table.

It did not used to be. Category carried 35% against a 0.85 close-peer
threshold, which made the onboarding label a gate rather than a signal: a shop
with a different label could reach 0.65 at best, an "adjacent" label 0.825, and
neither could ever qualify no matter how alike the two shops really traded. Two
genuinely different businesses that happened to tick the same box -- a corner
kirana and a cafe under "Food & Beverage" -- were pooled, and the cafe would be
told that five of six shops like it recovered with an evening offer on the
strength of six kiranas.

Three changes fix that, and all three are visible in the components on /ops:

  1. Behaviour outweighs the label. Category is now a 10% prior. A shop with a
     different label but genuinely similar trading can still clear the bar; a
     shop with the same label and different trading no longer does.
  2. The day-shape comparison is centred (a correlation, not a cosine). Cosine
     on two all-positive vectors is dominated by their shared mean, so it
     reported 0.87 for a mobile accessories shop against a chai stall and 0.91
     for another chai stall. Centred, those become 0.63 and 0.72.
  3. Ticket and scale are absolute rupees and counts, measured from the ledger.
     The old volume band was relative to each category's own mean, so it
     inherited the label as well and compounded the error rather than
     catching it.
"""
from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.graph import behaviour  # noqa: E402

ADJACENT = {
    "food_stall": {"kirana"},
    "kirana": {"food_stall", "pharmacy"},
    "pharmacy": {"kirana"},
    "salon": {"mobile_accessories"},
    "mobile_accessories": {"salon"},
}

# How far apart two figures may be before they score zero. A ticket 2.5x larger
# is a different kind of shop; a shop doing 3x the transactions is a different
# size of operation.
TICKET_TOLERANCE = 2.5
SCALE_TOLERANCE = 3.0


def category_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return 0.5 if b in ADJACENT.get(a, ()) else 0.0


def locality_score(a_loc: str, a_type: str, b_loc: str, b_type: str) -> float:
    if a_loc == b_loc:
        return 1.0
    if a_type == b_type:
        return 0.7
    return 0.2


def shape_score(a: list[float], b: list[float]) -> float:
    """Correlation of two normalised shapes, rescaled to 0..1.

    Centring is the whole point. Both vectors are shares that sum to 1 and are
    positive everywhere, so they share a large mean; an uncentred dot product
    mostly measures that shared mean, which carries no information about when
    a shop is busy. Subtracting it first leaves only the shape.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    mean_a, mean_b = statistics.mean(a), statistics.mean(b)
    da = [x - mean_a for x in a]
    db_ = [y - mean_b for y in b]
    denom = math.sqrt(sum(x * x for x in da)) * math.sqrt(sum(y * y for y in db_))
    if not denom:
        return 0.0
    r = sum(x * y for x, y in zip(da, db_)) / denom
    return max(0.0, (r + 1.0) / 2.0)


def ratio_score(a: float | None, b: float | None, tolerance: float) -> float:
    """1.0 when equal, falling to 0 at `tolerance`x apart, in either direction."""
    if not a or not b or a <= 0 or b <= 0:
        return 0.0
    gap = abs(math.log(a / b))
    return max(0.0, 1.0 - gap / math.log(tolerance))


def score(a, b, profile_a: dict | None = None, profile_b: dict | None = None) -> dict:
    """a and b are merchant rows. Returns the total plus every component."""
    pa = profile_a or behaviour.load(a["id"])
    pb = profile_b or behaviour.load(b["id"])
    w = config.SIMILARITY_WEIGHTS

    parts = {
        "hour_shape": shape_score(pa.get("hour_shape"), pb.get("hour_shape")),
        "weekday_shape": shape_score(pa.get("weekday_shape"), pb.get("weekday_shape")),
        "ticket": ratio_score(pa.get("avg_ticket"), pb.get("avg_ticket"),
                              TICKET_TOLERANCE),
        "scale": ratio_score(pa.get("txns_per_day"), pb.get("txns_per_day"),
                             SCALE_TOLERANCE),
        "locality": locality_score(a["locality"], a["locality_type"],
                                   b["locality"], b["locality_type"]),
        "category": category_score(a["category"], b["category"]),
    }
    total = sum(parts[k] * w[k] for k in w)
    return {"total": round(total, 4),
            "components": {k: round(v, 3) for k, v in parts.items()},
            "behavioural_total": round(
                sum(parts[k] * w[k] for k in ("hour_shape", "weekday_shape",
                                              "ticket", "scale")), 4),
            "declared_total": round(
                sum(parts[k] * w[k] for k in ("locality", "category")), 4)}


def explain(a, b) -> str:
    """One line for /ops, in the order a person would check it."""
    s = score(a, b)
    c = s["components"]
    return (f"{s['total']:.2f} = day {c['hour_shape']:.2f} · week "
            f"{c['weekday_shape']:.2f} · ticket {c['ticket']:.2f} · scale "
            f"{c['scale']:.2f} · locality {c['locality']:.2f} · "
            f"label {c['category']:.2f}")
