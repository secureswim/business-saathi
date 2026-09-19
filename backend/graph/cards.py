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
from backend.graph import behaviour  # noqa: E402

READABLE = {"food_stall": "food stall", "kirana": "kirana store", "salon": "salon",
            "pharmacy": "pharmacy", "mobile_accessories": "mobile accessories shop"}
LOC = {"college_area": "college area", "office_park": "office area",
       "residential_colony": "residential colony", "market_street": "market street"}

# The cohort key is now MEASURED -- rhythm|ticket_band|locality_type -- so these
# render it in words. Reading it as category|locality|band produced sentences
# like "Among middays in micro locations with college_area volume", which is
# not just ugly: it is a retrievable document stating something meaningless.
# (singular, plural) -- these appear both as "a shop that TAKES..." and as
# "shops that TAKE...", and Cognee is going to embed whatever we write.
RHYTHM = {
    "morning": ("takes most of its money in the morning",
                "take most of their money in the morning"),
    "midday": ("takes most of its money around midday",
               "take most of their money around midday"),
    "evening": ("takes most of its money in the evening",
                "take most of their money in the evening"),
    "spread": ("trades steadily through the day",
               "trade steadily through the day"),
    "unknown": ("has no established pattern yet",
                "have no established pattern yet"),
}
TICKET = {"micro": "very small bills", "small": "small bills",
          "mid": "mid-sized bills", "large": "large bills",
          "unknown": "bills of no established size"}


def _cohort_words(cohort_key: str, plural: bool = False) -> tuple[str, str, str]:
    """(rhythm phrase, ticket phrase, locality phrase) for a measured key."""
    parts = (cohort_key or "").split("|")
    if len(parts) != 3:
        return RHYTHM["unknown"][plural], TICKET["unknown"], "an unknown area"
    rhythm, ticket, ltype = parts
    return (RHYTHM.get(rhythm, RHYTHM["unknown"])[plural],
            TICKET.get(ticket, TICKET["unknown"]),
            LOC.get(ltype, ltype.replace("_", " ")))


def merchant_profile_card(row, profile: dict | None = None) -> dict:
    """What this merchant declared, and what they measurably do.

    Both, deliberately. The declared label is still worth retrieving on -- it is
    how a person describes their own shop -- but it is not what defines the
    cohort any more, so the card says what the ledger says as well."""
    profile = profile or behaviour.load(row["id"])
    shape = profile.get("hour_shape") or json.loads(row["hourly_vector"])
    peak_hours = sorted(range(len(shape)), key=lambda i: -shape[i])[:4]
    peaks = ", ".join(str(7 + h) for h in sorted(peak_hours))
    key = behaviour.cohort_key(row["id"], row["locality_type"], profile)
    rhythm_txt, ticket_txt, loc_txt = _cohort_words(key)
    rhythm_plural, _, _ = _cohort_words(key, plural=True)
    text = (
        f"Merchant {row['id']} is registered as a "
        f"{READABLE.get(row['category'], row['category'])} in {row['locality']}, "
        f"a {LOC.get(row['locality_type'], row['locality_type'])}. "
        f"Measured from payments: it {rhythm_txt} and takes {ticket_txt}"
        + (f" (about Rs {profile['avg_ticket']:.0f} per payment, "
           f"{profile['txns_per_day']:.0f} payments a day)"
           if profile.get("avg_ticket") else "")
        + f". Busiest hours: {peaks}. "
        f"Behavioural cohort: shops in a {loc_txt} that {rhythm_plural} "
        f"and take {ticket_txt}."
    )
    return {"text": text, "metadata": {
        "kind": "merchant_profile", "merchant_id": row["id"],
        "declared_category": row["category"], "locality": row["locality"],
        "locality_type": row["locality_type"],
        "measured_rhythm": profile.get("rhythm"),
        "measured_ticket_band": profile.get("ticket_band"),
        "avg_ticket": profile.get("avg_ticket"),
        "txns_per_day": profile.get("txns_per_day"),
        "cohort_key": key,
        "hourly_signature": [round(v, 5) for v in shape]}}


def experience_card(exp: dict, cohort_key: str) -> dict:
    """One situation -> action -> outcome chain. The important card."""
    rhythm_txt, ticket_txt, loc_txt = _cohort_words(cohort_key)
    sit = exp.get("situation_kind") or "a change in sales"
    sev = exp.get("severity")
    band_txt = f" concentrated in the {exp['band']} band" if exp.get("band") else ""
    params = ", ".join(f"{k} {v}" for k, v in (exp.get("params") or {}).items())
    verdict = exp["verdict"]
    delta = exp["delta_pct"]

    text = (
        f"A shop in a {loc_txt} that {rhythm_txt} and takes {ticket_txt} "
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
    rhythm_txt, ticket_txt, loc_txt = _cohort_words(pattern["cohort_key"], plural=True)
    rate = (pattern["worked"] / pattern["tried"] * 100) if pattern["tried"] else 0
    text = (
        f"Among shops in a {loc_txt} that {rhythm_txt} and take {ticket_txt}, "
        f"facing {pattern['situation_kind'].replace('_', ' ')}, a "
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
    rows = [r for r in repo.all_merchants() if r["avg_daily"] > 0]
    profiles_by_id = behaviour.load_many([r["id"] for r in rows])
    for row in repo.all_merchants():
        if row["avg_daily"] <= 0:
            continue
        profile = profiles_by_id.get(row["id"]) or behaviour.load(row["id"])
        profiles.append(merchant_profile_card(row, profile))
        key = behaviour.cohort_key(row["id"], row["locality_type"], profile)
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


def fingerprint(payload: dict | None = None) -> str:
    """A stable hash of exactly what would be ingested.

    Cognee has no way to tell you whether what it holds matches the database
    you are demoing from, and `add_text` only ever appends -- so a re-ingest
    after a regeneration leaves the OLD cards in the graph alongside the new
    ones, and retrieval surfaces both. Two contradictory versions of "what
    worked for merchants like you" is worse than no Cognee at all.

    So the ingest records this fingerprint and every status check compares it.
    """
    import hashlib

    payload = payload or all_cards()
    h = hashlib.sha256()
    for group in ("profiles", "experiences", "patterns"):
        for card in payload.get(group, []):
            h.update(card["text"].encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()[:16]
