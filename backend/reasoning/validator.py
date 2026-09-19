"""The grounding gate: what makes "never fabricate" a property of the system.

It sits between the answer and the speaker. Every CLAIM-BEARING figure in the
utterance -- rupees, percentages, counts of merchants -- must be traceable to
an evidence value, to something the merchant themselves said, or to the
question. A figure that is not gets sent back to the model to fix.

Two things changed here, and both were making the old system look stupid:

  * it used to check EVERY numeral. "teen din", "6 baje", "18-21", "do teen
    hafte" all counted as unbacked business claims, so a perfectly good answer
    was thrown away because it mentioned a duration. Figures are now classified
    by role and only the claim-bearing ones are checked.
  * a failure used to mean the whole utterance was discarded and an unrelated
    template was spoken instead. That is the "it reverted to the original
    answer" symptom. Failure now returns the offending figures so the caller
    can ask the model to correct them.

What did NOT change: an unbacked rupee figure still never reaches the merchant.
"""
from __future__ import annotations

import re
from typing import Any

NUMBER = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)(?![\w])")
INTERNAL_MERCHANT_ID = re.compile(r"\bM\d{3,}\b", re.I)

# Roles that carry a business claim and must be grounded.
CLAIM_ROLES = {"money", "percent", "count"}

MONEY_BEFORE = re.compile(r"(?:₹|rs\.?|inr|rupees?|rupaye|rupay)\s*$", re.I)
MONEY_AFTER = re.compile(r"^\s*(?:rs\.?|rupees?|rupaye|rupay|/-)", re.I)
PERCENT_AFTER = re.compile(r"^\s*(?:%|percent|pratishat|fisadi)", re.I)
# durations, clock times, dates, ordinals -- descriptive, not claims
DURATION_AFTER = re.compile(
    r"^\s*(?:%|)?\s*(?:din|dino|dinon|day|days|hafte|hafta|haftey|week|weeks|"
    r"mahine|mahina|month|months|saal|year|years|ghante|ghanta|hour|hours|"
    r"minute|minutes|baje|bajay|am|pm|o'clock|tarikh|date|:\d)", re.I)
DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}\s*[-/]\s*\d{1,2}")
COUNT_AFTER = re.compile(
    r"^\s*(?:of|out of|me se|mein se|dukaan|dukan|shops?|merchants?|"
    r"logon|log|bottles?|pieces?|packets?|units?|strips?|litres?|kg)", re.I)

# Small numbers that are almost always structural rather than claims.
STRUCTURAL = {0.0, 1.0, 2.0, 3.0, 4.0}


def classify(text: str, match: re.Match) -> str:
    """What role does this figure play in the sentence?"""
    before = text[max(0, match.start() - 14):match.start()]
    after = text[match.end():match.end() + 16]
    window = text[max(0, match.start() - 8):match.end() + 8]

    if DATE_LIKE.search(window):
        return "date"
    if PERCENT_AFTER.match(after):
        return "percent"
    if MONEY_BEFORE.search(before) or MONEY_AFTER.match(after):
        return "money"
    if DURATION_AFTER.match(after):
        return "duration"
    if COUNT_AFTER.match(after):
        return "count"
    value = _to_float(match.group(1))
    if value is not None and value in STRUCTURAL:
        return "structural"
    if value is not None and abs(value) >= 1000:
        # a large bare number in a business answer is a rupee figure in all but
        # name; treat it as a claim rather than letting it through unchecked
        return "money"
    return "count"


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def extract_numbers(text: str) -> list[float]:
    """Every numeral, unclassified. Kept for callers that just want the digits."""
    return [v for v in (_to_float(m.group(1)) for m in NUMBER.finditer(text or ""))
            if v is not None]


def claims(text: str) -> list[dict]:
    """The figures that actually assert something about the business."""
    out = []
    for m in NUMBER.finditer(text or ""):
        value = _to_float(m.group(1))
        if value is None:
            continue
        role = classify(text, m)
        if role in CLAIM_ROLES:
            out.append({"value": value, "role": role, "text": m.group(1)})
    return out


def _walk(obj: Any, sink: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        sink.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, sink)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, sink)
    elif isinstance(obj, str):
        sink.extend(extract_numbers(obj))


def numeric_closure(evidence, question: str = "",
                    stated: list[dict] | None = None) -> set[float]:
    """Every number the answer is allowed to use, plus tolerant spoken forms.

    Three sources, all legitimate: the evidence, anything the merchant said in
    this conversation, and the question itself -- if they asked about ₹50,000,
    the answer may say ₹50,000.
    """
    raw: list[float] = []
    for ev in evidence:
        value = ev.value if hasattr(ev, "value") else ev.get("value", {})
        _walk(value, raw)
    raw.extend(extract_numbers(question))
    for fact in stated or []:
        if fact.get("value_num") is not None:
            raw.append(float(fact["value_num"]))

    closure: set[float] = set()
    for n in raw:
        closure.update({n, abs(n), round(n), round(abs(n)), round(n, 1)})
        if abs(n) >= 100:
            closure.add(round(n / 100) * 100)
            closure.add(round(n / 10) * 10)
        if abs(n) >= 1000:
            closure.add(round(n / 1000) * 1000)
            closure.add(round(n / 500) * 500)
    return closure


def matches_any(n: float, allowed: set[float], tol: float = 0.5) -> bool:
    for a in allowed:
        if abs(a - n) <= tol:
            return True
        # a percentage spoken without its sign, or rounded for speech
        if a != 0 and abs(abs(a) - abs(n)) <= max(tol, abs(a) * 0.02):
            return True
    return False


def validate(utterance: str, evidence, question: str = "",
             stated: list[dict] | None = None) -> tuple[bool, list]:
    """(passed, unbacked). `unbacked` is detailed enough to hand back to the model."""
    allowed = numeric_closure(evidence, question, stated)
    unbacked: list = []
    for claim in claims(utterance):
        if not matches_any(claim["value"], allowed):
            unbacked.append(claim)
    for mid in dict.fromkeys(INTERNAL_MERCHANT_ID.findall(utterance or "")):
        unbacked.append({"value": mid, "role": "merchant_id",
                         "text": mid})
    return (not unbacked), unbacked


def describe(unbacked: list) -> str:
    """The correction note sent back to the model."""
    parts = []
    for u in unbacked:
        if u.get("role") == "merchant_id":
            parts.append(f"'{u['text']}' is an internal identifier and must never "
                         f"be spoken")
        else:
            parts.append(f"{u['text']} ({u['role']})")
    return "; ".join(parts)
