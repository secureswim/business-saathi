"""The reasoning layer.

Takes a parsed intent and assembles an answer from four independent sources:

  1. the merchant's own transaction data      (FinancialEngine)
  2. merchants like them                      (KnowledgeGraph peer traversal)
  3. their financial position                 (FinancialEngine cashflow)
  4. area-wide patterns                       (KnowledgeGraph locality signal)

No other system combines all four. The fourth — peer-relative anomaly
detection — is the one that is impossible without the graph.

Output is a structured `Answer`, never raw prose: the UI renders the four
quadrants from it, the LLM adapter only phrases the spoken line, and the
workflow engine reads the proposed action off it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.adapters.graph import LocalGraph, _params_label, get_graph
from app.adapters.llm import get_llm
from app.core.financial import FinancialEngine
from app.core.intent import Intent, parse
from app.core.synth import CATEGORIES, LOCALITIES

SLOT_LABEL_HI = {"evening": "shaam 6-9 PM", "lunch": "lunch 12-3 PM", "morning": "subah 7-11 AM"}
SLOT_LABEL_EN = {"evening": "6-9 PM", "lunch": "12-3 PM", "morning": "7-11 AM"}

ACTION_LABEL = {
    "evening_offer": "an evening offer",
    "deep_discount": "a deep discount",
    "price_increase": "a price increase",
    "festive_prestock": "festive pre-stocking",
}


@dataclass
class Card:
    """One quadrant of the answer."""

    source: str          # own_data | merchants_like_you | financial_position | area_wide
    title: str
    body: str
    highlight: str | None = None
    tone: str = "neutral"  # neutral | positive | warning | critical
    trace: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    action_type: str
    params: dict[str, Any]
    label: str
    proposal_hi: str
    evidence_line: str
    evidence_line_hi: str
    expected_gmv_delta_pct: float
    confidence: float
    requires_approval: bool = True

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Answer:
    intent: str
    family: str
    spoken: str
    cards: list[Card]
    proposal: Proposal | None
    evidence: dict[str, Any] | None
    trace: list[str]
    charts: dict[str, Any] = field(default_factory=dict)
    simulation: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "family": self.family,
            "spoken": self.spoken,
            "cards": [c.__dict__ for c in self.cards],
            "proposal": self.proposal.as_dict() if self.proposal else None,
            "evidence": self.evidence,
            "trace": self.trace,
            "charts": self.charts,
            "simulation": self.simulation,
        }


class ReasoningLayer:
    def __init__(self, graph: LocalGraph | None = None, fin: FinancialEngine | None = None) -> None:
        self.graph = graph or get_graph()
        self.fin = fin or FinancialEngine(self.graph.ds)
        self.llm = get_llm()

    # ----------------------------------------------------------------- api
    def answer(self, merchant_id: str, text: str) -> Answer:
        intent = parse(text)
        return self.answer_intent(merchant_id, intent)

    def answer_intent(self, merchant_id: str, intent: Intent) -> Answer:
        bundle = self._bundle(merchant_id, intent)
        # the language layer sees only JSON-safe values, never live objects
        if bundle.get("_proposal"):
            bundle["recommendation"] = bundle["_proposal"].as_dict()
        cards = self._cards(merchant_id, intent, bundle)
        proposal = bundle.get("_proposal")
        spoken = self.llm.say(bundle)
        return Answer(
            intent=intent.name,
            family=intent.family,
            spoken=spoken,
            cards=cards,
            proposal=proposal,
            evidence=bundle.get("evidence"),
            trace=bundle.get("trace", []),
            charts=self._charts(merchant_id, intent),
            simulation=bundle.get("simulation"),
        )

    # ------------------------------------------------------------- bundle
    def _bundle(self, mid: str, intent: Intent) -> dict[str, Any]:
        g, f = self.graph, self.fin
        m = g.ds.merchant(mid)
        slot_name, slot_trend = f.worst_slot(mid)
        trend = f.trend(mid)
        loc = g.locality_health(mid)

        bundle: dict[str, Any] = {
            "intent": intent.name,
            "family": intent.family,
            "asked": intent.slots,
            "merchant": {"id": m.id, "name": m.name,
                         "category": CATEGORIES[m.category]["label"],
                         "locality": LOCALITIES[m.locality]["label"]},
            "own": {
                "trend": trend.as_dict(),
                "worst_slot": {**slot_trend.as_dict(), "slot": slot_name,
                               "label": SLOT_LABEL_HI[slot_name],
                               "label_en": SLOT_LABEL_EN[slot_name]},
                "avg_ticket": round(f.avg_ticket(mid), 1),
            },
            "locality": loc,
            "trace": [f"merchant:{mid}", f"category:{m.category}", f"locality:{m.locality}"],
        }

        # --- intent-specific enrichment ---------------------------------
        if intent.name in ("sales_drop", "business_health", "what_should_i_do"):
            ev = g.evidence(mid, "evening_offer" if slot_name == "evening" else "evening_offer")
            bundle["evidence"] = ev.as_dict() if ev else None
            if ev and ev.success_rate >= 0.5 and trend.delta_pct < -5:
                bundle["_proposal"] = self._offer_proposal(mid, ev, slot_name)
            bundle["cashflow"] = f.cashflow(mid).as_dict()
            bundle["trace"] += ev.path if ev else []

        elif intent.name == "area_or_me":
            bundle["evidence"] = None

        elif intent.name == "cash_forecast":
            spend = intent.slots.get("amount", 0.0)
            # a bare "next week" question is not a purchase question
            label = "inventory purchase" if spend >= 1000 else "planned purchase"
            cf = f.cashflow(mid, horizon_days=int(intent.slots.get("horizon_days", 14)),
                            planned_spend=spend if spend >= 1000 else 0.0, spend_label=label)
            bundle["cashflow"] = cf.as_dict()

        elif intent.name == "rush_forecast":
            bundle["rush"] = f.next_rush(mid)

        elif intent.name == "festive_prep":
            ev = g.evidence(mid, "festive_prestock")
            bundle["evidence"] = ev.as_dict() if ev else None
            bundle["trace"] += ev.path if ev else []

        elif intent.name == "discount_check":
            ev = g.evidence(mid, "deep_discount")
            bundle["evidence"] = ev.as_dict() if ev else None
            pct = intent.slots.get("percent", 30.0)
            if ev:
                el, cap = _elasticity(f, ev, "discount_pct")
                bundle["simulation"] = f.simulate(mid, "discount_pct", pct, el, cap)
                bundle["trace"] += ev.path
            # steer to the action that actually has evidence behind it
            alt = g.evidence(mid, "evening_offer")
            if alt and alt.success_rate > (ev.success_rate if ev else 0):
                bundle["_proposal"] = self._offer_proposal(mid, alt, slot_name)

        elif intent.name == "price_whatif":
            ev = g.evidence(mid, "price_increase")
            bundle["evidence"] = ev.as_dict() if ev else None
            rupees = intent.slots.get("amount", 10.0)
            if ev:
                el, cap = _elasticity(f, ev, "price_increase")
                bundle["simulation"] = f.simulate(mid, "price_increase", rupees, el, cap)
                bundle["trace"] += ev.path

        elif intent.name == "create_offer":
            ev = g.evidence(mid, "evening_offer")
            bundle["evidence"] = ev.as_dict() if ev else None
            if ev:
                bundle["_proposal"] = self._offer_proposal(mid, ev, slot_name)
                bundle["trace"] += ev.path

        return bundle

    # ------------------------------------------------------------ proposal
    def _offer_proposal(self, mid: str, ev, slot_name: str) -> Proposal:
        # Peers' discounts transfer as a PERCENTAGE of their own bill, never as a
        # rupee figure. Copying "Rs 19 off" from a peer selling Rs 100 plates onto
        # a Rs 50 plate is a 38% discount nobody in the graph ever tried.
        pcts = [float(s["params"].get("discount_pct", 0) or 0)
                for s in ev.sample if s["outcome"] == "recovered"]
        pcts = [p for p in pcts if p] or [15.0]
        pcts.sort()
        med_pct = pcts[len(pcts) // 2]
        ticket = self.fin.avg_ticket(mid)
        rupees = max(1, round(ticket * med_pct / 100))
        window = ev.params.get("window", "18:00-21:00")
        days = int(ev.params.get("days", 3))
        ev_en = (f"{ev.success_count} of {ev.peer_count} {ev.cohort} who hit this pattern "
                 f"recovered after running it ({ev.success_rate * 100:.0f}%)")
        ev_hi = (f"{ev.peer_count} merchants mein se {ev.success_count} ne ye chalaya "
                 f"aur recover kiya")
        return Proposal(
            action_type="evening_offer",
            params={"discount_rupees": rupees, "discount_pct": round(med_pct, 1),
                    "window": window, "days": days},
            label=f"a ₹{rupees}-off offer, {window}, for {days} days",
            proposal_hi=f"₹{rupees} off, {window}, {days} din ke liye.",
            evidence_line=ev_en,
            evidence_line_hi=ev_hi,
            expected_gmv_delta_pct=ev.median_gmv_delta_pct,
            confidence=round(min(0.95, ev.success_rate * (0.7 + 0.05 * ev.peer_count)), 2),
        )

    # --------------------------------------------------------------- cards
    def _cards(self, mid: str, intent: Intent, b: dict) -> list[Card]:
        own, loc = b["own"], b["locality"]
        ws = own["worst_slot"]
        d = own["trend"]["delta_pct"]

        c1 = Card(
            source="own_data",
            title="From your own data",
            body=(f"Sales {abs(d):.0f}% {'down' if d < 0 else 'up'} this week. "
                  f"The change is concentrated in {ws['label_en']}. "
                  f"Your average daily is ₹{own['trend']['recent_avg']:,.0f}, "
                  f"{'down' if d < 0 else 'up'} from ₹{own['trend']['baseline_avg']:,.0f} last month."),
            highlight=f"{d:+.0f}%",
            tone="critical" if d <= -15 else "warning" if d < -5 else "positive",
            trace=[f"90 days of transaction rollups · {ws['label_en']} slot isolated",
                   "weekday seasonality divided out before comparison"],
        )

        ev = b.get("evidence")
        if ev:
            c2 = Card(
                source="merchants_like_you",
                title="From merchants like you",
                body=(f"{ev['success_count']} out of {ev['peer_count']} {ev['cohort']} who faced this "
                      f"pattern recovered after {ACTION_LABEL.get(ev['action_type'], ev['action_type'])}"
                      f" ({_params_label(ev['params'])}). "
                      f"Median outcome {ev['median_gmv_delta_pct']:+.0f}% GMV"
                      + (f", retention {ev['median_retention_pct']:.0f}% after 30 days."
                         if ev['median_retention_pct'] is not None else ".")),
                highlight=f"{ev['success_rate'] * 100:.0f}% success rate",
                tone="positive" if ev["success_rate"] >= 0.6 else "warning",
                trace=[" → ".join(ev["path"])] + [
                    f"peer {s['peer']}: {s['outcome']} ({s['gmv_delta_pct']:+.0f}%) — {s['note']}"
                    for s in ev["sample"][:4]
                ],
            )
        else:
            c2 = Card(
                source="merchants_like_you",
                title="From merchants like you",
                body="No similar merchant in the graph has tried this yet, so there is no peer "
                     "evidence to stand on. I will not guess.",
                tone="neutral",
                trace=["peer traversal returned an empty action set"],
            )

        cf = b.get("cashflow") or self.fin.cashflow(mid).as_dict()
        planned = next((l for l in cf["expense_lines"]
                        if l["label"] in ("inventory purchase", "planned purchase")), None)
        body = (f"You have ₹{cf['upcoming_expenses']:,.0f} in expenses due within "
                f"{cf['horizon_days']} days against ₹{cf['projected_inflow']:,.0f} of projected "
                f"retained margin.")
        if planned:
            body += (f" The ₹{planned['amount']:,.0f} {planned['label']} is possible but leaves "
                     f"your buffer at ₹{cf['closing_buffer']:,.0f}.")
        else:
            body += f" Closing buffer ₹{cf['closing_buffer']:,.0f}."
        c3 = Card(
            source="financial_position",
            title="From your financial position",
            body=body,
            highlight=f"₹{cf['closing_buffer']:,.0f} buffer",
            tone={"tight": "critical", "manageable": "warning", "comfortable": "positive"}[cf["verdict"]],
            trace=[f"{l['label']} ₹{l['amount']:,.0f} due in {l['due_in_days']}d" for l in cf["expense_lines"]]
                  + ["projection uses retained category margin, not GMV"],
        )

        verdict_body = {
            "merchant_specific": (f"This decline is specific to your shop. {loc['peer_sample']} similar "
                                  f"merchants in {loc['locality']} are at {loc['area_trend_pct']:+.0f}%. "
                                  f"Something changed for you — not the market."),
            "area_wide": (f"The whole area is moving with you: your {loc['merchant_trend_pct']:+.0f}% "
                          f"against {loc['area_trend_pct']:+.0f}% across {loc['peer_sample']} similar "
                          f"merchants in {loc['locality']}."),
            "outperforming_area": (f"You are ahead of your area: {loc['merchant_trend_pct']:+.0f}% "
                                   f"against {loc['area_trend_pct']:+.0f}% across {loc['peer_sample']} "
                                   f"similar merchants."),
        }[loc["verdict"]]
        c4 = Card(
            source="area_wide",
            title="From area-wide patterns",
            body=verdict_body,
            highlight={"merchant_specific": "yours, not the market",
                       "area_wide": "area-wide",
                       "outperforming_area": "ahead of area"}[loc["verdict"]],
            tone="warning" if loc["verdict"] == "merchant_specific" else "neutral",
            trace=[f"peer-relative anomaly detection across {loc['peer_sample']} same-category, "
                   f"same-locality merchants — not possible without the graph"],
        )
        return [c1, c2, c3, c4]

    # -------------------------------------------------------------- charts
    def _charts(self, mid: str, intent: Intent) -> dict[str, Any]:
        return {
            "daily": self.fin.daily_series(mid, days=45),
            "hourly": self.fin.hourly_profile(mid),
            "hourly_baseline": self._hourly_baseline(mid),
        }

    def _hourly_baseline(self, mid: str) -> dict[int, float]:
        rs = sorted(self.fin.ds.rolls_for(mid), key=lambda r: r.day)[-40:-10]
        prof: dict[int, float] = {}
        for r in rs:
            for h, v in r.by_hour.items():
                prof[h] = prof.get(h, 0.0) + v
        n = max(len(rs), 1)
        return {h: round(v / n, 2) for h, v in sorted(prof.items())}


# --------------------------------------------------------------------------


def _elasticity(fin: FinancialEngine, ev, kind: str) -> tuple[float, float | None]:
    """Median per-percent transaction response, computed per peer.

    Each peer's price shock is measured against *that peer's own* ticket, so the
    elasticity we transfer is scale-free. Also returns the largest shock any peer
    actually tried, which is where honest projection stops.
    """
    per_pct: list[float] = []
    shocks: list[float] = []
    for s in ev.sample:
        p = s.get("params", {})
        if kind == "price_increase":
            shock = p.get("increase_pct")
            if shock is None:
                inc = float(p.get("increase_rupees", 0) or 0)
                ticket = fin.avg_ticket(s["peer"])
                shock = inc / ticket * 100 if ticket else None
        else:
            shock = -float(p.get("discount_pct", 0) or 0)
        if not shock:
            continue
        txn = float(s.get("txn_delta_pct", 0) or 0)
        per_pct.append(txn / shock)
        shocks.append(abs(shock))
    if not per_pct:
        return -0.5, None
    per_pct.sort()
    n = len(per_pct)
    med = per_pct[n // 2] if n % 2 else (per_pct[n // 2 - 1] + per_pct[n // 2]) / 2
    return med, max(shocks)
