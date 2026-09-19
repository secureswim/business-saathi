"""Privacy enforcement at the graph boundary.

Two purposes, two filters:
  * SYNTHESIS -- what the merchant may hear: counts, rates, medians and
    parameter ranges. Merchant identifiers and absolute figures are removed,
    so the merchant literally cannot be told which shop recovered: the string
    never reaches the synthesis layer.
  * OPS -- what the team sees: peer ids and similarity components, so the
    cohort canvas can be drawn. Still no absolute rupee figures.

Below MIN_COHORT_SIZE nothing is returned at all, whatever the caller asks for.
This is a module, not a convention, because "we filter it in the UI" is not an
answer to a privacy question.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402

SYNTHESIS = "synthesis"
OPS = "ops"

# keys that must never reach the merchant-facing synthesis layer
IDENTIFIER_KEYS = {"peer_ids", "extended_ids", "peers", "extended", "merchant_id",
                   "merchant_ids", "ids", "name", "names"}
# keys that are absolute money rather than relative movement
ABSOLUTE_KEYS = {"before", "after", "revenue", "amount", "avg_daily", "total_revenue"}


class CohortTooSmall(Exception):
    pass


def check_cohort(cohort_ids: list[str]) -> None:
    if len(cohort_ids) < config.MIN_COHORT_SIZE:
        raise CohortTooSmall(
            f"cohort of {len(cohort_ids)} is below the privacy floor of "
            f"{config.MIN_COHORT_SIZE}")


def unavailable_result(source: str, cohort_ids: list[str]) -> dict:
    return {
        "value": {"available": False, "sufficient": False,
                  "cohort_size": len(cohort_ids),
                  "reason": f"cohort below privacy floor of {config.MIN_COHORT_SIZE}"},
        "basis": {"source": source, "min_cohort_size": config.MIN_COHORT_SIZE,
                  "enforced_by": "graph.privacy"},
    }


def _strip(obj, keys: set[str]):
    if isinstance(obj, dict):
        return {k: _strip(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [_strip(v, keys) for v in obj]
    return obj


def enforce(result: dict, cohort_ids: list[str], purpose: str) -> dict:
    """Every graph retrieval passes through here before it is returned."""
    check_cohort(cohort_ids)
    if purpose == SYNTHESIS:
        return {"value": _strip(result["value"], IDENTIFIER_KEYS | ABSOLUTE_KEYS),
                "basis": _strip(result["basis"], IDENTIFIER_KEYS | ABSOLUTE_KEYS)}
    if purpose == OPS:
        return {"value": _strip(result["value"], ABSOLUTE_KEYS),
                "basis": _strip(result["basis"], ABSOLUTE_KEYS)}
    raise ValueError(f"unknown purpose {purpose}")


def split_for_transport(result: dict, cohort_ids: list[str]) -> dict:
    """What the API sends: ops-safe `basis` for the console, synthesis-safe `value`.

    The runner attaches the synthesis-safe value to the evidence the reasoner
    sees, while /ops receives the fuller basis over the websocket. One call
    site, so the two can never drift apart.
    """
    check_cohort(cohort_ids)
    ops = enforce(result, cohort_ids, OPS)
    syn = enforce(result, cohort_ids, SYNTHESIS)
    return {"value": syn["value"], "basis": ops["basis"],
            "ops_value": ops["value"]}
