"""The tool registry: everything the agent is allowed to do, and nothing else.

This module is the whole trust boundary. The model chooses which of these to
call and with what arguments; it never executes one, never writes SQL, never
does arithmetic on its own. Each entry carries a JSON Schema so the parameters
arrive typed rather than guessed out of the question text, which is what the
old planner had to do and mostly could not.

Two rules are enforced here rather than asked for in a prompt:

  * everything is READ-ONLY except `remember_fact` (writes a tier-C merchant
    statement) and `propose_action` (builds a proposal Python controls, which
    still cannot execute without spoken approval);
  * `get_optional_stock_context` and `stock_cover` are refused unless stock has
    actually been mentioned or already stated, so ordinary questions never turn
    into an inventory interrogation.

Every tool returns an Evidence envelope, so whatever the model eventually says
can be checked against the numbers that came back.
"""
from __future__ import annotations

import ast
import operator
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.analytics import money, outcome, patterns, trend  # noqa: E402
from backend.data import context as ctx, db, repository as repo  # noqa: E402
from backend.models.evidence import Evidence, ok, unavailable  # noqa: E402

STOCK_TOOLS = {"get_optional_stock_context", "stock_cover"}
WRITE_TOOLS = {"remember_fact"}
STOCK_WORDS = re.compile(
    r"\b(stock|inventory|bacha|bache|bachi|bachaa|quantity|units?|maal|"
    r"bottles?|pieces?|packets?|strips?|litres?|kg)\b", re.I)

# Tools that cannot run until something else has. The agent does not have to
# know this: the executor resolves it, transitively, before running the tool.
#
# `propose_action` needing the playbook is the one that matters. "offer bana
# de" makes calling propose_action directly the obvious move, and without the
# playbook it returns "no cohort-supported play" -- an empty result that looks
# exactly like the cohort having no evidence, rather than like a missing step.
PREREQUISITES: dict[str, tuple[str, ...]] = {
    "get_peer_relative_anomaly": ("get_peer_cohort",),
    "get_peer_playbook": ("get_peer_cohort",),
    "get_failed_plays": ("get_peer_cohort",),
    "propose_action": ("get_peer_cohort", "get_peer_playbook"),
}

# kept for callers that only ask "does this need the cohort?"
NEEDS_COHORT = {name for name, deps in PREREQUISITES.items()
                if "get_peer_cohort" in deps}


def prerequisites(name: str) -> list[str]:
    """Everything that must have run before `name`, in order, deduplicated."""
    order: list[str] = []

    def walk(tool: str) -> None:
        for dep in PREREQUISITES.get(tool, ()):
            walk(dep)
            if dep not in order:
                order.append(dep)

    walk(name)
    return order


