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
    assert 63.0 in unbacked or 912.0 in unbacked


def test_internal_merchant_identifier_is_caught_even_when_number_is_small():
    ok, unbacked = validator.validate(
        "Similar shops include M004 and M008.", _evidence())
    assert not ok
    assert "M004" in unbacked and "M008" in unbacked


def test_every_intent_passes_the_validator_end_to_end():
    for q in ("sales kyun kam hain", "mera business kaisa chal raha hai",
              "mere jaise shops mein kya chal raha hai",
              "agle hafte ke liye kya prepare karun", "30% discount doon?",
              "offer bana de", "paisa theek rahega", "sabse zyada sales kis time",
              "aaj kuch unusual hai kya", "aaj mausam kaisa hai"):
        r = engine.ask(config.DEMO_MERCHANT, q)
        assert r["answer"]["validator"]["passed"], (q, r["answer"]["validator"])


def test_unknown_intent_asks_rather_than_guessing():
    r = engine.ask(config.DEMO_MERCHANT, "aaj mausam kaisa hai")
    assert r["intent"] == "unknown"
    assert validator.extract_numbers(r["answer"]["hinglish"]) == []
