"""The tools, and the runner that executes them.

The model calls these; it never touches SQL and never does arithmetic. Every
tool returns an Evidence envelope. `available: False` is a first-class result,
not an error -- the answer omits that dimension rather than hedging about it,
and /ops renders a greyed card so "why didn't it mention cash?" has a visible
answer.

Two entry points, and they are not equals. `call()` executes one tool with its
own typed arguments and is what the agent loop uses. `run_tools()` executes a
fixed dependency-ordered set and exists only for the offline fallback, for when
no model provider can be reached.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.analytics import anomaly, money, outcome, patterns, trend  # noqa: E402
from backend.data import context as ctx, db, repository as repo  # noqa: E402
from backend.graph import privacy  # noqa: E402
from backend.graph.store import get_store  # noqa: E402
from backend.models.evidence import Evidence, MerchantContext, ok, unavailable  # noqa: E402
from backend.reasoning import toolsets  # noqa: E402

# which stock item a merchant would plausibly be asked about, by category
ASK_SUBJECT = {
    "food_stall": "cold drink",
    "kirana": "cooking oil",
    "pharmacy": "paracetamol",
    "mobile_accessories": "charging cable",
    "salon": "hair colour",
}


class Runner:
    """Executes a toolset, accumulating evidence and streaming it out."""

    def __init__(self, merchant_id: str, emit=None, params: dict | None = None):
        self.merchant_id = merchant_id
        self.emit = emit or (lambda ev: None)
        self.params = params or {}
        self.evidence: list[Evidence] = []
        self.by_tool: dict[str, Evidence] = {}
        self.store = get_store()
        self.context: MerchantContext | None = None
        self.ops_values: dict[str, dict] = {}   # richer values for /ops only
        self._called: set = set()               # (tool, args) already executed

    # ------------------------------------------------------------- plumbing
    def _add(self, ev: Evidence, ops_value: dict | None = None) -> Evidence:
        self.evidence.append(ev)
        self.by_tool[ev.tool] = ev
        if ops_value is not None:
            self.ops_values[ev.tool] = ops_value
        self.emit(ev)
        return ev

    def value(self, tool: str) -> dict | None:
        ev = self.by_tool.get(tool)
        return ev.value if ev and ev.available else None

    def _cohort(self) -> tuple[list[str], list[str], str]:
        v = self.value("get_peer_cohort") or {}
        return (v.get("peer_ids", []) or self.ops_values.get("get_peer_cohort", {}).get("peer_ids", []),
                v.get("extended_ids", []) or self.ops_values.get("get_peer_cohort", {}).get("extended_ids", []),
                v.get("cohort_key", "") or self.ops_values.get("get_peer_cohort", {}).get("cohort_key", ""))

    # ------------------------------------------------------- agent entry point
    def call(self, name: str, args: dict | None = None) -> Evidence:
        """Execute ONE tool with its own arguments and return its Evidence.

        This is what the agent loop uses. Arguments are merged into the run's
        parameter bag so the existing tool bodies keep working unchanged, and a
        prerequisite the model did not think to ask for is fetched here rather
        than being the model's problem.
        """
        from backend.reasoning import tools as toolspec

        args = {k: v for k, v in (args or {}).items() if v is not None}
        if name not in toolspec.BY_NAME:
            return unavailable(name, "unknown tool", "own_data")

        # a tool may be called again with different arguments; a repeat with the
        # same arguments is served from what we already have
        signature = (name, tuple(sorted(args.items(), key=lambda kv: kv[0])))
        if signature in self._called:
            return self.by_tool[name]

        if self.context is None and name != "get_merchant_context":
            self._run_one("get_merchant_context")
        if name in toolspec.NEEDS_COHORT and "get_peer_cohort" not in self.by_tool:
            self._run_one("get_peer_cohort")

        self.params.update(args)
        before = len(self.evidence)
        method = getattr(self, f"_a_{name}", None)
        try:
            if method is not None:
                method(args)
            else:
                fn = getattr(self, f"_t_{name}", None)
                if fn is None:
                    return unavailable(name, "tool not implemented", "own_data")
                self.by_tool.pop(name, None)
                fn()
        except Exception as exc:                      # noqa: BLE001
            return self._add(unavailable(name, f"tool error: {type(exc).__name__}: {exc}",
                                         "own_data"))
        self._called.add(signature)
        if len(self.evidence) > before:
            return self.evidence[-1]
        return self.by_tool.get(name) or unavailable(name, "no result", "own_data")

    def run(self, intent: str) -> list[Evidence]:
        return self.run_tools(toolsets.all_tools(intent))

    def run_tools(self, selected: list[str]) -> list[Evidence]:
        """Execute a validated planner selection in dependency waves."""
        ts = toolsets.for_tools(selected)
        for wave in (ts["wave1"], ts["wave2"], ts.get("wave3", [])):
            if not wave:
                continue
            if len(wave) == 1:
                self._run_one(wave[0])
            else:
                with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                    list(pool.map(self._run_one, wave))
        return self.evidence

    def _run_one(self, tool: str) -> None:
        if tool in self.by_tool:
            return
        # argument-taking tools take theirs from the run's parameter bag, which
        # the offline router fills in from the question
        arg_fn = getattr(self, f"_a_{tool}", None)
        fn = getattr(self, f"_t_{tool}", None)
        if arg_fn is None and fn is None:
            return
        try:
            if arg_fn is not None:
                arg_fn(dict(self.params))
            else:
                fn()
        except Exception as exc:                      # noqa: BLE001
            self._add(unavailable(tool, f"tool error: {type(exc).__name__}: {exc}",
                                  "own_data"))

    # ---------------------------------------------------------------- tools
    def _t_get_merchant_context(self) -> None:
        self.context = repo.merchant_context(self.merchant_id)
        self._add(Evidence(
            tool="get_merchant_context", value=self.context.to_dict(),
            basis={"source": "repository.merchant_context",
                   "note": "category, locality and volume band come from onboarding; "
                           "days_of_history is counted from the transaction ledger"},
            source="own_data", tier="A"))

    def _t_get_business_health(self) -> None:
        if repo.days_of_history(self.merchant_id) < 7:
            self._add(unavailable("get_business_health", "under 7 days of history",
                                  "own_data"))
            return
        self._add(ok("get_business_health",
                     trend.business_health(self.merchant_id,
                                           int(self.params.get("window_days", 7))),
                     "own_data"))

    def _t_get_sales_trend(self) -> None:
        if repo.days_of_history(self.merchant_id) < 14:
            self._add(unavailable("get_sales_trend", "under 14 days of history",
                                  "own_data"))
            return
        self._add(ok("get_sales_trend",
                     trend.sales_trend(self.merchant_id,
                                       int(self.params.get("window_days", 7)),
                                       int(self.params.get("baseline_days", 30))),
                     "own_data"))

    def _t_get_time_patterns(self) -> None:
        r = patterns.time_patterns(self.merchant_id)
        if not r["value"].get("available"):
            self._add(unavailable("get_time_patterns", r["value"]["reason"], "own_data"))
            return
        self._add(ok("get_time_patterns", r, "own_data"))

    def _t_get_recent_situations(self) -> None:
        rows = repo.recent_situations(self.merchant_id,
                                      days=int(self.params.get("days", 30)))
        if not rows:
            self._add(unavailable("get_recent_situations", "nothing detected in 30 days",
                                  "own_data"))
            return
        self._add(Evidence(
            tool="get_recent_situations",
            value={"available": True, "count": len(rows), "window_days": 30,
                   "situations": [{"kind": r["kind"], "detected_on": r["detected_on"],
                                   "severity": r["severity"], "band": r["band"],
                                   "peer_relative": r["peer_relative"]} for r in rows[:5]]},
            basis={"source": "repository.recent_situations", "window_days": 30},
            source="own_data", tier="A"))

    def _t_get_peer_cohort(self) -> None:
        raw = self.store.peers(self.merchant_id)
        ids = raw["value"]["peer_ids"]
        if len(ids) < config.MIN_COHORT_SIZE:
            self._add(unavailable(
                "get_peer_cohort",
                f"cohort of {len(ids)} is below the privacy floor of {config.MIN_COHORT_SIZE}",
                "graph", extra_basis={"min_cohort_size": config.MIN_COHORT_SIZE}))
            return
        split = privacy.split_for_transport(raw, ids)
        # ids are kept out of `value` (merchant-facing) but retained for the runner
        # and for /ops via ops_values.
        ev = Evidence(tool="get_peer_cohort", value=split["value"], basis=split["basis"],
                      source="graph", tier="A")
        self._add(ev, ops_value=raw["value"])

    def _t_get_peer_relative_anomaly(self) -> None:
        peer_ids, _, _ = self._cohort()
        if not peer_ids:
            self._add(unavailable("get_peer_relative_anomaly", "no cohort available",
                                  "graph"))
            return
        r = anomaly.peer_relative_anomaly(self.merchant_id, peer_ids,
                                          int(self.params.get("window_days", 7)))
        if not r["value"].get("conclusive"):
            self._add(unavailable("get_peer_relative_anomaly", r["value"]["reason"],
                                  "graph"))
            return
        self._add(ok("get_peer_relative_anomaly", r, "graph"))

    def _t_get_peer_playbook(self) -> None:
        peer_ids, _, _ = self._cohort()
        if not peer_ids:
            self._add(unavailable("get_peer_playbook", "no cohort available", "graph"))
            return
        kind = self.params.get("situation_kind") or self._situation_kind()
        r = self.store.peer_playbook(kind, peer_ids)
        if not r["value"].get("options"):
            self._add(unavailable("get_peer_playbook",
                                  f"no cohort experience for '{kind}'", "graph"))
            return
        self._add(ok("get_peer_playbook", r, "graph"))

    def _t_get_failed_plays(self) -> None:
        peer_ids, extended, _ = self._cohort()
        ids = peer_ids + extended
        if len(ids) < config.MIN_COHORT_SIZE:
            self._add(unavailable("get_failed_plays", "cohort too small", "graph"))
            return
        family = self.params.get("action_family", "discount")
        r = self.store.failed_plays(family, ids)
        if not r["value"].get("evidence"):
            self._add(unavailable("get_failed_plays",
                                  r["value"].get("reason", "no matching actions"), "graph"))
            return
        self._add(ok("get_failed_plays", r, "graph"))

    def _t_get_local_pattern(self) -> None:
        c = self.context or repo.merchant_context(self.merchant_id)
        r = self.store.local_pattern(c.category, c.locality)
        if r["value"].get("available") is False:
            self._add(unavailable("get_local_pattern", r["value"]["reason"], "graph"))
            return
        self._add(ok("get_local_pattern", r, "graph"))

    def _t_get_cohort_seasonality(self) -> None:
        c = self.context or repo.merchant_context(self.merchant_id)
        r = self.store.cohort_seasonality(c.category, c.locality_type, 7)
        if r["value"].get("available") is False:
            self._add(unavailable("get_cohort_seasonality", r["value"]["reason"], "graph"))
            return
        self._add(ok("get_cohort_seasonality", r, "graph"))

    def _t_get_cohort_profile(self) -> None:
        c = self.context or repo.merchant_context(self.merchant_id)
        r = self.store.cohort_profile(c.category, c.locality)
        if r["value"].get("available") is False:
            self._add(unavailable("get_cohort_profile", r["value"]["reason"], "graph"))
            return
        self._add(ok("get_cohort_profile", r, "graph"))

    def _t_get_demand_forecast(self) -> None:
        r = patterns.demand_forecast(self.merchant_id, int(self.params.get("days", 7)))
        if not r["value"].get("available"):
            self._add(unavailable("get_demand_forecast", r["value"]["reason"], "own_data"))
            return
        rush = patterns.rush_forecast(self.merchant_id)
        v = dict(r["value"])
        if rush["value"].get("available"):
            v["next_rush"] = {k: rush["value"][k]
                              for k in ("weekday", "hour", "multiple", "day")}
        self._add(Evidence(tool="get_demand_forecast", value=v,
                           basis={**r["basis"], "rush_basis": rush["basis"]},
                           source="own_data", tier="A"))

    def _t_get_money_position(self) -> None:
        amount = self.params.get("purchase_amount")
        r = (money.purchase_check(self.merchant_id, float(amount))
             if amount else money.cash_view(self.merchant_id,
                                            int(self.params.get("days", 7))))
        if not r["value"].get("available"):
            self._add(unavailable("get_money_position", r["value"].get("reason", "no history"),
                                  "finance"))
            return
        tier = "A" if r["value"].get("scope") == "inflow_only" else "B"
        self._add(ok("get_money_position", r, "finance", tier=tier))

    def _t_get_optional_stock_context(self) -> None:
        c = self.context or repo.merchant_context(self.merchant_id)
        subject = self.params.get("subject") or ASK_SUBJECT.get(c.category, "stock")

        # tier B: a connected feed
        if c.has_stock_feed:
            snap = repo.stock_snapshot(self.merchant_id, subject)
            if snap:
                self._add(Evidence(
                    tool="get_optional_stock_context",
                    value={"available": True, "subject": snap["item_label"],
                           "quantity": snap["quantity"], "unit": snap["unit"],
                           "as_of": snap["as_of"], "source": snap["source"]},
                    basis={"source": "repository.stock_snapshot",
                           "tier": "B", "note": "from a connected billing/POS feed"},
                    source="integration", tier="B"))
                return

        # tier C: something the merchant said, still inside its TTL
        said = ctx.get(self.merchant_id, "stock_estimate", subject)
        if said and said.get("value_num") is not None:
            self._add(Evidence(
                tool="get_optional_stock_context",
                value={"available": True, "subject": said.get("subject") or subject,
                       "quantity": said["value_num"], "unit": said.get("unit"),
                       "as_of": said["stated_at"], "source": "merchant_stated"},
                basis={"source": "data.context", "tier": "C",
                       "utterance": said["utterance"], "expires_at": said["expires_at"],
                       "note": "the merchant told us this; attributed back when used"},
                source="merchant_input", tier="C"))
            return

        # neither: ask, but only because the answer would change
        self._add(unavailable(
            "get_optional_stock_context",
            "no connected stock feed and nothing stated recently",
            "integration", tier="B", ask="stock_estimate", ask_subject=subject))

    def _t_get_action_history(self) -> None:
        r = outcome.action_history(self.merchant_id, int(self.params.get("limit", 3)))
        if not r["value"].get("available"):
            self._add(unavailable("get_action_history", r["value"]["reason"], "own_data"))
            return
        self._add(ok("get_action_history", r, "own_data"))

    def _t_propose_action(self) -> None:
        pb = self.value("get_peer_playbook")
        if not pb or not pb.get("best"):
            self._add(unavailable("propose_action",
                                  "no cohort-supported play above the confidence floor",
                                  "graph"))
            return
        best = pb["best"]
        params = dict(best.get("common_params") or {})
        proposal = {
            "type": best["action"],
            "params": {"discount_rs": int(params.get("discount_rs", 10)),
                       "window": params.get("window", "18-21"),
                       "days": int(params.get("days", 3))}
            if best["action"] == "evening_offer" else
            {k: (int(v) if isinstance(v, float) and v.is_integer() else v)
             for k, v in params.items()},
            "condition": pb.get("situation_kind", "sales_decline"),
            "evidence_summary": (f"{best['worked']} of {best['tried']} similar merchants "
                                 f"({best['success_rate']}%)"),
        }
        self._add(Evidence(
            tool="propose_action", value=proposal,
            basis={"source": "reasoning.propose_action",
                   "derived_from": "graph.peer_playbook",
                   "note": "parameters are the cohort's modal values, not a guess"},
            source="graph", tier="A"))

    # ===================================================== agent-era tools
    # These take their arguments explicitly, which is the whole point: the old
    # pipeline could not express "50,000 rupees, next month" at all, so it
    # answered a generic seven-day question instead.

    def _a_sales_lookup(self, args: dict) -> None:
        from backend.reasoning import tools as T
        start, end, label = T.resolve_period(args.get("period", "last_7_days"),
                                             args.get("start"), args.get("end"))
        t = T._totals(self.merchant_id, start, end)
        if not t["days_open"]:
            # Zero is a real answer, not a missing one. "nothing has come in
            # yet today" is exactly what the merchant asked and is true.
            self._add(Evidence(
                tool="sales_lookup",
                value={"available": True, "period": label, "total": 0.0, "txns": 0,
                       "avg_ticket": None, "per_day": 0.0, "days_open": 0,
                       "no_activity": True},
                basis={"source": "repository.window_totals",
                       "window": [start.isoformat(), end.isoformat()],
                       "note": "no payment rows in this window"},
                source="own_data", tier="A"))
            return
        self._add(Evidence(
            tool="sales_lookup",
            value={"available": True, "period": label, "total": t["total"],
                   "txns": t["txns"], "avg_ticket": t["avg_ticket"],
                   "per_day": t["per_day"], "days_open": t["days_open"]},
            basis={"source": "repository.window_totals",
                   "window": [start.isoformat(), end.isoformat()],
                   "note": "closed days excluded from the per-day mean"},
            source="own_data", tier="A"))

    def _a_compare_periods(self, args: dict) -> None:
        from backend.reasoning import tools as T
        s1, e1, l1 = T.resolve_period(args.get("period", "this_week"))
        s2, e2, l2 = T._shift(s1, e1, args.get("compare_to", "previous_period"))
        a, b = T._totals(self.merchant_id, s1, e1), T._totals(self.merchant_id, s2, e2)
        if not a["days_open"] or not b["days_open"]:
            self._add(unavailable("compare_periods",
                                  "one of the two periods has no trading days",
                                  "own_data"))
            return
        diff = a["per_day"] - b["per_day"]
        pct = diff / b["per_day"] * 100.0 if b["per_day"] else 0.0
        self._add(Evidence(
            tool="compare_periods",
            value={"available": True, "period": l1, "compared_to": l2,
                   "per_day": a["per_day"], "per_day_before": b["per_day"],
                   "total": a["total"], "total_before": b["total"],
                   "difference_per_day": round(diff, 0), "change_pct": round(pct, 1),
                   "direction": "up" if pct > 2 else ("down" if pct < -2 else "flat")},
            basis={"source": "repository.window_totals",
                   "windows": [[s1.isoformat(), e1.isoformat()],
                               [s2.isoformat(), e2.isoformat()]],
                   "method": "per-trading-day means, so unequal windows compare fairly"},
            source="own_data", tier="A"))

    def _a_afford_check(self, args: dict) -> None:
        amount = float(args.get("amount") or 0)
        days = int(args.get("days") or 30)
        if amount <= 0:
            self._add(unavailable("afford_check", "no amount given", "finance"))
            return
        r = money.purchase_check(self.merchant_id, amount, days)
        v = r["value"]
        if not v.get("available"):
            self._add(unavailable("afford_check", v.get("reason", "no history"), "finance"))
            return
        ob = money.obligation_projection(self.merchant_id, days)["value"]
        v = dict(v)
        v["days"] = days
        if ob.get("available"):
            v["bills_total"] = ob["total"]
            v["bills"] = [{"label": i["label"], "amount": i["amount"],
                           "due_on": i["due_on"]} for i in ob["items"]]
        tier = "A" if v.get("scope") == "inflow_only" else "B"
        self._add(Evidence(tool="afford_check", value=v,
                           basis={**r["basis"], "horizon_days": days},
                           source="finance", tier=tier))

    def _a_stock_cover(self, args: dict) -> None:
        subject = args.get("subject") or "stock"
        qty = args.get("quantity")
        rate = args.get("units_per_day")

        if qty is None:
            said = ctx.get(self.merchant_id, "stock_estimate", subject)
            if said and said.get("value_num") is not None:
                qty = said["value_num"]
            else:
                c = self.context or repo.merchant_context(self.merchant_id)
                snap = repo.stock_snapshot(self.merchant_id, subject) if c.has_stock_feed else None
                if snap:
                    qty = snap["quantity"]
        if rate is None:
            said = ctx.get(self.merchant_id, "daily_units", subject)
            if said and said.get("value_num") is not None:
                rate = said["value_num"]

        if qty is None:
            self._add(unavailable("stock_cover",
                                  f"no quantity known for {subject}", "merchant_input",
                                  tier="C", ask="stock_estimate", ask_subject=subject))
            return
        if not rate:
            # The honest gap. Payment data is denominated in rupees, so it can
            # never supply units per day; only the merchant can.
            self._add(unavailable(
                "stock_cover",
                f"stock of {subject} is known but not how many sell per day; "
                f"payment data is in rupees and cannot supply a unit rate",
                "merchant_input", tier="C", ask="daily_units", ask_subject=subject))
            return

        f = patterns.demand_forecast(self.merchant_id, 7)["value"]
        mult = 1.0
        if f.get("available") and f.get("per_day"):
            base = sum(d["expected"] for d in f["per_day"]) / len(f["per_day"])
            tomorrow = f["per_day"][0]["expected"]
            mult = round(tomorrow / base, 2) if base else 1.0
        effective = rate * mult
        cover = qty / effective if effective else 0.0
        today = db.today()
        need_7 = effective * 7
        self._add(Evidence(
            tool="stock_cover",
            value={"available": True, "subject": subject, "quantity": qty,
                   "units_per_day": rate, "demand_multiplier": mult,
                   "days_of_cover": round(cover, 1),
                   "runs_out_on": (today + timedelta(days=int(cover))).isoformat(),
                   "reorder_by": (today + timedelta(days=max(0, int(cover) - 1))).isoformat(),
                   "units_needed_7d": int(round(need_7)),
                   "shortfall_7d": max(0, int(round(need_7 - qty)))},
            basis={"source": "reasoning.stock_cover",
                   "quantity_from": "merchant_stated_or_pos",
                   "rate_from": "merchant_stated",
                   "demand_basis": "analytics.demand_forecast (rupee demand, used only "
                                   "to scale the merchant's own unit rate)"},
            source="merchant_input", tier="C"))

    def _a_calculate(self, args: dict) -> None:
        from backend.reasoning import tools as T
        expression = str(args.get("expression", ""))
        label = str(args.get("label", "calculation"))
        try:
            result = T.safe_eval(expression)
        except Exception as exc:                      # noqa: BLE001
            self._add(unavailable("calculate", f"invalid expression: {exc}", "own_data"))
            return
        self._add(Evidence(
            tool="calculate",
            value={"available": True, "label": label, "expression": expression,
                   "result": round(result, 2)},
            basis={"source": "reasoning.calculate",
                   "note": "arithmetic only; evaluated in Python, not by the model"},
            source="own_data", tier="A"))

    def _a_remember_fact(self, args: dict) -> None:
        kind = args.get("kind", "stock_estimate")
        subject = args.get("subject")
        value = args.get("value")
        row = ctx.store(self.merchant_id, kind, str(args.get("utterance", "")),
                        subject=subject, value_num=float(value) if value is not None else None,
                        unit=args.get("unit"))
        self._add(Evidence(
            tool="remember_fact",
            value={"available": True, "kind": kind, "subject": subject,
                   "value": row.get("value_num"), "unit": row.get("unit"),
                   "expires_at": row.get("expires_at")},
            basis={"source": "data.context.store", "tier": "C",
                   "utterance": row.get("utterance"),
                   "note": "the merchant told us this; it expires on its own and is "
                           "never aggregated into the graph"},
            source="merchant_input", tier="C"))

    # ------------------------------------------------------------- helpers
    def _situation_kind(self) -> str:
        t = self.value("get_sales_trend")
        if not t:
            h = self.value("get_business_health")
            t = {"change_pct": h["change_pct"], "worst_band": h.get("worst_band")} if h else None
        if not t:
            # no trend tool in this intent's set: compute it rather than guessing
            try:
                t = trend.sales_trend(self.merchant_id)["value"]
            except Exception:      # noqa: BLE001
                return "sales_decline"
        change = t.get("change_pct", 0.0)
        band = t.get("worst_band")
        if change <= -10 and band in ("16-19", "19-22"):
            return "evening_decline"
        if change <= -10:
            return "sales_decline"
        if change >= 12:
            return "demand_surge"
        return "sales_decline"


def pending_ask(evidence: list[Evidence]) -> Evidence | None:
    """At most one clarifying question per exchange."""
    for ev in evidence:
        if ev.ask:
            return ev
    return None
