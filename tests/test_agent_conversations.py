"""CRITICAL. The conversations the old architecture could not hold.

Each of these is a bug that was visible on stage: a question that fell outside
the thirteen intents and got a canned answer, a follow-up that re-ran the
previous question, a clarifying question asked twice, an amount the planner
could not extract.

The model is scripted (see `scripted` in conftest) so these run offline and
deterministically. Everything else is real: the loop, the tool execution, the
prerequisite insertion, the grounding gate, the repair path and the SQLite
conversation store. What is faked is only which tool the model decides to call,
which is the part a test should be pinning anyway.

A live-model smoke suite is at the bottom, behind SAATHI_LIVE_LLM=1.
"""
import os

import pytest

import config
from backend.data import context as ctx
from backend.reasoning import agent, conversation, engine, providers


@pytest.fixture(autouse=True)
def _clean_thread():
    """A stated fact outlives a test otherwise, and the NEXT test then sees
    stock as 'live' and gets offered tools it was asserting are hidden."""
    from backend.data import db

    conversation.clear()
    providers.reset_cooldowns()
    db.write("DELETE FROM merchant_inputs", ())
    yield
    conversation.clear()
    db.write("DELETE FROM merchant_inputs", ())


M = "M001"


# ---------------------------------------------------------------- 1. afford
def test_a_spend_amount_and_horizon_reach_the_tool(scripted):
    """'Can I make a 50k payment next month?' used to produce a generic
    seven-day net position: the rule planner could not extract an amount, and
    nothing ever asked the model for one."""
    llm = scripted([
        [("afford_check", {"amount": 50000, "days": 30})],
        {"final": {"hinglish": "Haan, 50,000 ka payment ho jayega.",
                   "english": "Yes, a 50,000 payment is affordable.",
                   "confidence": "high", "used_tools": ["afford_check"]}},
    ])
    r = engine.ask(M, "Kya main agle mahine 50k ka payment kar sakta hoon?",
                   conversation_id=conversation.new_id())

    assert llm.args_for("afford_check") == {"amount": 50000, "days": 30}
    value = next(e for e in r["evidence"] if e["tool"] == "afford_check")["value"]
    assert value["purchase"] == 50000
    assert value["days"] == 30
    assert value["verdict"] in ("comfortable", "tight", "unsafe")
    assert r["answer"]["validator"]["passed"]


def test_afford_check_names_the_bills_it_counted():
    from backend.reasoning import runner
    run = runner.Runner(M)
    value = run.call("afford_check", {"amount": 50000, "days": 30}).value
    assert value["scope"] == "net_position"
    assert value["bills"], "the merchant has obligations; they must be itemised"
    assert {b["label"] for b in value["bills"]} <= {"rent", "supplier", "salary",
                                                    "loan_emi"}


# ----------------------------------------------------------------- 2. stock
def test_stock_conversation_asks_once_then_uses_what_it_was_told(scripted):
    """Three turns. It used to answer the first with a forecast plus a hardcoded
    "cold drink kitna bacha hai", then give the same answer again when told."""
    cid = conversation.new_id()

    # turn 1: stock is mentioned, nothing is known, so it asks
    scripted([
        [("stock_cover", {"subject": "cold drink"})],
        {"final": {"hinglish": "Kitne cold drink bache hain?",
                   "english": "How many cold drinks are left?",
                   "confidence": "low", "ask": "Kitne cold drink bache hain?",
                   "ask_kind": "stock_estimate", "ask_subject": "cold drink"}},
    ])
    first = engine.ask(M, "Enough stock hai?", conversation_id=cid)
    assert first["ask"]["kind"] == "stock_estimate"

    # turn 2: they answer. The fact is recorded and the rate is still missing.
    llm2 = scripted([
        [("remember_fact", {"kind": "stock_estimate", "subject": "cold drink",
                            "value": 60, "unit": "bottles",
                            "utterance": "60 bottles"})],
        [("stock_cover", {"subject": "cold drink"})],
        {"final": {"hinglish": "Roz kitni bottles bikti hain?",
                   "english": "How many bottles sell per day?",
                   "confidence": "low", "ask": "Roz kitni bottles bikti hain?",
                   "ask_kind": "daily_units", "ask_subject": "cold drink"}},
    ])
    second = engine.ask(M, "60 bottles", conversation_id=cid)
    assert "remember_fact" in llm2.tools_called()
    stored = ctx.get(M, "stock_estimate", "cold drink")
    assert stored and stored["value_num"] == 60
    assert second["ask"]["kind"] == "daily_units"
    # it must NOT have repeated the first answer
    assert second["answer"]["hinglish"] != first["answer"]["hinglish"]

    # turn 3: the rate arrives and the question finally gets answered
    llm3 = scripted([
        [("remember_fact", {"kind": "daily_units", "subject": "cold drink",
                            "value": 20, "unit": "bottles",
                            "utterance": "roz 20 bikti hain"})],
        [("stock_cover", {"subject": "cold drink"})],
        {"final": {"hinglish": "60 bottles hain aur roz 20 bikti hain, "
                               "toh teen din chalega.",
                   "english": "60 bottles at 20 a day is about three days.",
                   "confidence": "high", "used_tools": ["stock_cover"]}},
    ])
    third = engine.ask(M, "roz 20 bikti hain", conversation_id=cid)
    cover = next(e for e in third["evidence"] if e["tool"] == "stock_cover")["value"]
    assert cover["available"]
    assert cover["quantity"] == 60 and cover["units_per_day"] == 20
    assert 2.0 <= cover["days_of_cover"] <= 4.0
    assert cover["runs_out_on"] and cover["reorder_by"]
    assert third["ask"] is None, "it must not ask again for something it was told"
    assert third["answer"]["validator"]["passed"]
    assert "stock_cover" in llm3.tools_called()


