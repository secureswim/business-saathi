"""A new merchant gets useful advice without being told we know them."""
import config
from backend.reasoning import engine

COLD = config.COLDSTART_MERCHANT


def test_cold_start_intent_wins_regardless_of_the_question():
    for q in ("sales kyun kam hain", "business kaisa chalega", "kya prepare karun"):
        assert engine.ask(COLD, q)["intent"] == "cold_start"


def test_cold_start_answer_is_useful():
    r = engine.ask(COLD, "business kaisa chalega")
    hi = r["answer"]["hinglish"]
    assert "similar" in hi or "area" in hi
    assert r["answer"]["validator"]["passed"]


def test_cold_start_does_not_claim_personal_knowledge():
    hi = engine.ask(COLD, "business kaisa chalega")["answer"]["hinglish"].lower()
    assert "aapka apna history abhi nahi hai" in hi


def test_cold_start_uses_the_cohort_not_the_merchant():
    r = engine.ask(COLD, "business kaisa chalega")
    tools = {e["tool"] for e in r["evidence"] if e["available"]}
    assert "get_cohort_profile" in tools
