"""CRITICAL. The validator makes "never fabricate" a property of the system."""
import config
from backend.models.evidence import Evidence
from backend.reasoning import engine, validator
from backend.reasoning.templates import TemplateReasoner


def _evidence():
    return [Evidence(tool="get_sales_trend",
                     value={"change_pct": -14.7, "current_daily": 4451,
                            "baseline_daily": 5216, "worst_band": "16-19",
                            "direction": "down", "worst_band_pct": -30.0},
                     basis={"source": "test"}, source="own_data")]


def test_empty_evidence_refuses_to_invent():
    out = TemplateReasoner().synthesize("sales_diagnosis", "sales kyun kam hain", [])
    assert validator.extract_numbers(out["hinglish"]) == []
    assert any(w in out["hinglish"].lower() for w in ("samajh", "nahi"))


def test_backed_figures_pass():
    ok, unbacked = validator.validate(
        "Sales 14.7% neeche hain, daily average Rs 4,451 hai.", _evidence())
    assert ok and not unbacked


def test_spoken_rounding_is_allowed():
    ok, _ = validator.validate("Daily average takreeban Rs 4,450 hai.", _evidence())
    assert ok


def test_fabricated_figure_is_caught():
    ok, unbacked = validator.validate(
        "Sales 63% neeche hain aur 912 naye customers aaye.", _evidence())
    assert not ok
    values = {u["value"] for u in unbacked}
    assert 63.0 in values or 912.0 in values


def test_internal_merchant_identifier_is_caught_even_when_number_is_small():
    ok, unbacked = validator.validate(
        "Similar shops include M004 and M008.", _evidence())
    assert not ok
    values = {u["value"] for u in unbacked}
    assert "M004" in values and "M008" in values


def test_durations_and_clock_times_are_not_business_claims():
    """The old gate checked every numeral, so "teen din" and "6 baje" counted
    as fabricated figures and threw away a perfectly good answer."""
    ok, unbacked = validator.validate(
        "Sales 14.7% neeche hain. Teen din mein pata chalega, shaam 6 baje se "
        "9 baje tak sabse zyada bikri hoti hai.", _evidence())
    assert ok, unbacked


def test_a_figure_from_the_merchants_own_question_is_grounded():
    ok, _ = validator.validate(
        "Haan, 50,000 nikal sakte hain.", _evidence(),
        question="kya main 50000 ka payment kar sakta hoon")
    assert ok


def test_a_figure_the_merchant_stated_is_grounded():
    ok, _ = validator.validate(
        "60 bottles bache hain.", _evidence(),
        stated=[{"value_num": 60.0}])
    assert ok


def test_every_intent_passes_the_validator_end_to_end():
    for q in ("sales kyun kam hain", "mera business kaisa chal raha hai",
              "mere jaise shops mein kya chal raha hai",
              "agle hafte ke liye kya prepare karun", "30% discount doon?",
              "offer bana de", "paisa theek rahega", "sabse zyada sales kis time",
              "aaj kuch unusual hai kya", "aaj mausam kaisa hai"):
        r = engine.ask(config.DEMO_MERCHANT, q)
        assert r["answer"]["validator"]["passed"], (q, r["answer"]["validator"])


def test_an_unanswerable_question_is_honest_and_does_not_loop():
    """It used to ask "sales ya paise?" -- a clarify loop for a question the
    merchant had already asked clearly. Now it says what it cannot see."""
    r = engine.ask(config.DEMO_MERCHANT, "mera profit kitna hai")
    text = r["answer"]["hinglish"].lower()
    assert "ya paise ke baare mein" not in text
    assert any(w in text for w in ("nahi", "payments"))
    assert r["answer"]["validator"]["passed"]
