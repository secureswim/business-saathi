"""Financial engine: own-data analytics, cashflow projection, anomaly detection.

Everything here is computed from the merchant's own transaction rollups. The
graph supplies peer context; this module supplies the merchant's own truth.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date as _date
from typing import Any

from app.core.synth import Dataset, dataset

EVENING = range(18, 21)   # 6-9 PM
LUNCH = range(12, 15)
MORNING = range(7, 11)
SLOTS = {"evening": EVENING, "lunch": LUNCH, "morning": MORNING}

# Retained margin by category — GMV is not cash in hand.
MARGIN = {
    "chai_stall": 0.30,
    "food_stall": 0.28,
    "kirana": 0.14,
    "salon": 0.55,
    "mobile_repair": 0.35,
}


@dataclass
class Trend:
    recent_avg: float
    baseline_avg: float
    delta_pct: float
    window_days: int
    baseline_days: int

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: round(v, 1) if isinstance(v, float) else v for k, v in d.items()}


@dataclass
class Cashflow:
    projected_inflow: float
    upcoming_expenses: float
    expense_lines: list[dict[str, Any]]
    horizon_days: int
    opening_buffer: float
    closing_buffer: float
    daily_run_rate: float
    verdict: str

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("projected_inflow", "upcoming_expenses", "opening_buffer",
                  "closing_buffer", "daily_run_rate"):
            d[k] = round(d[k])
        return d


class FinancialEngine:
    def __init__(self, ds: Dataset | None = None) -> None:
        self.ds = ds or dataset()

    # ---- helpers -------------------------------------------------------
    def _rolls(self, mid: str) -> list:
        return sorted(self.ds.rolls_for(mid), key=lambda r: r.day)

    def weekday_factors(self, mid: str) -> dict[int, float]:
        """Each merchant has its own weekly rhythm. A short window that happens to
        land on slow weekdays is not a decline, so we divide it out before
        comparing — otherwise every Monday looks like an emergency."""
        rs = self._rolls(mid)
        totals: dict[int, list[float]] = {}
        for r in rs:
            wd = _date.fromisoformat(r.day).weekday()
            totals.setdefault(wd, []).append(r.gmv)
        overall = sum(r.gmv for r in rs) / max(len(rs), 1)
        return {wd: (sum(v) / len(v) / overall if overall else 1.0) for wd, v in totals.items()}

    def _adj(self, mid: str, rows: list, hours=None) -> float:
        """Deseasonalised mean GMV (optionally restricted to an hour slot)."""
        if not rows:
            return 0.0
        f = self.weekday_factors(mid)
        tot = 0.0
        for r in rows:
            val = r.gmv if hours is None else sum(v for h, v in r.by_hour.items() if h in hours)
            tot += val / max(f.get(_date.fromisoformat(r.day).weekday(), 1.0), 0.2)
        return tot / len(rows)

    # ---- trends --------------------------------------------------------
    def trend(self, mid: str, window: int = 7, baseline: int = 30) -> Trend:
        rs = self._rolls(mid)
        recent, base = rs[-window:], rs[-(window + baseline):-window]
        a, b = self._adj(mid, recent), self._adj(mid, base)
        return Trend(a, b, ((a - b) / b * 100) if b else 0.0, window, len(base))

    def slot_trends(self, mid: str, window: int = 7, baseline: int = 30) -> dict[str, Trend]:
        rs = self._rolls(mid)
        recent, base = rs[-window:], rs[-(window + baseline):-window]
        out: dict[str, Trend] = {}
        for name, hours in SLOTS.items():
            a, b = self._adj(mid, recent, hours), self._adj(mid, base, hours)
            out[name] = Trend(a, b, ((a - b) / b * 100) if b else 0.0, window, len(base))
        return out

    def worst_slot(self, mid: str) -> tuple[str, Trend]:
        st = self.slot_trends(mid)
        # only count slots that actually carry meaningful volume for this merchant
        material = {k: v for k, v in st.items() if v.baseline_avg > 0.10 * sum(x.baseline_avg for x in st.values())}
        target = material or st
        return min(target.items(), key=lambda kv: kv[1].delta_pct)

    def hourly_profile(self, mid: str, days: int = 30) -> dict[int, float]:
        rs = self._rolls(mid)[-days:]
        prof: dict[int, float] = {}
        for r in rs:
            for h, v in r.by_hour.items():
                prof[h] = prof.get(h, 0.0) + v
        n = max(len(rs), 1)
        return {h: round(v / n, 2) for h, v in sorted(prof.items())}

    def daily_series(self, mid: str, days: int = 45) -> list[dict[str, Any]]:
        return [{"day": r.day, "gmv": round(r.gmv), "txns": r.txns} for r in self._rolls(mid)[-days:]]

    def next_rush(self, mid: str) -> dict[str, Any]:
        """Demand forecast: the hour that most reliably spikes, and by how much."""
        prof = self.hourly_profile(mid)
        if not prof:
            return {}
        mean = sum(prof.values()) / len(prof)
        hour, val = max(prof.items(), key=lambda kv: kv[1])
        rs = self._rolls(mid)[-28:]
        by_weekday: dict[int, float] = {}
        from datetime import date as _d
        for r in rs:
            wd = _d.fromisoformat(r.day).weekday()
            by_weekday[wd] = by_weekday.get(wd, 0.0) + r.gmv
        best_wd = max(by_weekday.items(), key=lambda kv: kv[1])[0] if by_weekday else 0
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        avg_ticket = self.avg_ticket(mid)
        return {
            "hour": hour,
            "window": f"{hour}:00-{hour + 2}:00",
            "multiple_of_average": round(val / mean, 2) if mean else 0,
            "strongest_day": names[best_wd],
            "expected_txns_in_window": round(val / max(avg_ticket, 1)),
            "avg_ticket": round(avg_ticket),
        }

    def avg_ticket(self, mid: str) -> float:
        rs = self._rolls(mid)[-30:]
        g = sum(r.gmv for r in rs)
        t = sum(r.txns for r in rs)
        return g / t if t else 0.0

    # ---- cashflow ------------------------------------------------------
    def cashflow(self, mid: str, horizon_days: int = 14, planned_spend: float = 0.0,
                 spend_label: str = "planned purchase") -> Cashflow:
        """Projection on the merchant's *retained cash*, not their GMV.

        GMV is not money in hand — stock and inputs eat most of it. We work in
        category margin so a "you can afford it" answer is actually true.
        """
        tr = self.trend(mid)
        run_rate = tr.recent_avg           # recent, weaker rate: deliberately conservative
        margin = MARGIN[self.ds.merchant(mid).category]
        daily_margin = run_rate * margin

        lines = [
            {"label": e.label, "amount": e.amount, "due_in_days": e.due_in_days}
            for e in self.ds.expenses_for(mid) if e.due_in_days <= horizon_days
        ]
        expenses = sum(l["amount"] for l in lines)
        if planned_spend:
            lines = lines + [{"label": spend_label, "amount": planned_spend, "due_in_days": 1}]
            expenses += planned_spend

        inflow = daily_margin * horizon_days
        opening = daily_margin * 30        # cash on hand ~ a month of retained margin
        closing = opening + inflow - expenses

        if closing < daily_margin * 7:
            verdict = "tight"
        elif closing < daily_margin * 20:
            verdict = "manageable"
        else:
            verdict = "comfortable"
        return Cashflow(inflow, expenses, lines, horizon_days, opening, closing, run_rate, verdict)

    # ---- anomaly detection (own baseline) ------------------------------
    def anomalies(self, mid: str, threshold_pct: float = -12.0) -> list[dict[str, Any]]:
        """What the monitoring workflow scans for, before the merchant asks."""
        found: list[dict[str, Any]] = []
        overall = self.trend(mid, window=3, baseline=30)
        if overall.delta_pct <= threshold_pct:
            found.append({"kind": "overall_decline", "delta_pct": round(overall.delta_pct, 1),
                          "window_days": 3, "severity": _severity(overall.delta_pct)})
        slots = self.slot_trends(mid, window=3, baseline=30)
        day_base = max(sum(s.baseline_avg for s in slots.values()), 1.0)
        for name, t in slots.items():
            # only alert on slots that are material to this merchant's day —
            # a 20% dip in a slot worth 4% of takings is not news
            if t.baseline_avg / day_base < 0.15:
                continue
            if t.baseline_avg > 200 and t.delta_pct <= threshold_pct:
                found.append({"kind": f"{name}_decline", "slot": name,
                              "delta_pct": round(t.delta_pct, 1), "window_days": 3,
                              "severity": _severity(t.delta_pct)})
        cf = self.cashflow(mid)
        if cf.verdict == "tight":
            found.append({"kind": "cash_buffer_tight", "closing_buffer": round(cf.closing_buffer),
                          "severity": "warning"})
        return sorted(found, key=lambda a: a.get("delta_pct", 0))

    # ---- what-if -------------------------------------------------------
    def simulate(self, mid: str, kind: str, magnitude: float, elasticity: float,
                 max_observed_shock_pct: float | None = None) -> dict[str, Any]:
        """Project a change onto this merchant's own volumes using peer elasticity.

        `elasticity` is percent transaction change per percent price change,
        derived per-peer against that peer's own ticket size. Peer response is
        only transferable in those terms: +Rs 10 is a rounding error on a Rs 400
        salon bill and a 60% hike on a Rs 16 cup of chai.

        Beyond the largest shock any peer actually tried, we stop interpolating
        and charge near-unit elasticity for the excess, and say so. Extrapolating
        a gentle elasticity out to a 60% price rise is how a model produces a
        confident, absurd answer.
        """
        rs = self._rolls(mid)[-30:]
        txns = sum(r.txns for r in rs) / max(len(rs), 1)
        ticket = self.avg_ticket(mid)
        base_gmv = txns * ticket

        if kind == "price_increase":
            new_ticket, shock = ticket + magnitude, magnitude / max(ticket, 1) * 100
        elif kind == "discount_rupees":
            new_ticket, shock = max(ticket - magnitude, 1), -magnitude / max(ticket, 1) * 100
        elif kind == "discount_pct":
            new_ticket, shock = ticket * (1 - magnitude / 100), -magnitude
        else:
            new_ticket, shock = ticket, 0.0

        cap = abs(max_observed_shock_pct) if max_observed_shock_pct else None
        inside = shock if cap is None else max(-cap, min(cap, shock))
        excess = shock - inside
        txn_delta = elasticity * inside
        extrapolated = abs(excess) > 0.5
        if extrapolated:
            # outside the observed band, charge roughly unit elasticity on the
            # excess rather than pretending the gentle peer response holds
            txn_delta -= 0.9 * abs(excess)
        new_txns = max(txns * (1 + txn_delta / 100), 0.0)

        new_gmv = new_ticket * new_txns
        return {
            "baseline_daily_gmv": round(base_gmv),
            "projected_daily_gmv": round(new_gmv),
            "delta_pct": round((new_gmv - base_gmv) / base_gmv * 100, 1) if base_gmv else 0,
            "baseline_txns": round(txns),
            "projected_txns": round(new_txns),
            "baseline_ticket": round(ticket, 1),
            "projected_ticket": round(new_ticket, 1),
            "price_shock_pct": round(shock, 1),
            "max_observed_shock_pct": round(cap, 1) if cap else None,
            "elasticity": round(elasticity, 3),
            "txn_response_pct": round(txn_delta, 1),
            "beyond_observed_range": extrapolated,
            "elasticity_source": "median per-percent transaction response among peers who tried this",
        }


def _severity(delta: float) -> str:
    if delta <= -25:
        return "critical"
    if delta <= -15:
        return "high"
    return "warning"
