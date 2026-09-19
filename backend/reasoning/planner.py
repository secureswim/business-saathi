"""Gemini evidence planner with a deterministic offline fallback.

The model may select read-only evidence tools and extract bounded parameters.
It cannot execute tools, query SQL, perform arithmetic or construct an action.
Every returned plan is validated and dependency-expanded by Python.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.models.evidence import MerchantContext  # noqa: E402
from backend.reasoning import router, toolsets  # noqa: E402


PLANNER_TOOLS = sorted(toolsets.KNOWN_TOOLS)
STOCK_WORDS = re.compile(
    r"\b(stock|inventory|bacha|bache|bachi|quantity|units?|maal)\b", re.I
)
# These phrases express a transaction or safety decision where a wrong route
# changes application behaviour. The LLM still plans tools, while Python pins
# the intent and required evidence for these high-precision matches.
GUARDED_INTENTS = {"action_status", "action_request", "risk_check", "what_if"}

TOOL_DESCRIPTIONS = {
    "get_merchant_context": "Onboarding profile and days of payment history.",
    "get_business_health": "Payment revenue, transaction count and average ticket summary.",
    "get_sales_trend": "Recent payment trend versus the merchant's own baseline.",
    "get_time_patterns": "Strong and weak payment hours and weekdays.",
    "get_recent_situations": "Recent anomalies already detected from payments.",
    "get_peer_cohort": "Privacy-safe cohort of similar merchants.",
    "get_peer_relative_anomaly": "Whether a change is merchant-specific or area-wide.",
    "get_peer_playbook": "Actions that worked in similar payment situations.",
    "get_failed_plays": "Past action patterns that did not create lasting improvement.",
    "get_local_pattern": "Privacy-safe local payment pattern.",
    "get_cohort_seasonality": "Cohort payment seasonality for forecasting.",
    "get_cohort_profile": "Cohort baseline for a new merchant with little history.",
    "get_demand_forecast": "Forecast of future payment inflow, not inventory demand.",
    "get_money_position": "Payment inflow view; expenses only when separately available.",
    "get_optional_stock_context": "POS or merchant-reported stock; use only if stock is explicit.",
    "get_action_history": "Previously approved actions and measured outcomes.",
    "propose_action": "Build a Python-controlled proposal from a supported peer playbook.",
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": router.INTENTS},
        "tools": {"type": "array", "items": {"type": "string", "enum": PLANNER_TOOLS}},
        "days": {"type": "integer", "minimum": 1, "maximum": 30},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        "purchase_amount": {"type": "number", "minimum": 0},
        "situation_kind": {"type": "string"},
        "action_family": {"type": "string"},
        "rationale": {"type": "string"},
        "review_after_tools": {"type": "boolean"},
    },
    "required": ["intent", "tools", "rationale"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "additional_tools": {
            "type": "array", "maxItems": 3,
            "items": {"type": "string", "enum": PLANNER_TOOLS},
        },
        "rationale": {"type": "string"},
    },
    "required": ["additional_tools", "rationale"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You plan evidence retrieval for Business Saathi, a payment-data assistant.
Choose the minimum useful tools for the merchant's request. Payment data can support revenue,
transaction count, average ticket, timing, trends, forecasts and privacy-safe peer comparisons.
It cannot reveal products sold, inventory, margins, profit, cash sales or the cause of customer
behaviour. Never infer those unavailable facts. Use get_optional_stock_context only when the
merchant explicitly asks about stock or inventory. Use propose_action only when the merchant
asks to create an action or when a payment decline has evidence-supported recovery options.
The application executes and validates tools; you only return a plan. Set review_after_tools true
only for ambiguous diagnosis, risk or multi-part planning questions where seeing the first evidence
may reveal a genuinely useful second tool wave. Keep rationale brief."""


@dataclass
class QueryPlan:
    intent: str
    tools: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    method: str = "fallback"
    rationale: str = ""
    normalised: str = ""
    review_after_tools: bool = False

    def toolset(self) -> dict[str, list[str]]:
        return toolsets.for_tools(self.tools)


