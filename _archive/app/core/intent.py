"""Intent parsing for Hindi / Hinglish / English merchant speech.

Deliberately rule-first. A merchant saying "sales kyun kam hain" must route
correctly on a bad 3G connection with no LLM round-trip, so the patterns carry
the routing and the LLM adapter is only asked to disambiguate what the rules
cannot. That also makes the demo deterministic on stage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any

# intent families map to the four answer modes in the deck
UNDERSTAND = "understand"
PREDICT = "predict"
PROTECT = "protect"
ACT = "act"


@dataclass
class Intent:
    name: str
    family: str
    confidence: float
    slots: dict[str, Any]
    matched: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Each rule: (intent, family, [regex patterns]). Hinglish is written the way
# merchants actually type and speak it, including Devanagari.
RULES: list[tuple[str, str, list[str]]] = [
    ("business_health", UNDERSTAND, [
        r"business\s+kaisa", r"how('?s| is)\s+(my\s+)?business", r"kaisa chal", r"overall",
        r"बिजनेस\s+कैसा", r"business\s+status", r"summary",
    ]),
    ("sales_drop", UNDERSTAND, [
        r"sales?\s+(kyun|kyu|why)", r"why\s+.*\b(drop|down|fall|less|low)",
        r"sales?\s+kam", r"kam\s+(kyun|kyu)", r"bikri\s+kam", r"सेल.*कम",
        r"(drop|decline|down)\b.*\bsales?", r"kya\s+hua",
    ]),
    ("area_or_me", UNDERSTAND, [
        r"(just|only)\s+me", r"sirf\s+(mera|mujhe|meri)", r"whole\s+area", r"poora\s+market",
        r"area\s+(mein|me)\b", r"market\s+(mein|me)\b", r"everyone", r"sabka",
        r"baaki\s+(dukaan|shops)", r"others?\s+(also|too)",
    ]),
    ("cash_forecast", PREDICT, [
        r"cash", r"paisa\s+(bachega|hoga|rahega)", r"afford", r"kharid\s+sakta",
        r"enough\s+money", r"buffer", r"पैसा", r"next\s+week.*money", r"inventory.*(buy|kharid)",
        r"(buy|kharid).*\b(inventory|stock)\b",
    ]),
    ("rush_forecast", PREDICT, [
        r"rush", r"bheed", r"busy\s+(kab|when)", r"peak", r"next\s+rush", r"kitna\s+banau",
        r"how\s+much\s+should\s+i\s+(make|prepare)", r"भीड",
    ]),
    ("festive_prep", PREDICT, [
        r"diwali", r"दिवाली", r"festiv", r"tyohar", r"holi", r"eid", r"stock\s+(kya|karu|for)",
        r"navratri", r"season",
    ]),
    ("discount_check", PROTECT, [
        r"\d+\s*%\s*(discount|off|chhut)", r"discount\s+(du|dun|karu|run|de)", r"deep\s+discount",
        r"should\s+i\s+discount", r"sale\s+lagau", r"छूट",
    ]),
    ("price_whatif", PROTECT, [
        r"(price|rate|daam|keemat)\s*.*(badha|increase|raise|bada)", r"raise\s+(my\s+)?price",
        r"(badha|increase)\s*.*(price|rate|daam)", r"₹?\s*\d+\s*(rupees?|rs\.?|₹)?\s*(zyada|more|badha)",
        r"दाम.*बढ", r"what\s+if\s+i\s+(raise|increase)",
    ]),
    ("create_offer", ACT, [
        r"offer\s+(bana|banao|bana de|create|set)", r"campaign\s+(bana|create|chalu)",
        r"chalu\s+kar", r"kar\s+do", r"run\s+(the\s+)?offer", r"start\s+(the\s+)?offer",
        r"ऑफर.*बना", r"\bdo\s+it\b", r"^\s*(haan|haa|ha)\s*[.!]?\s*$",
        r"^\s*(yes|yep|ok|okay|sure)\b", r"^\s*(theek hai|thik hai|sahi hai|bilkul)\b", r"^\s*हां\s*$",
    ]),
    ("what_should_i_do", UNDERSTAND, [
        r"kya\s+(karu|karna|karein)", r"what\s+should\s+i\s+do", r"suggest", r"recommend",
        r"advice", r"क्या\s+करूं", r"help",
    ]),
]

_RUPEE = re.compile(r"(?:₹|rs\.?\s*|rupees?\s*)(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:rupees?|rs\b|₹)", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|pct|fisadi)", re.I)
_K = re.compile(r"(\d+(?:\.\d+)?)\s*(?:k|hazaar|hazar|thousand)\b", re.I)


def extract_slots(text: str) -> dict[str, Any]:
    t = text.lower()
    slots: dict[str, Any] = {}

    if m := _PCT.search(t):
        slots["percent"] = float(m.group(1))

    amounts: list[float] = []
    for m in _K.finditer(t):
        amounts.append(float(m.group(1)) * 1000)
    for m in _RUPEE.finditer(t):
        amounts.append(float(m.group(1) or m.group(2)))
    if amounts:
        slots["amounts"] = sorted(set(amounts))
        slots["amount"] = max(amounts)
        slots["small_amount"] = min(amounts)

    if re.search(r"\b(evening|shaam|शाम|6-9|6 to 9)\b", t):
        slots["slot"] = "evening"
    elif re.search(r"\b(lunch|dopahar|afternoon)\b", t):
        slots["slot"] = "lunch"
    elif re.search(r"\b(morning|subah|सुबह)\b", t):
        slots["slot"] = "morning"

    if re.search(r"\b(week|hafta|hafte)\b", t):
        slots["horizon_days"] = 7
    if re.search(r"\b(month|mahina|mahine)\b", t):
        slots["horizon_days"] = 30

    return slots


def parse(text: str) -> Intent:
    t = text.strip().lower()
    best: Intent | None = None
    for name, family, pats in RULES:
        for p in pats:
            m = re.search(p, t, re.I)
            if not m:
                continue
            # longer, more specific matches win over short generic ones
            conf = min(0.96, 0.55 + 0.05 * len(m.group(0).split()) + 0.02 * len(p) / 10)
            if best is None or conf > best.confidence:
                best = Intent(name, family, conf, {}, m.group(0).strip())
            break
    if best is None:
        best = Intent("business_health", UNDERSTAND, 0.30, {}, "")
    best.slots = extract_slots(text)

    # a discount question with an explicit rupee-off in an evening window is
    # really the evening-offer action, not a deep-discount check
    if best.name == "discount_check" and best.slots.get("slot") == "evening" and "percent" not in best.slots:
        best.name, best.family = "create_offer", ACT
    return best
