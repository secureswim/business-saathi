"""Deterministic fallback intent routing.

Gemini's planner is the primary route when enabled. These fixed rules keep the
demo available offline and take over automatically when Gemini is unavailable.

Order matters:
  * action_request before risk_check, because "offer bana de" contains "offer"
  * action_status before sales_diagnosis, because "pichli baar ka result"
    contains "result"
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
    "peer_insight", "time_pattern", "planning", "money_check", "unknown",
]

RULES: list[tuple[str, list[str]]] = [
    ("action_status", [r"pichl[ie].*(result|kya hua|kaisa raha)", r"last time.*result",
                       r"(offer|campaign).*(ka kya hua|result)",
                       r"jo (kiya|chalaya).*(uska|result)"]),
    ("action_request", [r"bana de", r"banao", r"bana do", r"chalu kar", r"shuru kar",
                        r"start (the )?(offer|campaign)", r"create (the )?(offer|campaign)",
                        r"kar do", r"kar de", r"launch", r"haan karo"]),
    ("risk_check", [r"\d+\s*%", r"discount d(oo|u)n", r"discount dena", r"daam badha",
                    r"price badha", r"rate badha", r"badha (doon|dun|du)",
                    r"safe hai", r"risk", r"theek rahega kya", r"nuksan",
                    r"kharid(oon|un|na)", r"inventory.*(loon|lun|kharid)", r"invest"]),
    ("what_if", [r"agar main", r"agar hum", r"what if", r"toh kya hoga", r"to kya hoga",
                 r"agar .*(badha|ghata|kam|zyada)"]),
    ("money_check", [r"money", r"cash", r"kitna (banega|aayega|bachega)",
                     r"paisa (theek|bachega|rahega)", r"kharcha", r"kharche",
                     r"expense", r"udhaar"]),
    ("planning", [r"prepare", r"tayyari", r"kya (karun|karoon|karna chahiye)",
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
                      r"kab (zyada|sabse)", r"peak time", r"busiest"]),
    ("anomaly_check", [r"unusual", r"normal hai", r"sab theek", r"kuch (gadbad|alag)",
                       r"koi problem", r"anything wrong"]),
    ("sales_diagnosis", [r"why", r"kam (hai|hain|ho gay)", r"gir (gay|rah)", r"drop",
                         r"down", r"ghat", r"kharab"]),
    ("business_health", [r"kaisa chal", r"kaisi chal", r"how is",
                         r"(business|sales|dukaan|shop|dhandha) kais",
                         r"health", r"halat", r"summary", r"update"]),
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


def classify(text: str, days_of_history: int = 365) -> dict:
    normalised = normalise(text)

    if days_of_history < 14:
        return {"intent": "cold_start", "method": "rule", "confidence": 1.0,
                "matched": "merchant has under 14 days of history",
                "normalised": normalised}

    for intent, patterns in RULES:
        for p in patterns:
            if re.search(p, normalised):
                return {"intent": intent, "method": "rule", "confidence": 0.9,
                        "matched": p, "normalised": normalised}

    return {"intent": "unknown", "method": "rule", "confidence": 0.0,
            "matched": None, "normalised": normalised}