def _validated(raw: dict, question: str, method: str) -> QueryPlan:
    intent = raw.get("intent") if raw.get("intent") in router.INTENTS else "unknown"
    selected = [name for name in raw.get("tools", []) if name in toolsets.KNOWN_TOOLS]

    # Stock is outside the normal Soundbox data boundary. Even if the model asks
    # for it, retain it only when the merchant explicitly raised it.
    if not STOCK_WORDS.search(router.normalise(question)):
        selected = [name for name in selected if name != "get_optional_stock_context"]
    if intent not in toolsets.CAN_PROPOSE:
        selected = [name for name in selected if name != "propose_action"]

    safe = toolsets.for_tools(selected)
    tools = safe["wave1"] + safe["wave2"] + safe["wave3"]
    params = {}
    for key in ("days", "limit", "purchase_amount", "situation_kind", "action_family"):
        if key in raw:
            params[key] = raw[key]
    if "days" in params:
        params["days"] = max(1, min(30, int(params["days"])))
    if "limit" in params:
        params["limit"] = max(1, min(10, int(params["limit"])))
    if "purchase_amount" in params:
        params["purchase_amount"] = max(0.0, float(params["purchase_amount"]))
    return QueryPlan(intent=intent, tools=tools, params=params, method=method,
                     rationale=str(raw.get("rationale", ""))[:240],
                     normalised=router.normalise(question),
                     review_after_tools=bool(raw.get("review_after_tools", False)))


class FallbackPlanner:
    name = "rules"

    def plan(self, question: str, context: MerchantContext,
             history: list[dict] | None = None) -> QueryPlan:
        routed = router.classify(question, days_of_history=context.days_of_history)
        selected = toolsets.all_tools(routed["intent"])
        if STOCK_WORDS.search(routed["normalised"]):
            selected.append("get_optional_stock_context")
        return _validated({"intent": routed["intent"], "tools": selected,
                           "rationale": routed.get("matched") or "rule fallback"},
                          question, "rule")

    def review(self, question: str, context: MerchantContext, plan: QueryPlan,
               evidence: list, history: list[dict] | None = None) -> tuple[list[str], str]:
        return [], "offline plan is complete"


class GeminiPlanner:
    name = "gemini"

    def __init__(self):
        self._fallback = FallbackPlanner()

    def plan(self, question: str, context: MerchantContext,
             history: list[dict] | None = None) -> QueryPlan:
        if context.is_cold_start:
            return self._fallback.plan(question, context, history)
        try:
            prompt = json.dumps({
                "merchant_question": question,
                "merchant_context": context.to_dict(),
                "recent_conversation": history or [],
                "available_tools": TOOL_DESCRIPTIONS,
            }, ensure_ascii=False)
            raw = _llm_json(SYSTEM_PROMPT, prompt, PLAN_SCHEMA,
                            max_tokens=700, timeout=7.0)
            provider = raw.pop("_provider", "llm")
            routed = router.classify(question, days_of_history=context.days_of_history)
            if (routed["intent"] in GUARDED_INTENTS
                    and routed.get("matched")
                    and raw.get("intent") != routed["intent"]):
                model_intent = raw.get("intent", "unknown")
                raw["intent"] = routed["intent"]
                raw["tools"] = list(dict.fromkeys(
                    toolsets.all_tools(routed["intent"]) + list(raw.get("tools", []))))
                raw["rationale"] = (
                    f"Safety route {routed['intent']} over model route {model_intent}; "
                    + str(raw.get("rationale", "")))
            return _validated(raw, question, provider)
        except Exception:  # network/schema failure must never break the Soundbox
            return self._fallback.plan(question, context, history)

    def review(self, question: str, context: MerchantContext, plan: QueryPlan,
               evidence: list, history: list[dict] | None = None) -> tuple[list[str], str]:
        if not plan.review_after_tools:
            return [], "initial plan requested no review"
        try:
            executed = [e.tool if hasattr(e, "tool") else e.get("tool") for e in evidence]
            prompt = json.dumps({
                "merchant_question": question,
                "recent_conversation": history or [],
                "intent": plan.intent,
                "initial_plan": plan.tools,
                "evidence": [e.to_dict() if hasattr(e, "to_dict") else e for e in evidence],
                "available_tools": TOOL_DESCRIPTIONS,
                "instruction": "Select only genuinely missing tools. Return an empty list if sufficient.",
            }, ensure_ascii=False, default=str)
            raw = _llm_json(
                "Review the evidence plan. Request at most three additional tools only when their "
                "results could materially change the answer. Never request inventory unless the "
                "merchant explicitly mentioned stock. Do not repeat executed tools.",
                prompt, REVIEW_SCHEMA, max_tokens=400, timeout=5.0)
            candidate = _validated({"intent": plan.intent,
                                    "tools": raw.get("additional_tools", []),
                                    "rationale": raw.get("rationale", "")},
                                   question, "gemini-review")
            additional = [name for name in candidate.tools if name not in executed][:3]
            return additional, str(raw.get("rationale", ""))[:240]
        except Exception:
            return [], "review unavailable; continuing with initial evidence"


