"""The sixteen tools, and the two-wave concurrent runner.

The LLM calls these; it never touches SQL and never does arithmetic. Every tool
returns an Evidence envelope. `available: False` is a first-class result, not an
error -- the synthesis layer omits that dimension rather than hedging about it,
and /ops renders a greyed card so "why didn't it mention cash?" has a visible
answer.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.analytics import anomaly, money, outcome, patterns, trend  # noqa: E402
from backend.data import context as ctx, repository as repo  # noqa: E402
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
        fn = getattr(self, f"_t_{tool}", None)
        if fn is None:
            return
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            self._add(unavailable(tool, f"tool error: {type(exc).__name__}", "own_data"))

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
        self._add(ok("get_business_health", trend.business_health(self.merchant_id),
                     "own_data"))

    def _t_get_sales_trend(self) -> None:
        if repo.days_of_history(self.merchant_id) < 14:
            self._add(unavailable("get_sales_trend", "under 14 days of history",
                                  "own_data"))
            return
        self._add(ok("get_sales_trend", trend.sales_trend(self.merchant_id), "own_data"))

    def _t_get_time_patterns(self) -> None:
        r = patterns.time_patterns(self.merchant_id)
        if not r["value"].get("available"):
            self._add(unavailable("get_time_patterns", r["value"]["reason"], "own_data"))
            return
        self._add(ok("get_time_patterns", r, "own_data"))

    def _t_get_recent_situations(self) -> None:
        rows = repo.recent_situations(self.merchant_id, days=30)
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
        r = anomaly.peer_relative_anomaly(self.merchant_id, peer_ids)
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
