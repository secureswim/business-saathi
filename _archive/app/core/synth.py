"""Synthetic Paytm merchant dataset.

The data is simulated; the intelligence pipeline that reads it is real. Every
number the demo shows is computed from these rows at query time, never
hardcoded in the UI.

Seeded deterministically so the demo reproduces exactly, run after run.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

SEED = 505  # Team 505
TODAY = date(2026, 9, 15)
DAYS = 90

# --------------------------------------------------------------------------
# taxonomy
# --------------------------------------------------------------------------

CATEGORIES = {
    "chai_stall": dict(label="Chai Stall", ticket=(15, 45), daily_txns=(220, 380)),
    "food_stall": dict(label="Food Stall", ticket=(40, 160), daily_txns=(70, 150)),
    "kirana": dict(label="Kirana Store", ticket=(80, 600), daily_txns=(25, 60)),
    "salon": dict(label="Salon", ticket=(150, 700), daily_txns=(12, 35)),
    "mobile_repair": dict(label="Mobile Repair", ticket=(200, 1800), daily_txns=(6, 20)),
}

LOCALITIES = {
    "sector-62-noida": dict(label="Sector 62, Noida", kind="office_college"),
    "lajpat-nagar": dict(label="Lajpat Nagar, Delhi", kind="market"),
    "karol-bagh": dict(label="Karol Bagh, Delhi", kind="market"),
    "sector-18-noida": dict(label="Sector 18, Noida", kind="mall_office"),
}

# hour-of-day weight curves -> "customer pattern"
PATTERNS = {
    "evening_heavy": {**{h: 0.2 for h in range(7, 24)}, 18: 2.6, 19: 3.0, 20: 2.4, 13: 1.4, 14: 1.0},
    "lunch_heavy": {**{h: 0.2 for h in range(7, 24)}, 12: 2.8, 13: 3.0, 14: 1.8, 19: 1.4},
    "morning_heavy": {**{h: 0.2 for h in range(7, 24)}, 8: 2.6, 9: 2.8, 10: 1.8, 18: 1.2},
    "steady": {h: 1.0 for h in range(9, 22)},
}

VOLUME_BANDS = [("micro", 0, 1500), ("small", 1500, 4000), ("mid", 4000, 9000), ("large", 9000, 10**9)]


def volume_band(avg_daily: float) -> str:
    for name, lo, hi in VOLUME_BANDS:
        if lo <= avg_daily < hi:
            return name
    return "large"


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass
class Merchant:
    id: str
    name: str
    category: str
    locality: str
    pattern: str
    opened_days_ago: int
    base_txns: int
    ticket: tuple[int, int]
    ticket_mean: float = 0.0   # this merchant's own stable average bill

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ticket"] = list(self.ticket)
        return d


@dataclass
class DayRoll:
    """Per-merchant per-day rollup, with hourly buckets."""

    merchant_id: str
    day: str
    txns: int
    gmv: float
    by_hour: dict[int, float]


@dataclass
class ActionRecord:
    """A thing a merchant actually did, and what measurably happened next.

    These are the action->outcome chains the knowledge graph traverses. They are
    what makes a recommendation evidence-backed rather than a guess.
    """

    id: str
    merchant_id: str
    action_type: str
    params: dict[str, Any]
    started_days_ago: int
    duration_days: int
    outcome: str  # recovered | no_change | spike_then_fade | declined
    gmv_delta_pct: float
    txn_delta_pct: float
    retention_after_30d_pct: float | None = None
    note: str = ""


@dataclass
class Expense:
    merchant_id: str
    label: str
    amount: float
    due_in_days: int
    recurring: bool = False


@dataclass
class Dataset:
    merchants: list[Merchant] = field(default_factory=list)
    rolls: list[DayRoll] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)
    expenses: list[Expense] = field(default_factory=list)

    # ---- lookups -------------------------------------------------------
    def merchant(self, mid: str) -> Merchant:
        return next(m for m in self.merchants if m.id == mid)

    def rolls_for(self, mid: str) -> list[DayRoll]:
        return [r for r in self.rolls if r.merchant_id == mid]

    def actions_for(self, mid: str) -> list[ActionRecord]:
        return [a for a in self.actions if a.merchant_id == mid]

    def expenses_for(self, mid: str) -> list[Expense]:
        return [e for e in self.expenses if e.merchant_id == mid]

    def avg_daily_gmv(self, mid: str, window: int = 30) -> float:
        rs = sorted(self.rolls_for(mid), key=lambda r: r.day)[-window:]
        return sum(r.gmv for r in rs) / max(len(rs), 1)


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

_NAMES = [
    "Sharma Chai Corner", "Gupta Tea Stall", "Balaji Chai Point", "Anand Tea House",
    "Shree Momos", "Delhi Chaat Bhandar", "Punjabi Rolls", "Tandoori Junction",
    "Hot Chowmein Center", "Sai Snacks", "Krishna Kirana", "Verma General Store",
    "Nehru Provision Store", "Jai Mata Di Store", "Apna Kirana Mart",
    "Style Studio Salon", "Glow Unisex Salon", "New Look Hair Care",
    "Mobile Care Point", "QuickFix Mobiles", "Gadget Clinic",
    "Chai Sutta Adda", "Tea Break", "Amul Chai Stall", "Bombay Vada Pav",
    "Sector Snacks", "Rajdhani Kirana", "Ganesh Store", "Cut & Style",
    "Screen Repair Hub",
]


def _hour_curve(pattern: str) -> dict[int, float]:
    return dict(PATTERNS[pattern])


def _seasonal(d: date) -> float:
    """Weekly rhythm + a mild festive ramp as Diwali approaches."""
    w = {0: 0.92, 1: 0.95, 2: 0.98, 3: 1.0, 4: 1.12, 5: 1.22, 6: 1.08}[d.weekday()]
    # Diwali window in this synthetic year: ~5 Nov 2026; ramp only if inside range
    diwali = date(2026, 11, 5)
    delta = (diwali - d).days
    festive = 1.0
    if 0 <= delta <= 21:
        festive = 1.0 + 0.35 * (1 - delta / 21)
    return w * festive


def build(seed: int = SEED) -> Dataset:
    rng = random.Random(seed)
    ds = Dataset()

    # ---- merchants ----
    plan: list[tuple[str, str, str]] = []
    # deliberately dense in food/chai + Sector 62 so peer evidence is meaningful.
    # The demo's hero merchant is M-001, so the first entry is its profile.
    plan += [("food_stall", "sector-62-noida", "evening_heavy") for _ in range(5)]
    plan += [("chai_stall", "sector-62-noida", "evening_heavy") for _ in range(4)]
    plan += [("chai_stall", "sector-62-noida", "lunch_heavy") for _ in range(2)]
    plan += [("food_stall", "sector-62-noida", "lunch_heavy") for _ in range(3)]
    plan += [("kirana", "sector-62-noida", "steady") for _ in range(2)]
    plan += [("chai_stall", "lajpat-nagar", "evening_heavy") for _ in range(2)]
    plan += [("food_stall", "lajpat-nagar", "evening_heavy") for _ in range(3)]
    plan += [("kirana", "lajpat-nagar", "steady") for _ in range(2)]
    plan += [("salon", "lajpat-nagar", "steady") for _ in range(2)]
    plan += [("food_stall", "karol-bagh", "lunch_heavy") for _ in range(2)]
    plan += [("kirana", "karol-bagh", "morning_heavy") for _ in range(2)]
    plan += [("mobile_repair", "karol-bagh", "steady") for _ in range(2)]
    plan += [("chai_stall", "sector-18-noida", "morning_heavy") for _ in range(1)]
    plan += [("food_stall", "sector-18-noida", "evening_heavy") for _ in range(1)]
    plan += [("salon", "sector-18-noida", "steady") for _ in range(1)]
    plan += [("mobile_repair", "sector-18-noida", "steady") for _ in range(1)]

    names = _NAMES[:]
    rng.shuffle(names)
    for i, (cat, loc, pat) in enumerate(plan, start=1):
        spec = CATEGORIES[cat]
        lo, hi = spec["daily_txns"]
        ds.merchants.append(
            Merchant(
                id=f"M-{i:03d}",
                name=names[i % len(names)] if i < len(names) else f"Merchant {i}",
                category=cat,
                locality=loc,
                pattern=pat,
                opened_days_ago=rng.choice([120, 200, 340, 500, 800, 95]),
                base_txns=rng.randint(lo, hi),
                ticket=tuple(spec["ticket"]),  # type: ignore[arg-type]
                # each merchant has a stable average bill; day-to-day variation
                # belongs on volume, not on a re-rolled ticket size
                ticket_mean=round(rng.uniform(*spec["ticket"]) * 0.62, 1),
            )
        )

    # the hero of the demo: food stall, Sector 62, evening-heavy
    hero = ds.merchants[0]
    hero.name = "Shree Momos & Chaat"
    hero.ticket_mean = 55.0
    hero.base_txns = 96

    # ---- which merchants ran which experiment (seeds the evidence base) ----
    # evening offer, food/chai, office-college locality -> 6 merchants, 5 recovered
    evening_pool = [
        m for m in ds.merchants
        if m.category in ("chai_stall", "food_stall")
        and LOCALITIES[m.locality]["kind"] == "office_college"
        and m.pattern == "evening_heavy"
        and m.id != hero.id
    ]
    evening_trials = evening_pool[:6]

    # steep discount (>=25%) -> spike then fade in 3 of 4
    discount_pool = [m for m in ds.merchants if m.id not in {x.id for x in evening_trials}
                     and m.category in ("food_stall", "kirana") and m.id != hero.id]
    discount_trials = discount_pool[:4]

    # price increase Rs 5-15 -> 4 merchants, mixed
    price_pool = [m for m in ds.merchants if m.category in ("chai_stall", "food_stall") and m.id not in
                  {x.id for x in evening_trials + discount_trials} and m.id != hero.id]
    price_trials = price_pool[:4]

    # festive pre-stock last Diwali
    # The hero has run no experiments at all — that is the whole point of the
    # story: he has no analyst, no consultant, no peer network, so he guesses.
    festive_trials = [m for m in ds.merchants
                      if m.category in ("chai_stall", "food_stall", "kirana") and m.id != hero.id][:8]

    aid = 0

    def add_action(m: Merchant, action_type: str, params: dict, started: int, dur: int,
                   outcome: str, gmv_d: float, txn_d: float, ret: float | None = None, note: str = "") -> None:
        nonlocal aid
        aid += 1
        ds.actions.append(ActionRecord(
            id=f"A-{aid:03d}", merchant_id=m.id, action_type=action_type, params=params,
            started_days_ago=started, duration_days=dur, outcome=outcome,
            gmv_delta_pct=round(gmv_d, 1), txn_delta_pct=round(txn_d, 1),
            retention_after_30d_pct=ret, note=note,
        ))

    for idx, m in enumerate(evening_trials):
        recovered = idx < 5  # 5 of 6
        # discount sized against the merchant's own ticket, not a flat rupee
        # figure — a Rs 10 offer is 18% off a plate of momos and 60% off a chai
        exp_ticket = m.ticket_mean or sum(m.ticket) / 2 * 0.62
        off = max(1, round(exp_ticket * rng.uniform(0.14, 0.22)))
        add_action(
            m, "evening_offer",
            {"discount_rupees": off, "discount_pct": round(off / exp_ticket * 100, 1),
             "window": "18:00-21:00", "days": 3},
            started=rng.randint(20, 70), dur=3,
            outcome="recovered" if recovered else "no_change",
            gmv_d=rng.uniform(14, 27) if recovered else rng.uniform(-2, 2),
            txn_d=rng.uniform(16, 30) if recovered else rng.uniform(-3, 2),
            ret=rng.uniform(62, 78) if recovered else 8.0,
            note="evening decline recovered within the offer window" if recovered
            else "no measurable change; footfall itself had dropped",
        )

    for idx, m in enumerate(discount_trials):
        faded = idx < 3  # 3 of 4
        add_action(
            m, "deep_discount", {"discount_pct": rng.choice([25, 30, 35]), "days": 7},
            started=rng.randint(25, 80), dur=7,
            outcome="spike_then_fade" if faded else "recovered",
            gmv_d=rng.uniform(22, 41) if faded else rng.uniform(12, 18),
            txn_d=rng.uniform(30, 55) if faded else rng.uniform(14, 20),
            ret=rng.uniform(4, 14) if faded else rng.uniform(55, 65),
            note="volume spiked during the offer, retention collapsed after" if faded
            else "held some of the gain after the offer ended",
        )

    # Price increases are generated RELATIVE to each merchant's own ticket, and
    # the volume response follows from a real elasticity. A flat "Rs 10 more" is
    # a rounding error on a salon bill and a 60% hike on a cup of chai, so a
    # dataset with flat rupee increases teaches the graph a false elasticity.
    for m in price_trials:
        exp_ticket = m.ticket_mean or sum(m.ticket) / 2 * 0.62
        inc = max(1, round(exp_ticket * rng.uniform(0.06, 0.30)))
        shock = inc / exp_ticket * 100
        # customers differ: some localities absorb a price rise, some walk away
        elasticity = rng.uniform(-1.15, -0.28)
        txn_d = elasticity * shock
        gmv_d = ((1 + shock / 100) * (1 + txn_d / 100) - 1) * 100
        if txn_d <= -10:
            out = "declined"
        elif abs(txn_d) < 4:
            out = "no_change"
        elif gmv_d > 2:
            out = "recovered"
        else:
            out = "declined"
        add_action(m, "price_increase",
                   {"increase_rupees": inc, "increase_pct": round(shock, 1)},
                   started=rng.randint(30, 85), dur=30, outcome=out, gmv_d=gmv_d, txn_d=txn_d,
                   note={"no_change": f"volume held at +{shock:.0f}% price",
                         "declined": f"lost {abs(txn_d):.0f}% of transactions at +{shock:.0f}% price",
                         "recovered": f"margin gain outweighed a {abs(txn_d):.0f}% volume dip"}[out])

    for m in festive_trials:
        prestocked = rng.random() < 0.5
        add_action(m, "festive_prestock", {"festival": "Diwali 2025", "extra_stock_pct": 60 if prestocked else 0},
                   started=315, dur=10,
                   outcome="recovered" if prestocked else "no_change",
                   gmv_d=rng.uniform(190, 240) if prestocked else rng.uniform(85, 110),
                   txn_d=rng.uniform(180, 230) if prestocked else rng.uniform(80, 105),
                   note="pre-stocked and captured the festive surge" if prestocked
                   else "ran out of stock mid-surge")

    # ---- daily/hourly rollups ----
    action_by_merchant: dict[str, list[ActionRecord]] = {}
    for a in ds.actions:
        action_by_merchant.setdefault(a.merchant_id, []).append(a)

    for m in ds.merchants:
        curve = _hour_curve(m.pattern)
        tot_w = sum(curve.values())
        ticket_mean = m.ticket_mean or (sum(m.ticket) / 2 * 0.62)
        for back in range(DAYS - 1, -1, -1):
            d = TODAY - timedelta(days=back)
            mult = _seasonal(d) * rng.uniform(0.93, 1.07)

            # the hero's evening decline: last 10 days, 18:00-21:00 only
            hero_evening_hit = 1.0
            if m.id == hero.id and back <= 9:
                hero_evening_hit = 0.70

            # merchants running an experiment get a real lift in the data
            lift = 1.0
            for a in action_by_merchant.get(m.id, []):
                if a.started_days_ago >= back > a.started_days_ago - a.duration_days:
                    lift *= 1 + (a.gmv_delta_pct / 100) * (0.6 if a.outcome == "no_change" else 1.0)

            txns = max(1, int(m.base_txns * mult * lift))
            by_hour: dict[int, float] = {}
            gmv = 0.0
            for h, w in curve.items():
                share = w / tot_w
                n = txns * share
                if m.id == hero.id and 18 <= h <= 20 and back <= 9:
                    n *= hero_evening_hit
                if n <= 0:
                    continue
                amt = n * ticket_mean * rng.uniform(0.96, 1.04)
                by_hour[h] = round(amt, 2)
                gmv += amt
            ds.rolls.append(DayRoll(m.id, d.isoformat(), txns, round(gmv, 2), by_hour))

    # ---- upcoming expense ledger ----
    for m in ds.merchants:
        avg = ds.avg_daily_gmv(m.id)
        ds.expenses.append(Expense(m.id, "Shop rent", round(avg * 2.4, -2), 6, True))
        ds.expenses.append(Expense(m.id, "Supplier payment", round(avg * 1.6, -2), 3, True))
        ds.expenses.append(Expense(m.id, "Electricity", round(avg * 0.35, -2), 11, True))
        if rng.random() < 0.5:
            ds.expenses.append(Expense(m.id, "Loan EMI", round(avg * 0.9, -2), 9, True))

    return ds


# --------------------------------------------------------------------------

_CACHE: Dataset | None = None


def dataset() -> Dataset:
    global _CACHE
    if _CACHE is None:
        _CACHE = build()
    return _CACHE


def dump(path: str | Path) -> dict[str, int]:
    ds = dataset()
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    (p / "merchants.json").write_text(json.dumps([m.as_dict() for m in ds.merchants], indent=2))
    (p / "rolls.json").write_text(json.dumps([asdict(r) for r in ds.rolls]))
    (p / "actions.json").write_text(json.dumps([asdict(a) for a in ds.actions], indent=2))
    (p / "expenses.json").write_text(json.dumps([asdict(e) for e in ds.expenses], indent=2))
    return {"merchants": len(ds.merchants), "day_rollups": len(ds.rolls),
            "actions": len(ds.actions), "expenses": len(ds.expenses)}


if __name__ == "__main__":
    print(dump(Path(__file__).resolve().parents[2] / "data"))