def _gemini_json(system: str, prompt: str, schema: dict, max_tokens: int,
                 timeout: float = 8.0) -> dict:
    import httpx

    def compatible(value):
        if isinstance(value, dict):
            # The v1beta generateContent endpoint rejects this JSON Schema key
            # even though newer Gemini documentation lists it as supported.
            return {k: compatible(v) for k, v in value.items()
                    if k not in {"additionalProperties"}}
        if isinstance(value, list):
            return [compatible(v) for v in value]
        return value

    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{config.GEMINI_MODEL}:generateContent")
    response = httpx.post(
        url,
        headers={"x-goog-api-key": config.GEMINI_API_KEY,
                 "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": compatible(schema),
                "maxOutputTokens": max_tokens,
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    if not text:
        raise ValueError("Gemini returned no text: " + json.dumps(payload)[:600])
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("Gemini returned non-JSON text: " + text[:300])
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Gemini returned a non-object")
    return value


def _nvidia_json(system: str, prompt: str, schema: dict, max_tokens: int,
                 timeout: float = 8.0) -> dict:
    """Call NVIDIA's hosted NIM through its OpenAI-compatible endpoint."""
    import httpx

    schema_prompt = (prompt + "\n\nReturn only a JSON object matching this JSON Schema:\n"
                     + json.dumps(schema, ensure_ascii=False))
    response = httpx.post(
        f"{config.NVIDIA_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.NVIDIA_API_KEY}",
                 "Content-Type": "application/json"},
        json={
            "model": config.NVIDIA_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": schema_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"NVIDIA NIM HTTP {response.status_code}: {response.text[:500]}")
    content = response.json()["choices"][0]["message"]["content"]
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content or "", re.S)
        if not match:
            raise ValueError("NVIDIA NIM returned no JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("NVIDIA NIM returned a non-object")
    return value


_provider_cooldown_until: dict[str, float] = {}


def _provider_order() -> list[str]:
    if config.LLM_PROVIDER == "gemini":
        return ["gemini"] if config.gemini_ready() else []
    if config.LLM_PROVIDER == "nvidia":
        return ["nvidia-nim"] if config.nvidia_ready() else []
    if config.LLM_PROVIDER == "nvidia-first":
        order = []
        if config.nvidia_ready():
            order.append("nvidia-nim")
        if config.gemini_ready():
            order.append("gemini")
        return order
    order = []
    if config.gemini_ready():
        order.append("gemini")
    if config.nvidia_ready():
        order.append("nvidia-nim")
    return order


def _llm_json(system: str, prompt: str, schema: dict, max_tokens: int,
              timeout: float = 8.0) -> dict:
    """Use the selected provider, with quota/network failover in auto mode."""
    now = time.monotonic()
    providers = _provider_order()
    errors = []
    for provider in providers:
        if _provider_cooldown_until.get(provider, 0) > now and len(providers) > 1:
            continue
        try:
            if provider == "gemini":
                value = _gemini_json(system, prompt, schema, max_tokens, timeout)
            else:
                value = _nvidia_json(system, prompt, schema, max_tokens, timeout)
            value["_provider"] = provider
            return value
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider}: {type(exc).__name__}")
            print(f"[llm] {provider} failed: {type(exc).__name__}: {str(exc)[:240]}")
            if config.LLM_PROVIDER in {"auto", "nvidia-first"}:
                cooldown = 20 if "503" in str(exc) else 300
                _provider_cooldown_until[provider] = now + cooldown
    raise RuntimeError("all LLM providers unavailable (" + ", ".join(errors) + ")")


def get_planner():
    return GeminiPlanner() if (config.USE_REAL_LLM and config.llm_ready()) else FallbackPlanner()
