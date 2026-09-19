"""SQLite rows -> the natural-language documents Cognee ingests.

Three kinds of card. The combination of readable text plus structured metadata
is what Cognee's extraction turns into typed entities and relationships.

DELIBERATELY NOT INGESTED: raw transaction rows, individual revenue figures,
merchant names, or anything that would let one merchant's numbers be
reconstructed. Severity and delta are percentages of the merchant's OWN
baseline, never rupees -- a percentage cannot be reversed into a revenue figure
without the baseline, and the baseline is not in the graph.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402

READABLE = {"food_stall": "food stall", "kirana": "kirana store", "salon": "salon",
            "pharmacy": "pharmacy", "mobile_accessories": "mobile accessories shop"}
LOC = {"college_area": "college area", "office_park": "office area",
       "residential_colony": "residential colony", "market_street": "market street"}


def merchant_profile_card(row) -> dict:
    vec = json.loads(row["hourly_vector"])
    peak_hours = sorted(range(len(vec)), key=lambda i: -vec[i])[:4]
    peaks = ", ".join(str(7 + h) for h in sorted(peak_hours))
    text = (
        f"Merchant {row['id']} is a {READABLE.get(row['category'], row['category'])} "
        f"in {row['locality']}, a {LOC.get(row['locality_type'], row['locality_type'])}. "
        f"Business volume band: {row['volume_band']}. "
        f"Busiest hours: {peaks}. "
        f"Cohort: {row['category']} in {row['locality_type']} at {row['volume_band']} volume."
    )
    return {"text": text, "metadata": {
        "kind": "merchant_profile", "merchant_id": row["id"],
        "category": row["category"], "locality": row["locality"],
        "locality_type": row["locality_type"], "volume_band": row["volume_band"],
        "cohort_key": f"{row['category']}|{row['locality_type']}|{row['volume_band']}",
        "hourly_signature": vec}}


def experience_card(exp: dict, cohort_key: str) -> dict:
    """One situation -> action -> outcome chain. The important card."""
    cat, ltype, band = cohort_key.split("|")
    sit = exp.get("situation_kind") or "a change in sales"
    sev = exp.get("severity")
    band_txt = f" concentrated in the {exp['band']} band" if exp.get("band") else ""
    params = ", ".join(f"{k} {v}" for k, v in (exp.get("params") or {}).items())
    verdict = exp["verdict"]
    delta = exp["delta_pct"]

    text = (
        f"A {READABLE.get(cat, cat)} in a {LOC.get(ltype, ltype)} with {band} volume "
        f"experienced {sit.replace('_', ' ')}"
        + (f" of {abs(sev):.0f}% against its own baseline" if sev else "")
        + f"{band_txt}. The merchant responded with a "
        f"{exp['type'].replace('_', ' ')} ({params}). "
        f"The measured outcome was {delta:+.1f}% on the affected metric. "
        f"Verdict: {verdict}."
    )
    return {"text": text, "metadata": {
        "kind": "experience", "cohort_key": cohort_key,
        "situation_kind": exp.get("situation_kind"), "severity": sev,
        "action_type": exp["type"], "params": exp.get("params"),
        "verdict": verdict, "delta_pct": delta,
        "simulated": bool(exp.get("simulated", 1))}}


def cohort_pattern_card(pattern: dict) -> dict:
    """The aggregate the merchant actually hears. Regenerated on every write-back."""
    cat, ltype, band = pattern["cohort_key"].split("|")
    rate = (pattern["worked"] / pattern["tried"] * 100) if pattern["tried"] else 0
    text = (
        f"Among {READABLE.get(cat, cat)}s in {LOC.get(ltype, ltype)} locations with "
        f"{band} volume facing {pattern['situation_kind'].replace('_', ' ')}, a "
        f"{pattern['action_type'].replace('_', ' ')} has been tried "
        f"{pattern['tried']} times and worked {pattern['worked']} times "
        f"({rate:.0f}%)."
        + (f" Median change {pattern['median_delta']:+.1f}%."
           if pattern.get("median_delta") is not None else "")
    )
    return {"text": text, "metadata": {
        "kind": "cohort_pattern", "cohort_key": pattern["cohort_key"],
        "situation_kind": pattern["situation_kind"],
        "action_type": pattern["action_type"], "tried": pattern["tried"],
        "worked": pattern["worked"], "success_rate": round(rate, 1)}}


def all_cards() -> dict[str, list[dict]]:
    """Everything worth ingesting, in one pass over the ledger."""
    profiles, experiences, patterns = [], [], []
    for row in repo.all_merchants():
        if row["avg_daily"] <= 0:
            continue
        profiles.append(merchant_profile_card(row))
        key = f"{row['category']}|{row['locality_type']}|{row['volume_band']}"
        for exp in repo.cohort_experiences([row["id"]]):
            experiences.append(experience_card(exp, key))
    # Pattern cards are written as cohort statements -- "among food stalls in
    # college areas, X has been tried N times and worked M times". At tried=1
    # that sentence is one merchant's single afternoon wearing a cohort's
    # clothes: it is both unusable as evidence AND a description of an
    # individual, which is exactly what the cohort floor exists to prevent.
    # The floor upstream counts MERCHANTS, so it cannot catch this; this one
    # counts ATTEMPTS. Experience cards are unaffected -- they never claim to
    # be an aggregate.
    skipped = 0
    for row in db.q("SELECT * FROM learned_patterns"):
        pattern = dict(row)
        if pattern["tried"] < config.MIN_ATTEMPTS_TO_RECOMMEND:
            skipped += 1
            continue
        patterns.append(cohort_pattern_card(pattern))
    return {"profiles": profiles, "experiences": experiences, "patterns": patterns,
            "patterns_below_floor": skipped}
