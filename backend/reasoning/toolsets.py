"""intent -> fixed tool set, in dependency waves.

Wave 1 tools are independent and run concurrently. Wave 2 tools need the
cohort, so they run after wave 1. This two-wave shape is worth roughly 600ms
of the latency budget versus running everything serially.
"""
from __future__ import annotations

# wave 1: independent    wave 2: needs the cohort from get_peer_cohort
TOOLSETS: dict[str, dict[str, list[str]]] = {
    "business_health": {
        "wave1": ["get_merchant_context", "get_business_health", "get_peer_cohort"],
        "wave2": ["get_peer_relative_anomaly"],
    },
    "sales_diagnosis": {
        "wave1": ["get_merchant_context", "get_sales_trend", "get_peer_cohort"],
        "wave2": ["get_peer_relative_anomaly", "get_peer_playbook"],
        "wave3": ["propose_action"],
    },
    "anomaly_check": {
        "wave1": ["get_merchant_context", "get_business_health", "get_recent_situations",
                  "get_peer_cohort"],
        "wave2": ["get_peer_relative_anomaly"],
    },
    "peer_insight": {
        "wave1": ["get_merchant_context", "get_peer_cohort"],
        "wave2": ["get_local_pattern", "get_peer_playbook"],
    },
    "time_pattern": {
        "wave1": ["get_merchant_context", "get_time_patterns"],
        "wave2": [],
    },
    "demand_forecast": {
        "wave1": ["get_merchant_context", "get_demand_forecast", "get_time_patterns",
                  "get_peer_cohort"],
        "wave2": ["get_cohort_seasonality"],
    },
    "planning": {
        "wave1": ["get_merchant_context", "get_demand_forecast", "get_peer_cohort"],
        "wave2": ["get_cohort_seasonality", "get_peer_playbook"],
    },
    "risk_check": {
        "wave1": ["get_merchant_context", "get_sales_trend", "get_peer_cohort",
                  "get_money_position"],
        "wave2": ["get_failed_plays"],
    },
    "what_if": {
        "wave1": ["get_merchant_context", "get_peer_cohort"],
        "wave2": ["get_failed_plays", "get_peer_playbook"],
    },
    "money_check": {
        "wave1": ["get_merchant_context", "get_money_position", "get_demand_forecast"],
        "wave2": [],
    },
    "action_request": {
        "wave1": ["get_merchant_context", "get_sales_trend", "get_peer_cohort"],
        "wave2": ["get_peer_playbook"],
        "wave3": ["propose_action"],
    },
    "action_status": {
        "wave1": ["get_merchant_context", "get_action_history"],
        "wave2": [],
    },
    "cold_start": {
        "wave1": ["get_merchant_context"],
        "wave2": ["get_cohort_profile"],
    },
    "unknown": {"wave1": [], "wave2": []},
}

# intents that may end in an action proposal
CAN_PROPOSE = {"sales_diagnosis", "action_request", "planning"}


def for_intent(intent: str) -> dict[str, list[str]]:
    ts = dict(TOOLSETS.get(intent, TOOLSETS["unknown"]))
    ts.setdefault("wave3", [])
    return ts


def all_tools(intent: str) -> list[str]:
    ts = for_intent(intent)
    return ts["wave1"] + ts["wave2"] + ts["wave3"]


# Dependency order for a model-created plan. The model selects tools, but this
# table decides execution order and silently inserts required prerequisites.
WAVE1 = {
    "get_merchant_context", "get_business_health", "get_sales_trend",
    "get_time_patterns", "get_recent_situations", "get_peer_cohort",
    "get_demand_forecast", "get_money_position", "get_optional_stock_context",
    "get_action_history",
}
WAVE2 = {
    "get_peer_relative_anomaly", "get_peer_playbook", "get_failed_plays",
    "get_local_pattern", "get_cohort_seasonality", "get_cohort_profile",
}
WAVE3 = {"propose_action"}
KNOWN_TOOLS = WAVE1 | WAVE2 | WAVE3
NEEDS_COHORT = {
    "get_peer_relative_anomaly", "get_peer_playbook", "get_failed_plays",
    "propose_action",
}


def for_tools(selected: list[str]) -> dict[str, list[str]]:
    """Turn an untrusted model selection into a safe dependency-ordered plan."""
    clean = [name for name in selected if name in KNOWN_TOOLS]
    if "get_merchant_context" not in clean:
        clean.insert(0, "get_merchant_context")
    if any(name in NEEDS_COHORT for name in clean) and "get_peer_cohort" not in clean:
        clean.insert(1, "get_peer_cohort")
    if "propose_action" in clean and "get_peer_playbook" not in clean:
        clean.append("get_peer_playbook")
    clean = list(dict.fromkeys(clean))
    return {
        "wave1": [name for name in clean if name in WAVE1],
        "wave2": [name for name in clean if name in WAVE2],
        "wave3": [name for name in clean if name in WAVE3],
    }