def test_stock_tools_are_not_even_offered_unless_stock_is_mentioned(scripted):
    """Ordinary questions must not turn into an inventory interrogation."""
    from backend.reasoning import tools as T
    assert not T.stock_is_live("sales kyun kam hain", M)
    offered = {s["name"] for s in T.schemas(allow_stock=False)}
    assert "stock_cover" not in offered
    assert "get_optional_stock_context" not in offered


# ------------------------------------------------------------ 3. diagnosis
def test_diagnosis_then_peer_followup_then_proposal(scripted):
    cid = conversation.new_id()

    scripted([
        [("get_sales_trend", {}), ("get_peer_cohort", {})],
        [("get_peer_relative_anomaly", {})],
        {"final": {"hinglish": "Sales apne normal se neeche hain, shaam mein "
                               "sabse zyada.",
                   "english": "Sales are below baseline, worst in the evening.",
                   "confidence": "high"}},
    ])
    engine.ask(M, "Sales kyun kam hai?", conversation_id=cid)

    # the follow-up must arrive with the thread attached
    llm2 = scripted([
        [("get_peer_playbook", {"situation_kind": "evening_decline"})],
        {"final": {"hinglish": "Aapke jaise dukaanon mein shaam ka offer chala.",
                   "english": "Similar shops ran an evening offer.",
                   "confidence": "high"}},
    ])
    engine.ask(M, "aur mere jaise logon ka?", conversation_id=cid)
    thread = str(llm2.seen[0])
    assert "Sales kyun kam hai?" in thread, "the follow-up did not see the thread"

    # and the proposal is built in Python, not by the model
    llm3 = scripted([
        [("get_peer_playbook", {"situation_kind": "evening_decline"})],
        [("propose_action", {})],
        {"final": {"hinglish": "Shaam ka offer bana diya hai, approve karein?",
                   "english": "I've prepared an evening offer. Approve?",
                   "confidence": "high", "recommends_action": True}},
    ])
    third = engine.ask(M, "theek hai offer bana do", conversation_id=cid)
    proposal = third["action_proposal"]
    assert proposal and proposal["type"] == "evening_offer"
    assert set(proposal["params"]) >= {"discount_rs", "window", "days"}
    assert proposal["evidence_summary"], "a proposal must carry its cohort evidence"


def test_the_model_cannot_set_action_parameters(scripted):
    """The model may recommend. The numbers come from the cohort, in Python."""
    scripted([
        [("get_peer_playbook", {"situation_kind": "evening_decline"})],
        # the model tries to dictate a 90% discount
        [("propose_action", {"discount_rs": 900, "days": 60})],
        {"final": {"hinglish": "Offer taiyar hai.", "english": "Offer ready.",
                   "confidence": "high"}},
    ])
    r = engine.ask(M, "offer bana do", conversation_id=conversation.new_id())
    params = r["action_proposal"]["params"]
    assert params["discount_rs"] != 900
    assert params["days"] != 60


