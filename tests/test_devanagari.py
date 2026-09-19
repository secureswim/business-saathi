"""Spoken questions must route the same as typed ones.

Sarvam's `saarika` returns hi-IN transcripts in DEVANAGARI while every rule in
router.py is Latin Hinglish. Before transliteration, every single spoken
question routed to `unknown` and the merchant got asked to clarify -- in a loop,
because the clarification was spoken too. These tests exist so that cannot
come back silently.
"""
import pytest

from backend.reasoning.devanagari import has_devanagari, to_latin
from backend.reasoning.router import classify

SPOKEN = [
    ("सेल्स क्यों कम है", "sales_diagnosis"),
    ("मेरे जैसे शॉप में क्या चल रहा है", "peer_insight"),
    ("अगले हफ्ते के लिए क्या तैयारी करूं", "planning"),
    ("ऑफर बना दे", "action_request"),
    ("पैसा ठीक रहेगा अगले हफ्ते", "money_check"),
    ("बिजनेस कैसा चल रहा है", "business_health"),
    ("कौनसे टाइम सबसे ज्यादा बिक्री होती है", "time_pattern"),
    ("तीस परसेंट डिस्काउंट दूं", "risk_check"),
    ("अगले हफ्ते कैसा रहेगा", "demand_forecast"),
    ("पिछली बार का रिजल्ट क्या हुआ", "action_status"),
    ("सब ठीक है ना", "anomaly_check"),
]


@pytest.mark.parametrize("spoken,expected", SPOKEN)
def test_devanagari_routes_like_hinglish(spoken, expected):
    assert classify(spoken)["intent"] == expected, to_latin(spoken)


def test_no_spoken_question_falls_through_to_unknown():
    """The specific failure seen on stage: everything became a clarify loop."""
    intents = {classify(s)["intent"] for s, _ in SPOKEN}
    assert "unknown" not in intents


@pytest.mark.parametrize("word,latin", [
    ("कम", "kam"),         # final schwa deleted
    ("अगले", "agle"),      # internal schwa deleted
    ("अगर", "agar"),       # ...but not when the final one already went
    ("सबसे", "sabse"),     # never the first syllable, or this is "sbse"
    ("क्या", "kya"),       # a WRITTEN aa is not a schwa and must survive
    ("रहा", "raha"),
])
def test_schwa_deletion(word, latin):
    assert to_latin(word) == latin


def test_latin_passes_through_untouched():
    typed = "sales kyun kam hai"
    assert to_latin(typed) == typed
    assert not has_devanagari(typed)


def test_typed_hinglish_still_routes_the_same():
    """The transliteration must not disturb the path the demo script uses."""
    assert classify("sales kyun kam hai")["intent"] == "sales_diagnosis"
    assert classify("mere jaise shops mein kya chal raha hai")["intent"] == "peer_insight"
    assert classify("offer bana de")["intent"] == "action_request"
