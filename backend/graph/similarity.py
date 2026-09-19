"""How two merchants are judged similar.

Four components, weighted. The weights live in config.py so they can go on a
slide unchanged, and every component score is kept so /ops can explain WHY two
merchants are peers instead of asserting that they are. "Not just business
type" is literally this table.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402

ADJACENT = {
    "food_stall": {"kirana"},
    "kirana": {"food_stall", "pharmacy"},
    "pharmacy": {"kirana"},
    "salon": {"mobile_accessories"},
    "mobile_accessories": {"salon"},
}
BAND_INDEX = {"low": 0, "mid": 1, "high": 2}


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


def band_score(a: str, b: str) -> float:
    return 1.0 - abs(BAND_INDEX[a] - BAND_INDEX[b]) / 2.0


def pattern_score(a_vec, b_vec) -> float:
    """Cosine similarity of the normalised hourly vectors."""
    if isinstance(a_vec, str):
        a_vec = json.loads(a_vec)
    if isinstance(b_vec, str):
        b_vec = json.loads(b_vec)
    dot = sum(x * y for x, y in zip(a_vec, b_vec))
    na = math.sqrt(sum(x * x for x in a_vec))
    nb = math.sqrt(sum(y * y for y in b_vec))
    return dot / (na * nb) if na and nb else 0.0


def score(a, b) -> dict:
    """a and b are merchant rows. Returns the total plus every component."""
    w = config.SIMILARITY_WEIGHTS
    parts = {
        "category": category_score(a["category"], b["category"]),
        "locality": locality_score(a["locality"], a["locality_type"],
                                   b["locality"], b["locality_type"]),
        "volume_band": band_score(a["volume_band"], b["volume_band"]),
        "customer_pattern": pattern_score(a["hourly_vector"], b["hourly_vector"]),
    }
    total = sum(parts[k] * w[k] for k in w)
    return {"total": round(total, 4),
            "components": {k: round(v, 3) for k, v in parts.items()}}
