"""Deterministic fallback routing, for when no model provider is reachable.

This is NOT the normal path any more -- the agent is. It exists so the product
still answers on venue wifi with the uplink down, and every answer it produces
is labelled `offline` so nobody mistakes it for the real thing.

It is kept honest rather than clever: a question it cannot route now says so
and offers what it can do, instead of guessing an intent and confidently
answering something else.

Order matters:
  * action_request before risk_check, because "offer bana de" contains "offer"
  * action_status before sales_diagnosis, because "pichli baar ka result"
    contains "result"
  * sales_lookup before sales_diagnosis, because "aaj kitna hua" is a lookup,
    not a diagnosis
  * cold_start pre-empts everything when history is thin
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.reasoning import devanagari  # noqa: E402

ALIASES_PATH = Path(__file__).resolve().parent / "aliases.json"

INTENTS = [
    "cold_start", "action_status", "action_request", "risk_check", "what_if",
    "sales_diagnosis", "business_health", "anomaly_check", "demand_forecast",
    "peer_insight", "time_pattern", "planning", "money_check",
    "sales_lookup", "period_compare", "afford_check", "help", "unknown",
]

# Rupee amounts spoken or written: "50k", "50,000", "50 hazaar", "2 lakh".
AMOUNT = re.compile(
    r"(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|hazaar|hazar|thousand|lakh|lac|l)?\b",
    re.I)
HORIZON = re.compile(
    r"\b(agle|next|is|this)\s+(mahine|month|hafte|week|saal|year)\b", re.I)
SPEND = re.compile(
    r"\b(payment|pay|kharid|khareed|invest|de\s*(sakta|sakte|paunga)|"
    r"nikal|bhar|afford|kar\s*sakta)\b", re.I)

RULES: list[tuple[str, list[str]]] = [
    ("action_status", [r"pichl[ie].*(result|kya hua|kaisa raha)", r"last time.*result",
                       r"(offer|campaign).*(ka kya hua|result|ka update|ka status)",
                       r"(update|status) (do|de|dijiye|batao)",
                       r"jo (kiya|chalaya).*(uska|result)"]),
    ("action_request", [r"bana de", r"banao", r"bana do", r"chalu kar", r"shuru kar",
                        r"start (the )?(offer|campaign)", r"create (the )?(offer|campaign)",
                        r"kar do", r"kar de", r"launch", r"haan karo"]),
    ("risk_check", [r"\d+\s*%\s*(discount|chhoot|off|kam|badha)",
                    r"(discount|price|daam|rate)\D{0,12}\d+\s*%",
                    r"discount d(oo|u)n", r"discount dena", r"daam badha",
                    r"price badha", r"rate badha", r"badha (doon|dun|du)",
                    r"safe hai", r"risk", r"theek rahega kya", r"nuksan",
                    r"kharid(oon|un|na)", r"inventory.*(loon|lun|kharid)", r"invest"]),
    ("what_if", [r"agar main", r"agar hum", r"what if", r"toh kya hoga", r"to kya hoga",
                 r"agar .*(badha|ghata|kam|zyada)"]),
    ("money_check", [r"\bmoney\b", r"\bcash\b", r"kitna (banega|aayega|bachega)",
                     r"paisa (theek|bachega|rahega)", r"kharcha", r"kharche",
                     r"\bexpense\b", r"udhaar"]),
    ("planning", [r"(enough|kaafi|kafi|poora|pura)\s+stock",
                  r"stock (hai|bacha|kam|khatam)", r"stock kitna",
                  r"prepare", r"tayyari", r"kya (karun|karoon|karna chahiye)",
                  r"(agle|next).*(hafte|week|mahine).*(kya|prepare|tayyari)",
                  r"stock.*(kitna|badha|kam)", r"kitna (banau|banaun|rakhun)"]),
    ("demand_forecast", [r"tomorrow", r"agle hafte", r"agla hafta", r"next week",
                         r"aage kya", r"kaisa (rahega|reh sakta|hoga)", r"forecast",
                         r"rush", r"bheed", r"peak", r"busy kab", r"diwali",
                         r"tyohar", r"festive", r"demand"]),
    ("peer_insight", [r"mere jaise", r"dusre (shop|dukan|log)", r"similar (shops|merchants)",
                      r"aas paas", r"area mein kya", r"baaki log", r"competition",
                      r"market mein kya"]),
    ("time_pattern", [r"kis time", r"kaunse time", r"sabse zyada.*(time|kab)",
                      r"kab (zyada|sabse)", r"peak time", r"busiest",
                      r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                      r"somvar|mangalvar|budhvar|guruvar|shukravar|shanivar|ravivar|"
                      r"weekend|weekday)\b",
                      r"(subah|shaam|evening|morning|dopahar)\D{0,12}(kaisa|kitna|kab)"]),
    ("anomaly_check", [r"unusual", r"normal hai", r"sab theek", r"kuch (gadbad|alag)",
                       r"koi problem", r"anything wrong"]),
    ("help", [r"^\s*(namaste|namaskar|hello|hi|hey|salaam|ram ram)\b",
              r"(tum|aap|you)\s+kya\s+(kar|bata|help)",
              r"what can you do", r"\bhelp\b", r"kaise (use|istemal)",
              r"kya kya (kar|bata)"]),
    ("period_compare", [r"(se|than)\s+(better|behtar|accha|achha|zyada|kam)\b",
                        r"compar", r"(pichle|last)\s+(hafte|week|mahine|month)\s+se",
                        r"(vs|versus)\b"]),
    # Written against the NORMALISED text: hafta/hafte have already become
    # "week", bikri has become "sales", paisa has become "money".
    ("sales_lookup", [r"\bkitn\w*\b[^.?!]{0,24}\b(hua|hui|aaya|aayi|tha|thi|kiya|"
                      r"bika|business|sales|money|kamai)\b",
                      r"\b(business|sales|kamai|kamaya|total|takings)\b"
                      r"[^.?!]{0,16}\bkitn\w*\b",
                      r"\b(kamai|kamaya|takings)\b",
                      r"average (bill|ticket|sales)", r"avg (bill|ticket)",
                      r"kitne (customer|grahak|transaction|txn)"]),
    ("sales_diagnosis", [r"\bwhy\b", r"kam (hai|hain|ho gay)", r"gir (gay|rah|i|e)",
                         r"\bdrop\b", r"\bdown\b", r"ghat", r"kharab",
                         r"\d+\s*%\s*(kyun|kaise|kam|gir)"]),
    ("business_health", [r"kaisa chal", r"kaisi chal", r"\bhow is\b",
                         r"(business|sales|dukaan|shop|dhandha) kais",
                         r"\bhealth\b", r"halat", r"\bsummary\b",
                         r"\bupdate\b\s*(do|de|dijiye)?\s*$"]),
]


@lru_cache(maxsize=1)
def _aliases() -> dict:
    if ALIASES_PATH.exists():
        return json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    return {}


def normalise(text: str) -> str:
    """Fold transcription variants so a mis-heard word still routes correctly.

    Devanagari first: Sarvam returns hi-IN transcripts in Devanagari and every
    rule below is written in Latin Hinglish, so without this every spoken
    question routes to `unknown`.
    """
    text = devanagari.to_latin(text)
    t = " " + " ".join(text.lower().split()) + " "
    for canonical, variants in _aliases().items():
        for v in sorted(variants, key=len, reverse=True):
            t = re.sub(rf"(?<![a-z]){re.escape(v)}(?![a-z])", canonical, t)
    return t.strip()


KAL_PAST = re.compile(r"\bkal\b[^.?!]{0,24}\b(thi|tha|the|hua|hue|kitn|kamai|bika|"
                      r"raha|rahi)\b", re.I)
KAL_FUTURE = re.compile(r"\bkal\b[^.?!]{0,24}\b(hoga|hogi|rahega|rahegi|karna|karoon|"
                        r"karun|banau|banaun|chahiye)\b", re.I)


def kal_sense(text: str) -> str | None:
    """Hindi 'kal' is both yesterday and tomorrow; the verb decides which.

    Folding it to 'tomorrow' in the alias table meant "kal ki sale kitni thi"
    asked for a forecast of a day that has already happened."""
    if KAL_PAST.search(text):
        return "past"
    if KAL_FUTURE.search(text):
        return "future"
    return None


def parse_amount(text: str) -> float | None:
    """A rupee figure the merchant wants to spend. '50k' -> 50000."""
    best = None
    for m in AMOUNT.finditer(text or ""):
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        suffix = (m.group(2) or "").lower()
        if suffix in ("k", "hazaar", "hazar", "thousand"):
            value *= 1_000
        elif suffix in ("lakh", "lac", "l"):
            value *= 100_000
        if value >= 500 and (best is None or value > best):
            best = value
    return best


def parse_horizon(text: str) -> int | None:
    m = HORIZON.search(text or "")
    if not m:
        return None
    unit = m.group(2).lower()
    if unit in ("mahine", "month"):
        return 30
    if unit in ("hafte", "week"):
        return 7
    return 60


def classify(text: str, days_of_history: int = 365) -> dict:
    normalised = normalise(text)
    sense = kal_sense(normalised)

    if days_of_history < 14:
        return {"intent": "cold_start", "method": "rule", "confidence": 1.0,
                "matched": "merchant has under 14 days of history",
                "normalised": normalised}

    # A rupee amount the merchant wants to SPEND outranks everything else: it
    # is the one question where answering a generic seven-day position instead
    # is not a smaller answer, it is a different one.
    amount = parse_amount(normalised)
    if amount and SPEND.search(normalised):
        return {"intent": "afford_check", "method": "rule", "confidence": 0.95,
                "matched": "spend amount", "normalised": normalised,
                "params": {"purchase_amount": amount, "amount": amount,
                           "days": parse_horizon(normalised) or 30}}

    if sense == "past":
        return {"intent": "sales_lookup", "method": "rule", "confidence": 0.9,
                "matched": "kal (past tense)", "normalised": normalised,
                "params": {"period": "yesterday"}}
    if sense == "future":
        return {"intent": "demand_forecast", "method": "rule", "confidence": 0.9,
                "matched": "kal (future tense)", "normalised": normalised,
                "params": {"days": 1}}

    for intent, patterns in RULES:
        for p in patterns:
            if re.search(p, normalised):
                out = {"intent": intent, "method": "rule", "confidence": 0.9,
                       "matched": p, "normalised": normalised, "params": {}}
                if intent == "sales_lookup":
                    out["params"] = {"period": _period_of(normalised)}
                if intent == "period_compare":
                    period, compare_to = _compare_pair(normalised)
                    out["params"] = {"period": period, "compare_to": compare_to}
                return out

    return {"intent": "unknown", "method": "rule", "confidence": 0.0,
            "matched": None, "normalised": normalised, "params": {}}


# Checked in order, most specific first. Note every one is written against the
# NORMALISED text, where the alias table has already folded hafta -> hafte.
# NOTE these run against the NORMALISED text, where the alias table has already
# folded hafta/hafte -> "week" and pichle/pichla -> "pichli". Writing them in
# raw Hinglish looks right and matches nothing.
PERIODS = [
    ("last_week", r"\b(pichli|pichle|last)\s+(week|hafte|hafta)\b"),
    ("last_month", r"\b(pichli|pichle|last)\s+(month|mahine|mahina)\b"),
    ("this_week", r"\b(is|iss|ye|yeh|this)\s+(week|hafte|hafta)\b"),
    ("this_month", r"\b(is|iss|ye|yeh|this)\s+(month|mahine|mahina)\b"),
    ("today", r"\b(aaj|today)\b"),
    ("yesterday", r"\b(yesterday|beeta kal)\b"),
]


def _period_of(text: str) -> str:
    for name, pattern in PERIODS:
        if re.search(pattern, text, re.I):
            return name
    return "last_7_days"


def _compare_pair(text: str) -> tuple[str, str]:
    """'ye hafta pichle hafte se better hai kya' -> (this_week, last_week)."""
    found = [name for name, pattern in PERIODS if re.search(pattern, text, re.I)]
    if "this_week" in found and "last_week" in found:
        return "this_week", "last_week"
    if "this_month" in found and "last_month" in found:
        return "this_month", "last_month"
    if "today" in found and "yesterday" in found:
        return "today", "yesterday"
    return _period_of(text), "previous_period"


class OfflinePlan:
    """What the offline path decided. Deliberately the same shape the agent
    reports, so /ops renders both without a special case."""

    __slots__ = ("intent", "matched", "normalised", "params", "method")

    def __init__(self, raw: dict):
        self.intent = raw["intent"]
        self.matched = raw.get("matched")
        self.normalised = raw.get("normalised", "")
        self.params = raw.get("params") or {}
        self.method = "offline"

    def __repr__(self) -> str:                        # pragma: no cover
        return f"OfflinePlan({self.intent!r}, {self.matched!r})"


def route(question: str, days_of_history: int = 365) -> OfflinePlan:
    return OfflinePlan(classify(question, days_of_history))