# -------------------------------------------------------------- 4. lookups
def test_today_then_yesterday_as_a_followup(scripted):
    cid = conversation.new_id()
    scripted([
        [("sales_lookup", {"period": "today"})],
        {"final": {"hinglish": "Aaj abhi tak kuch nahi aaya.",
                   "english": "Nothing in yet today.", "confidence": "high"}},
    ])
    engine.ask(M, "Aaj kitna business hua?", conversation_id=cid)

    llm2 = scripted([
        [("sales_lookup", {"period": "yesterday"})],
        {"final": {"hinglish": "Kal 4,232 rupaye ka business hua.",
                   "english": "Yesterday was 4,232 rupees.", "confidence": "high"}},
    ])
    r = engine.ask(M, "aur kal?", conversation_id=cid)
    assert llm2.args_for("sales_lookup") == {"period": "yesterday"}
    lookup = next(e for e in r["evidence"] if e["tool"] == "sales_lookup")["value"]
    assert lookup["period"] == "yesterday"


# --------------------------------------------------------- 5. out of scope
def test_profit_is_answered_honestly_without_a_clarify_loop(scripted):
    scripted([{"final": {
        "hinglish": "Profit mujhe nahi dikhta — main sirf payments dekh sakta hoon. "
                    "Sales ya timing ke baare mein poochhiye.",
        "english": "I can't see profit, only payments. Ask about sales or timing.",
        "confidence": "high"}}])
    r = engine.ask(M, "Mera profit kitna hai?", conversation_id=conversation.new_id())
    text = r["answer"]["hinglish"].lower()
    assert "ya paise ke baare mein" not in text
    assert r["answer"]["validator"]["passed"]
    assert r["ask"] is None


# ------------------------------------------------------------- 6. greeting
def test_greeting_gets_help_not_a_clarify_loop(scripted):
    scripted([{"final": {
        "hinglish": "Namaste! Poochhiye — aaj kitna business hua, sales kyun kam hai, "
                    "ya mere jaise dukaanon mein kya chal raha hai.",
        "english": "Hello! Try: how much today, why are sales down, what are "
                   "similar shops doing.",
        "confidence": "high"}}])
    r = engine.ask(M, "Namaste", conversation_id=conversation.new_id())
    assert r["answer"]["validator"]["passed"]
    assert len(r["evidence"]) <= 2, "a greeting should not trigger a data sweep"


# ------------------------------------------------------------ 7. the repair
def test_an_unbacked_figure_is_repaired_not_replaced(scripted):
    """The old behaviour discarded the whole answer and spoke an unrelated
    template. That is the "it reverted to the original answer" symptom."""
    from backend.analytics import trend
    # read the real figure rather than hardcoding one: the generator is
    # seeded, but the exact decline moves when the population changes
    real = abs(trend.sales_trend(M)["value"]["change_pct"])
    scripted([
        [("get_sales_trend", {})],
        {"final": {"hinglish": "Sales 63% neeche hain aur profit 99,999 kam hua.",
                   "english": "Sales are down 63% and profit fell by 99,999.",
                   "confidence": "high"}},
        # the repair round: the model corrects itself
        {"text": '{"hinglish": "Sales %s%% neeche hain.", '
                 '"english": "Sales are down %s%%.", "confidence": "high"}'
                 % (real, real)},
    ])
    r = engine.ask(M, "sales kyun kam hain", conversation_id=conversation.new_id())
    answer = r["answer"]
    assert answer["validator"]["repairs"] >= 1
    assert answer["validator"]["passed"]
    assert "63" not in answer["hinglish"] and "99,999" not in answer["hinglish"]
    assert answer["validator"]["substituted"] is False


def test_an_unrepairable_answer_keeps_what_is_grounded(scripted):
    """If repair fails we still never speak a fabricated figure -- but we also
    never swap in an unrelated answer. We keep the true part and say so."""
    from backend.analytics import trend
    real = abs(trend.sales_trend(M)["value"]["change_pct"])
    bad = {"hinglish": f"Sales {real}% neeche hain. Profit 88,888 kam hua.",
           "english": f"Sales are down {real}%. Profit fell 88,888.",
           "confidence": "high"}
    scripted([
        [("get_sales_trend", {})],
        {"final": bad},
        {"text": '{"hinglish": "Profit 88,888 kam hua.", '
                 '"english": "Profit fell 88,888.", "confidence": "high"}'},
        {"text": '{"hinglish": "Profit 88,888 kam hua.", '
                 '"english": "Profit fell 88,888.", "confidence": "high"}'},
    ])
    r = engine.ask(M, "sales kyun kam hain", conversation_id=conversation.new_id())
    answer = r["answer"]
    assert "88,888" not in answer["hinglish"]
    assert answer["validator"]["degraded"] is True
    assert answer["validator"]["substituted"] is False