# ---------------------------------------------------------------- schemas
def _p(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


DAYS = {"type": "integer", "minimum": 1, "maximum": 90,
        "description": "Number of days to look at or project."}

SPECS: list[dict] = [
    {
        "name": "get_merchant_context",
        "description": "The merchant's profile: trade, locality, volume band and how "
                       "many days of payment history exist. Cheap; call it when you "
                       "need to know who you are talking to.",
        "parameters": _p({}),
    },
    {
        "name": "sales_lookup",
        "description": "Actual takings for a named period: total rupees, transaction "
                       "count and average ticket. Use for 'how much today', 'what about "
                       "yesterday', 'this month so far', or any explicit date range. "
                       "This is the tool for a plain factual question about the money "
                       "that came in.",
        "parameters": _p({
            "period": {"type": "string",
                       "enum": ["today", "yesterday", "this_week", "last_week",
                                "this_month", "last_month", "last_7_days",
                                "last_30_days", "custom"],
                       "description": "Named period. Use 'custom' with start/end."},
            "start": {"type": "string", "description": "YYYY-MM-DD, only with custom."},
            "end": {"type": "string", "description": "YYYY-MM-DD, only with custom."},
        }, ["period"]),
    },
    {
        "name": "compare_periods",
        "description": "Compare takings between two periods and return the difference "
                       "in rupees and per cent. Use for 'is this week better than last "
                       "week', 'compared to last month'.",
        "parameters": _p({
            "period": {"type": "string",
                       "enum": ["today", "yesterday", "this_week", "last_week",
                                "this_month", "last_month", "last_7_days",
                                "last_30_days"]},
            "compare_to": {"type": "string",
                           "enum": ["yesterday", "last_week", "last_month",
                                    "previous_period", "same_period_last_month"]},
        }, ["period", "compare_to"]),
    },
    {
        "name": "get_sales_trend",
        "description": "Recent trend against the merchant's own baseline, broken down "
                       "by time-of-day band, with the worst band named. Use when asked "
                       "WHY takings changed, not just how much they were.",
        "parameters": _p({"window_days": DAYS, "baseline_days": DAYS}),
    },
    {
        "name": "get_business_health",
        "description": "Overall headline: revenue, transaction count, average ticket, "
                       "payment success rate and a steady/needs_attention verdict.",
        "parameters": _p({"window_days": DAYS}),
    },
    {
        "name": "get_time_patterns",
        "description": "Which hours and weekdays are strong or weak for this merchant. "
                       "Use for 'when is my rush', 'how are Saturdays'.",
        "parameters": _p({}),
    },
    {
        "name": "get_recent_situations",
        "description": "Business conditions already detected from payments in the last "
                       "N days (declines, surges, volatility).",
        "parameters": _p({"days": DAYS}),
    },
    {
        "name": "get_demand_forecast",
        "description": "Projected takings per day for the next N days, as a low/expected/"
                       "high band, plus the next rush window. This forecasts MONEY, not "
                       "units of any product.",
        "parameters": _p({"days": DAYS}),
    },
    {
        "name": "get_money_position",
        "description": "Money coming in over a horizon, and — only if bills are known — "
                       "the net position after them. Returns scope 'inflow_only' when "
                       "outgoings cannot be seen, which is a real answer, not a failure.",
        "parameters": _p({"days": DAYS}),
    },
    {
        "name": "afford_check",
        "description": "Can the merchant afford a specific spend? Give the amount in "
                       "rupees and the horizon. Returns a verdict, the expected buffer, "
                       "the buffer under a pessimistic forecast, the bills counted, and "
                       "how many days until it becomes safe. Use whenever a rupee figure "
                       "the merchant wants to SPEND appears in the question.",
        "parameters": _p({
            "amount": {"type": "number", "minimum": 1,
                       "description": "Rupees the merchant wants to spend."},
            "days": {"type": "integer", "minimum": 1, "maximum": 60,
                     "description": "Horizon. 'next month' is 30."},
        }, ["amount"]),
    },
    {
        "name": "get_peer_cohort",
        "description": "The privacy-safe cohort of similar merchants: how many, and on "
                       "what basis they are similar. Never returns names or identifiers.",
        "parameters": _p({}),
    },
    {
        "name": "get_peer_relative_anomaly",
        "description": "Is this merchant's change specific to them, or is the whole "
                       "area seeing it? The single most useful comparison available.",
        "parameters": _p({"window_days": DAYS}),
    },
    {
        "name": "get_peer_playbook",
        "description": "What similar merchants actually did in this situation and how "
                       "often it worked, as counts and rates. Never names anyone.",
        "parameters": _p({
            "situation_kind": {"type": "string",
                               "enum": ["evening_decline", "sales_decline",
                                        "demand_surge", "margin_pressure",
                                        "festive_window"],
                               "description": "Omit to use the detected situation."},
        }),
    },
    {
        "name": "get_failed_plays",
        "description": "How a family of actions has FAILED for similar merchants, "
                       "bucketed by size, with the safest bucket. Use when the merchant "
                       "proposes something risky, e.g. a big discount.",
        "parameters": _p({
            "action_family": {"type": "string",
                              "enum": ["discount", "offer", "price", "prep"]},
        }),
    },
    {
        "name": "get_local_pattern",
        "description": "How the merchant's own locality is trading overall right now.",
        "parameters": _p({}),
    },
    {
        "name": "get_cohort_seasonality",
        "description": "Weekday seasonality across the cohort, for forecasting.",
        "parameters": _p({}),
    },
    {
        "name": "get_cohort_profile",
        "description": "The cohort baseline, for a merchant with too little history of "
                       "their own. This is the cold-start answer.",
        "parameters": _p({}),
    },
    {
        "name": "get_action_history",
        "description": "This merchant's previously approved actions and measured results.",
        "parameters": _p({"limit": {"type": "integer", "minimum": 1, "maximum": 10}}),
    },
    {
        "name": "get_optional_stock_context",
        "description": "Stock on hand, from a connected POS feed or from what the "
                       "merchant recently said. Only available if stock was mentioned. "
                       "If nothing is known it returns unavailable and you should ask.",
        "parameters": _p({"subject": {"type": "string",
                                      "description": "The item, in the merchant's own "
                                                     "words, e.g. 'cold drink'."}}),
    },
    {
        "name": "stock_cover",
        "description": "How long stock will last: needs the quantity on hand and the "
                       "units sold per day. Returns days of cover, the day it runs out, "
                       "when to reorder, and the shortfall over the next week. Payment "
                       "data is in rupees and cannot supply the daily unit rate — if you "
                       "do not have it, ask the merchant for it.",
        "parameters": _p({
            "subject": {"type": "string", "description": "The item."},
            "quantity": {"type": "number", "description": "Units on hand. Omit to use "
                                                          "a stated or POS figure."},
            "units_per_day": {"type": "number",
                              "description": "Units sold per normal day, from the "
                                             "merchant. Omit only if already stated."},
        }, ["subject"]),
    },
    {
        "name": "calculate",
        "description": "Arithmetic over figures that are already in the evidence. Use "
                       "this instead of doing sums yourself — a number you computed in "
                       "your head is not grounded and will be rejected.",
        "parameters": _p({
            "expression": {"type": "string",
                           "description": "A plain arithmetic expression, e.g. "
                                          "'(5300 - 3992) * 7'. Digits and + - * / ( ) only."},
            "label": {"type": "string", "description": "What this figure represents."},
        }, ["expression", "label"]),
    },
    {
        "name": "remember_fact",
        "description": "Record something the merchant just told you that the payment "
                       "data cannot see — stock on hand, units sold per day, an upcoming "
                       "bill, seating capacity. Call this the moment they state it, then "
                       "carry on and answer. It expires on its own; never ask for a fact "
                       "already recorded.",
        "parameters": _p({
            "kind": {"type": "string",
                     "enum": ["stock_estimate", "daily_units", "capacity",
                              "upcoming_expense"]},
            "subject": {"type": "string", "description": "What it is about, e.g. "
                                                         "'cold drink'."},
            "value": {"type": "number", "description": "The number they gave."},
            "unit": {"type": "string", "description": "bottles, pieces, rupees, ..."},
            "utterance": {"type": "string", "description": "Their own words, verbatim."},
        }, ["kind", "subject", "value", "utterance"]),
    },
    {
        "name": "propose_action",
        "description": "Build an action proposal for the merchant to approve. You may "
                       "recommend, but the parameters are built in Python from what the "
                       "cohort actually did — you cannot set them. Requires a peer "
                       "playbook with enough evidence behind it.",
        "parameters": _p({}),
    },
]

BY_NAME = {s["name"]: s for s in SPECS}
TOOL_NAMES = [s["name"] for s in SPECS]


def schemas(allow_stock: bool = False) -> list[dict]:
    """The tool list offered to the model for this turn."""
    return [s for s in SPECS if allow_stock or s["name"] not in STOCK_TOOLS]


def stock_is_live(question: str, merchant_id: str) -> bool:
    """Stock tools are offered only when stock is genuinely on the table."""
    if STOCK_WORDS.search(question or ""):
        return True
    return any(r["kind"] in ("stock_estimate", "daily_units")
               for r in ctx.live_for(merchant_id))


# ------------------------------------------------------------------ dates
def resolve_period(period: str, start: str | None = None,
                   end: str | None = None) -> tuple[date, date, str]:
    """Named period -> inclusive day range. `today` is the ledger's today."""
    t = db.today()
    if period == "custom" and start and end:
        return date.fromisoformat(start), date.fromisoformat(end), f"{start} to {end}"
    if period == "today":
        return t, t, "today"
    if period == "yesterday":
        y = t - timedelta(days=1)
        return y, y, "yesterday"
    if period == "this_week":                        # Monday to today
        s = t - timedelta(days=t.weekday())
        return s, t, "this week"
    if period == "last_week":
        this_mon = t - timedelta(days=t.weekday())
        return this_mon - timedelta(days=7), this_mon - timedelta(days=1), "last week"
    if period == "this_month":
        return t.replace(day=1), t, "this month"
    if period == "last_month":
        first = t.replace(day=1)
        prev_end = first - timedelta(days=1)
        return prev_end.replace(day=1), prev_end, "last month"
    if period == "last_30_days":
        return t - timedelta(days=30), t - timedelta(days=1), "the last 30 days"
    return t - timedelta(days=7), t - timedelta(days=1), "the last 7 days"


def _shift(start: date, end: date, compare_to: str) -> tuple[date, date, str]:
    span = (end - start).days + 1
    if compare_to == "previous_period":
        return start - timedelta(days=span), start - timedelta(days=1), "the period before"
    if compare_to == "yesterday":
        y = db.today() - timedelta(days=1)
        return y, y, "yesterday"
    if compare_to == "last_week":
        return start - timedelta(days=7), end - timedelta(days=7), "the same days last week"
    if compare_to in ("last_month", "same_period_last_month"):
        return start - timedelta(days=30), end - timedelta(days=30), "a month earlier"
    return start - timedelta(days=span), start - timedelta(days=1), "the period before"


def _totals(merchant_id: str, start: date, end: date) -> dict:
    amount, txns, days = repo.window_totals(merchant_id, start, end)
    return {"total": round(amount, 0), "txns": txns, "days_open": days,
            "per_day": round(amount / days, 0) if days else 0.0,
            "avg_ticket": round(amount / txns, 1) if txns else None}


# -------------------------------------------------------------- arithmetic
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(expression: str) -> float:
    """Arithmetic only. No names, no calls, no attributes, no subscripts."""
    if len(expression) > 120:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("only numbers are allowed")
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("only + - * / and brackets are allowed")

    return ev(tree)
