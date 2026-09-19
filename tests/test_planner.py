"""The LLM may plan evidence retrieval, but Python owns its boundaries."""
import pytest

import config
from backend.data import repository as repo
from backend.reasoning import planner
from backend.reasoning import conversation, engine, llm
from backend.models.evidence import Evidence
from backend.reasoning.templates import TemplateReasoner


@pytest.fixture(autouse=True)
def stable_default_provider(monkeypatch):
    """Unit tests must not inherit a developer's live provider preference."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    planner._provider_cooldown_until.clear()


def test_gemini_plan_selects_tools_and_python_adds_dependencies(monkeypatch):
    monkeypatch.setattr(planner, "_gemini_json", lambda *args, **kwargs: {
        "intent": "sales_diagnosis",
        "tools": ["get_peer_relative_anomaly", "get_peer_playbook"],
        "rationale": "Compare the decline with similar merchants.",
    })
    plan = planner.GeminiPlanner().plan(
        "Meri sales achanak kyun kam hui?", repo.merchant_context(config.DEMO_MERCHANT))
    assert plan.method == "gemini"
    assert plan.intent == "sales_diagnosis"
    assert "get_merchant_context" in plan.tools
    assert "get_peer_cohort" in plan.tools


def test_inventory_tool_is_removed_without_an_explicit_stock_question(monkeypatch):
    monkeypatch.setattr(planner, "_gemini_json", lambda *args, **kwargs: {
        "intent": "planning",
        "tools": ["get_demand_forecast", "get_optional_stock_context"],
        "rationale": "Plan next week.",
    })
    plan = planner.GeminiPlanner().plan(
        "Agle hafte kya prepare karun?", repo.merchant_context(config.DEMO_MERCHANT))
    assert "get_demand_forecast" in plan.tools
    assert "get_optional_stock_context" not in plan.tools


def test_inventory_tool_is_allowed_when_merchant_explicitly_mentions_stock(monkeypatch):
    monkeypatch.setattr(planner, "_gemini_json", lambda *args, **kwargs: {
        "intent": "planning",
        "tools": ["get_optional_stock_context"],
        "rationale": "The merchant explicitly asked about stock.",
    })
    plan = planner.GeminiPlanner().plan(
        "Mera cold drink stock kitna bacha hai?",
        repo.merchant_context(config.DEMO_MERCHANT))
    assert "get_optional_stock_context" in plan.tools


def test_planner_cannot_propose_action_for_a_read_only_intent():
    plan = planner._validated({
        "intent": "time_pattern",
        "tools": ["get_time_patterns", "propose_action"],
        "rationale": "Find busy hours.",
    }, "Sabse busy time kya hai?", "gemini")
    assert "propose_action" not in plan.tools


def test_gemini_failure_falls_back_to_deterministic_rules(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(planner, "_gemini_json", fail)
    plan = planner.GeminiPlanner().plan(
        "sales kyun kam hain", repo.merchant_context(config.DEMO_MERCHANT))
    assert plan.method == "rule"
    assert plan.intent == "sales_diagnosis"


def test_high_consequence_action_intent_overrides_llm_misroute(monkeypatch):
    monkeypatch.setattr(planner, "_gemini_json", lambda *args, **kwargs: {
        "intent": "sales_diagnosis",
        "tools": ["get_sales_trend"],
        "rationale": "Incorrect model route.",
    })
    plan = planner.GeminiPlanner().plan(
        "Offer bana de", repo.merchant_context(config.DEMO_MERCHANT))
    assert plan.intent == "action_request"
    assert "propose_action" in plan.tools
    assert "get_peer_playbook" in plan.tools


def test_high_consequence_risk_intent_overrides_llm_misroute(monkeypatch):
    monkeypatch.setattr(planner, "_gemini_json", lambda *args, **kwargs: {
        "intent": "sales_diagnosis",
        "tools": ["get_sales_trend"],
        "rationale": "Incorrect model route.",
    })
    plan = planner.GeminiPlanner().plan(
        "30% discount doon?", repo.merchant_context(config.DEMO_MERCHANT))
    assert plan.intent == "risk_check"
    assert "get_failed_plays" in plan.tools


def test_engine_executes_the_planners_tool_selection(monkeypatch):
    chosen = planner.QueryPlan(
        intent="time_pattern",
        tools=["get_time_patterns"],
        method="gemini",
        rationale="The merchant asked when payments peak.",
        normalised="busy time",
    )
    class StaticPlanner:
        def plan(self, question, context, history=None):
            return chosen

        def review(self, question, context, plan, evidence, history=None):
            return [], "complete"

    monkeypatch.setattr(planner, "get_planner", lambda: StaticPlanner())
    monkeypatch.setattr(llm, "get_reasoner", lambda: TemplateReasoner())
    result = engine.ask(config.DEMO_MERCHANT, "Mera busy time kab hai?")
    assert result["intent"] == "time_pattern"
    assert {item["tool"] for item in result["evidence"]} == {
        "get_merchant_context", "get_time_patterns"
    }


def test_gemini_can_request_one_bounded_second_tool_wave(monkeypatch):
    monkeypatch.setattr(planner, "_gemini_json", lambda *args, **kwargs: {
        "additional_tools": ["get_peer_relative_anomaly", "get_time_patterns"],
        "rationale": "Compare the decline and locate its time window.",
    })
    plan = planner.QueryPlan("sales_diagnosis", ["get_sales_trend"],
                             method="gemini", review_after_tools=True)
    evidence = [
        Evidence("get_merchant_context", {"category": "food_stall"},
                 {"source": "test"}, "own_data"),
        Evidence("get_sales_trend", {"change_pct": -12},
                 {"source": "test"}, "own_data"),
    ]
    tools, rationale = planner.GeminiPlanner().review(
        "Sales kyun kam hai?", repo.merchant_context(config.DEMO_MERCHANT),
        plan, evidence)
    assert len(tools) <= 3
    assert "get_peer_relative_anomaly" in tools
    assert "get_peer_cohort" in tools
    assert "Compare" in rationale


def test_conversation_memory_is_compact_and_bounded():
    conversation.clear()
    for n in range(6):
        conversation.remember("demo-session", f"question {n}", "business_health",
                              f"answer {n}", ["get_business_health"])
    history = conversation.recent("demo-session")
    assert len(history) == 4
    assert history[0]["question"] == "question 2"
    assert set(history[0]) == {"question", "intent", "answer_summary", "evidence_tools"}


def test_auto_provider_falls_back_from_gemini_to_nvidia(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-test")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "nvidia-test")
    planner._provider_cooldown_until.clear()
    monkeypatch.setattr(planner, "_gemini_json",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("quota")))
    monkeypatch.setattr(planner, "_nvidia_json",
                        lambda *args, **kwargs: {"status": "ready"})
    result = planner._llm_json("system", "prompt", {"type": "object"}, 40)
    assert result["status"] == "ready"
    assert result["_provider"] == "nvidia-nim"


def test_nvidia_nim_uses_openai_compatible_json_request(monkeypatch):
    import httpx

    captured = {}

    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return {"choices": [{"message": {"content": '{"status":"ready"}'}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "test-key")
    result = planner._nvidia_json(
        "Return JSON", "status ready",
        {"type": "object", "properties": {"status": {"type": "string"}}}, 80)
    assert result == {"status": "ready"}
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == config.NVIDIA_MODEL
    assert captured["json"]["response_format"] == {"type": "json_object"}