# ------------------------------------------------------------- 8. offline
def test_offline_answers_the_same_questions_and_says_it_is_offline(monkeypatch):
    from backend.reasoning import providers as P
    monkeypatch.setattr(P, "available", lambda: False)

    afford = engine.ask(M, "kya main agle mahine 50k ka payment kar sakta hoon")
    assert afford["answer"]["reasoner"] == "offline"
    value = next(e for e in afford["evidence"] if e["tool"] == "afford_check")["value"]
    assert value["purchase"] == 50000 and value["days"] == 30

    lookup = engine.ask(M, "kal ki sale kitni thi")
    assert lookup["intent"] == "sales_lookup"
    assert "4,2" in lookup["answer"]["hinglish"] or "Rs" in lookup["answer"]["hinglish"]

    hello = engine.ask(M, "tum kya kar sakte ho")
    assert hello["intent"] == "help"
    assert hello["answer"]["validator"]["passed"]


def test_offline_takes_over_when_every_provider_fails(monkeypatch):
    from backend.reasoning import providers as P

    def explode(*args, **kwargs):
        raise P.ProviderError("all providers failed")

    monkeypatch.setattr(P, "available", lambda: True)
    monkeypatch.setattr(P, "chat", explode)
    r = engine.ask(M, "sales kyun kam hain", conversation_id=conversation.new_id())
    assert r["answer"]["reasoner"] == "offline"
    assert r["answer"]["validator"]["passed"]


# --------------------------------------------------------------- the loop
def test_the_loop_respects_its_budget(scripted, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_ROUNDS", 2)
    llm = scripted([
        [("get_sales_trend", {})],
        [("get_business_health", {})],
        [("get_time_patterns", {})],          # never reached
    ])
    r = engine.ask(M, "sales kyun kam hain", conversation_id=conversation.new_id())
    executed = [e["tool"] for e in r["evidence"]]
    assert "get_time_patterns" not in executed, executed
    assert r["answer"]["agent"]["rounds"] <= 2
    assert r["answer"]["hinglish"]


def test_a_prerequisite_is_inserted_without_the_model_asking(scripted):
    """The model asks for a peer playbook without a cohort. Python fetches the
    cohort rather than making that the model's problem."""
    scripted([
        [("get_peer_playbook", {"situation_kind": "evening_decline"})],
        {"final": {"hinglish": "Mil gaya.", "english": "Got it.",
                   "confidence": "high"}},
    ])
    r = engine.ask(M, "mere jaise dukaanon mein kya chala",
                   conversation_id=conversation.new_id())
    tools = [e["tool"] for e in r["evidence"]]
    assert "get_peer_cohort" in tools
    assert tools.index("get_peer_cohort") < tools.index("get_peer_playbook")


def test_conversation_memory_stays_compact():
    cid = conversation.new_id()
    for i in range(config.CONVERSATION_TURNS + 6):
        conversation.remember(cid, M, f"q{i}", f"a{i}", [])
    turns = conversation.recent(cid)
    assert len(turns) == config.CONVERSATION_TURNS
    assert turns[-1]["question"] == f"q{config.CONVERSATION_TURNS + 5}"


def test_tool_summaries_carry_values_but_never_whole_evidence():
    from backend.reasoning import runner
    run = runner.Runner(M)
    run.call("get_peer_cohort", {})
    summary = conversation.summarise(run.evidence)
    blob = str(summary)
    assert "cohort_size" in blob
    assert "M004" not in blob and "basis" not in blob


# ------------------------------------------------------------ live smoke
live = pytest.mark.skipif(os.getenv("SAATHI_LIVE_LLM") != "1",
                          reason="set SAATHI_LIVE_LLM=1 to spend live credits")


@live
def test_live_model_answers_the_three_demo_questions():
    for question in ("Bhai iss hafte sales kyun kam hain?",
                     "Kya main agle mahine 50k ka payment kar sakta hoon?",
                     "Mera profit kitna hai?"):
        r = engine.ask(M, question, conversation_id=conversation.new_id())
        assert r["answer"]["hinglish"].strip()
        assert r["answer"]["validator"]["passed"], (question, r["answer"]["validator"])
        assert r["elapsed_ms"] < 15000, (question, r["elapsed_ms"])
