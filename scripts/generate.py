"""Synthetic dataset for Business Saathi.

Deterministic: one seed, one Random instance, no use of the global random
module, one `today` captured once and written to meta. Regenerating on the same
day produces an identical database.

Eight patterns are seeded deliberately (see docs/DESIGN.md "Synthetic data
generator"). Nothing the demo says is a literal in the code -- every figure is
computed back out by scripts/verify_claims.py.

The ordering here matters and is the fix for the oldest bug in this file.
Actions are PLANNED first, their effect is written into the ledger while the
transactions are generated, and only then is each outcome MEASURED back out of
the rows. An outcome can therefore never disagree with the ledger it claims to
describe -- previously they agreed roughly half the time, because the outcome
figures were invented next to the ledger instead of being read from it.

Usage:  python scripts/generate.py [db_path]
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

SEED = 505
DAYS = 365
PER_CELL = 8                       # 5 categories x 4 localities x 8 = 160 merchants
OPEN_HOUR, CLOSE_HOUR = 7, 22      # 16 hourly buckets

# Real Diwali dates. The festive spike is placed on the CALENDAR, not at a
# fixed offset from the generation date, so "last Diwali" is actually Diwali
# and a merchant looking at the chart sees the spike where they remember it.
DIWALI = {
    2023: date(2023, 11, 12), 2024: date(2024, 11, 1), 2025: date(2025, 10, 20),
    2026: date(2026, 11, 8), 2027: date(2027, 10, 29), 2028: date(2028, 10, 17),
}

# ---------------------------------------------------------------- pattern 1
# Hour profile per category x locality. College-area food stalls peak at lunch
# and early evening; office-park kiranas peak before work and after it. This is
# what makes the similarity vector meaningful rather than decorative.
PEAKS = {
    ("food_stall", "college_area"): [(12, 2.6), (13, 2.9), (14, 1.6), (17, 2.2), (18, 2.8), (19, 2.4)],
    ("food_stall", "office_park"): [(9, 2.0), (13, 3.0), (14, 2.0), (18, 2.2), (19, 1.6)],
    ("food_stall", "residential_colony"): [(8, 1.8), (13, 1.6), (18, 2.2), (19, 2.6), (20, 2.2)],
    ("food_stall", "market_street"): [(11, 1.8), (13, 2.2), (16, 1.9), (18, 2.4), (19, 2.2), (20, 1.8)],
    ("kirana", "college_area"): [(11, 1.6), (17, 2.0), (19, 2.4), (20, 2.2)],
    ("kirana", "office_park"): [(9, 2.2), (10, 1.9), (19, 2.6), (20, 2.4), (21, 1.8)],
    ("kirana", "residential_colony"): [(8, 2.0), (9, 2.2), (18, 2.2), (19, 2.6), (20, 2.4)],
    ("kirana", "market_street"): [(10, 2.0), (12, 1.8), (17, 2.2), (19, 2.2)],
    ("salon", "college_area"): [(15, 2.0), (16, 2.2), (17, 2.4), (18, 2.2)],
    ("salon", "office_park"): [(12, 1.8), (18, 2.4), (19, 2.6)],
    ("salon", "residential_colony"): [(11, 2.0), (16, 2.2), (18, 2.4)],
    ("salon", "market_street"): [(12, 1.9), (16, 2.2), (17, 2.4), (18, 2.0)],
    ("pharmacy", "college_area"): [(11, 1.6), (18, 1.9), (20, 1.7)],
    ("pharmacy", "office_park"): [(10, 1.8), (13, 1.6), (19, 2.0)],
    ("pharmacy", "residential_colony"): [(9, 1.8), (12, 1.5), (19, 2.2), (20, 2.0)],
    ("pharmacy", "market_street"): [(11, 1.7), (17, 1.9), (19, 1.9)],
    ("mobile_accessories", "college_area"): [(13, 1.8), (16, 2.2), (17, 2.4), (19, 2.0)],
    ("mobile_accessories", "office_park"): [(13, 2.0), (18, 2.2), (19, 2.0)],
    ("mobile_accessories", "residential_colony"): [(17, 2.0), (19, 2.2), (20, 1.8)],
    ("mobile_accessories", "market_street"): [(12, 2.0), (15, 2.2), (17, 2.4), (19, 2.2)],
}

TICKET = {          # (mean, sigma) rupees per payment
    "food_stall": (42, 18), "kirana": (185, 90), "salon": (260, 120),
    "pharmacy": (210, 140), "mobile_accessories": (390, 260),
}
DAILY_TXNS = {      # (mean, sigma) payments per day
    "food_stall": (112, 28), "kirana": (74, 20), "salon": (22, 7),
    "pharmacy": (48, 14), "mobile_accessories": (19, 6),
}
STOCK_ITEMS = {
    "food_stall": [("cold drink", "bottles"), ("samosa", "pieces")],
    "kirana": [("cooking oil", "litres"), ("atta", "kg")],
    "pharmacy": [("paracetamol", "strips")],
    "mobile_accessories": [("charging cable", "pieces"), ("earphones", "pieces")],
    "salon": [("hair colour", "packets")],
}

GOOD = {"recovered", "sustained", "captured_festive"}

# ---------------------------------------------------------------- pattern 9
# Two genuinely different businesses filed under ONE onboarding label.
#
# Real category pickers are coarse: a corner kirana and a cafe both tick "Food
# & Beverage". If the cohort is decided by that label, their results get pooled
# and the cafe is told what worked for kiranas. Without a case like this in the
# data, the similarity engine cannot be shown to handle it -- every synthetic
# category behaved differently from every other, so the label was accidentally
# always right.
#
# So a third of the food stalls are cafes: same label, different trade. Higher
# ticket, afternoon-and-evening rhythm, far fewer transactions. Behaviour
# should separate them; the label cannot.
SUBTYPES = {
    "chai_stall": {"ticket": 1.00, "txn_scale": 1.00,
                   "peaks": [(8, 2.4), (9, 1.9), (12, 2.4), (13, 2.6),
                             (17, 2.2), (18, 2.6), (19, 2.2)]},
    "cafe": {"ticket": 3.60, "txn_scale": 0.30,
             "peaks": [(11, 1.6), (15, 2.4), (16, 2.8), (17, 2.6),
                       (19, 2.2), (20, 2.4), (21, 1.8)]},
}

# How an action's intended effect is written into the ledger. `hours` is None
# for an all-day action. `worked` decides which uplift band is sampled.
ACTION_EFFECT = {
    "evening_offer": {"hours": (18, 21), "win": (1.22, 1.42), "lose": (0.92, 1.05)},
    "deep_discount": {"hours": None, "win": (1.10, 1.26), "lose": (0.93, 1.06)},
    "moderate_discount": {"hours": None, "win": (1.09, 1.24), "lose": (0.95, 1.05)},
    "price_increase": {"hours": None, "win": (1.02, 1.06), "lose": (0.86, 0.99)},
    "prep_increase": {"hours": None, "win": (1.10, 1.30), "lose": (0.96, 1.05)},
    "festive_prestock": {"hours": None, "win": (1.85, 2.40), "lose": (1.02, 1.25)},
}

# Measured delta -> verdict. One table, used by the generator and quoted in
# docs, so a seeded outcome and a live one are classified the same way.
def classify(delta_pct: float, action_type: str, festive: bool = False) -> str:
    if festive:
        return "captured_festive" if delta_pct > 60 else "no_change"
    if action_type == "price_increase":
        if delta_pct > 1.5:
            return "sustained"
        if delta_pct > -3:
            return "no_change"
        return "minor_loss" if delta_pct > -9 else "worse"
    if delta_pct > 8:
        return "recovered" if "evening" in action_type else "sustained"
    if delta_pct > -5:
        # A discount that moved nothing still cost margin, so a flat result is
        # a failure, not a neutral. "temporary_spike" is reserved for the deep
        # discount that lifts during the window and gives it all back.
        return "temporary_spike" if action_type == "deep_discount" and delta_pct > 2 \
            else "no_change"
    return "worse"


# --------------------------------------------------------------------------
def hourly_vector(category: str, locality_type: str, rng: random.Random,
                  subtype: str | None = None) -> list[float]:
    peaks = SUBTYPES[subtype]["peaks"] if subtype else PEAKS[(category, locality_type)]
    vec = [0.35] * (CLOSE_HOUR - OPEN_HOUR + 1)
    for hour, weight in peaks:
        i = hour - OPEN_HOUR
        vec[i] = max(vec[i], weight)
        for off in (-1, 1):
            j = i + off
            if 0 <= j < len(vec):
                vec[j] = max(vec[j], weight * 0.55)
    vec = [max(0.05, v * rng.uniform(0.85, 1.15)) for v in vec]
    total = sum(vec)
    return [round(v / total, 5) for v in vec]


def band_for(avg_daily: float, category: str) -> str:
    mt, dt = TICKET[category][0], DAILY_TXNS[category][0]
    ratio = avg_daily / (mt * dt)
    return "low" if ratio < 0.75 else ("mid" if ratio < 1.25 else "high")


def add_months(anchor: date, months: int) -> date:
    """Calendar month arithmetic. Stepping by 32 days skipped a month roughly
    every seven steps and duplicated another, which produced merchants with two
    rent rows in one month and none in the next."""
    total = (anchor.year * 12 + anchor.month - 1) + months
    return date(total // 12, total % 12 + 1, min(anchor.day, 28))


def festive_window(today: date, start_day: date) -> tuple[date, date]:
    """The most recent Diwali inside the generated range."""
    candidates = [d for d in DIWALI.values() if start_day + timedelta(days=6) <= d <= today]
    anchor = max(candidates) if candidates else today - timedelta(days=332)
    return anchor - timedelta(days=2), anchor + timedelta(days=2)


# ---------------------------------------------------------------- pattern 7
def day_multiplier(d: date, category: str, rng: random.Random,
                   noise: float = 0.15) -> float:
    m = 1.0
    if d.weekday() >= 5:
        m *= 1.18 if category in ("food_stall", "salon", "mobile_accessories") else 0.94
    if d.day >= 27:
        m *= 0.90          # month-end squeeze
    elif d.day <= 5:
        m *= 1.08          # post-payday
    m *= rng.gauss(1.0, noise)
    return max(0.15, m)


def generation_date() -> date:
    """`today` for the whole dataset.

    Read from SAATHI_TODAY when set, so the same code produces the same
    database on a laptop in IST and a container in UTC. Without it the two
    silently diverge: every window here is anchored to this date, so a one-day
    difference moves the demo merchant's decline, the festive window and the
    obligation schedule, and the figures you rehearsed stop matching the ones
    on screen.
    """
    if config.GENERATION_DATE:
        return date.fromisoformat(config.GENERATION_DATE)
    return date.today()


def main() -> None:
    rng = random.Random(SEED)
    today = generation_date()
    start_day = today - timedelta(days=DAYS)
    fest_start, fest_end = festive_window(today, start_day)
    market_event_start = today - timedelta(days=120)  # pattern 4: market-wide dip
    market_event_end = market_event_start + timedelta(days=6)

    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            p.unlink()

    con = sqlite3.connect(db_path)
    con.executescript((ROOT / "backend" / "data" / "schema.sql").read_text())
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    # ---------------------------------------------------------- merchants
    merchants, n = [], 0
    for category in config.CATEGORIES:
        for locality, ltype in config.LOCALITIES:
            for _ in range(PER_CELL):
                n += 1
                mt, dt = TICKET[category][0], DAILY_TXNS[category][0]
                avg_daily = max(400.0, rng.gauss(mt * dt, mt * dt * 0.30))
                merchants.append({
                    "id": f"M{n:03d}",
                    "name": f"{category.replace('_', ' ').title()} {n}",
                    "category": category, "locality": locality, "locality_type": ltype,
                    "opened_on": (start_day - timedelta(days=rng.randint(30, 900))).isoformat(),
                    "volume_band": band_for(avg_daily, category),
                    "avg_daily": round(avg_daily, 2),
                    "hourly_vector": hourly_vector(category, ltype, rng),
                    "subtype": None,
                    # tier B availability: ~40% obligations, ~30% stock feed
                    "has_obligations": 1 if rng.random() < 0.40 else 0,
                    "has_stock_feed": 1 if rng.random() < 0.30 else 0,
                    # per-merchant ticket personality, so the average ticket is
                    # not a category constant divided back out of the amount
                    "ticket_bias": rng.uniform(0.80, 1.25),
                    "txn_scale": 1.0,
                })

    demo = merchants[0]
    assert demo["category"] == "food_stall" and demo["locality_type"] == "college_area"
    demo.update({"name": "Sharma Chai Corner", "avg_daily": 5300.0,
                 "volume_band": band_for(5300.0, "food_stall"),
                 "has_obligations": 1, "has_stock_feed": 0, "ticket_bias": 1.0})

    # The demo cell is a deliberately coherent cohort: same trade, same
    # locality, same volume band. Left to chance, a couple of these merchants
    # land in a neighbouring band, the tight cohort drops under the privacy
    # floor, and the collective answer disappears on stage for a reason that
    # looks like a bug but is the privacy gate doing its job.
    for m in merchants:
        if m["category"] == "food_stall" and m["locality_type"] == "college_area":
            m["avg_daily"] = round(rng.uniform(4200.0, 5750.0), 2) if m["id"] != "M001" \
                else 5300.0
            m["volume_band"] = band_for(m["avg_daily"], "food_stall")

    # pattern 9: cafes under the food_stall label.
    #
    # In every cell except the demo's, the last three food stalls become cafes.
    # The demo cell has no spare merchants -- M001, its six seeded decline
    # peers and the peer merchant account for all eight -- so three cafes are
    # APPENDED there instead. Converting one in place would have made the peer
    # merchant a cafe and quietly broken both the learning demo and the
    # "switch to M008" beat.
    cafe_seq = 0
    for locality, ltype in config.LOCALITIES:
        cell = [m for m in merchants
                if m["category"] == "food_stall" and m["locality"] == locality]
        for m in cell:
            m["subtype"] = "chai_stall"

        spec = SUBTYPES["cafe"]
        if ltype == "college_area":
            for _ in range(3):
                cafe_seq += 1
                avg_daily = round(rng.uniform(4200.0, 5750.0), 2)
                merchants.append({
                    "id": f"M{160 + cafe_seq:03d}",
                    "name": f"Cafe {cafe_seq}",
                    "category": "food_stall", "locality": locality,
                    "locality_type": ltype,
                    "opened_on": (start_day - timedelta(days=rng.randint(30, 900))).isoformat(),
                    "volume_band": band_for(avg_daily, "food_stall"),
                    "avg_daily": avg_daily,
                    "hourly_vector": hourly_vector("food_stall", ltype, rng,
                                                   subtype="cafe"),
                    "has_obligations": 1 if rng.random() < 0.40 else 0,
                    "has_stock_feed": 1 if rng.random() < 0.30 else 0,
                    "ticket_bias": spec["ticket"],
                    "txn_scale": spec["txn_scale"],
                    "subtype": "cafe",
                })
        else:
            for m in cell[-3:]:
                cafe_seq += 1
                m["subtype"] = "cafe"
                m["ticket_bias"] = spec["ticket"]
                m["txn_scale"] = spec["txn_scale"]
                m["hourly_vector"] = hourly_vector("food_stall", ltype, rng,
                                                   subtype="cafe")
                m["name"] = f"Cafe {cafe_seq}"

    # ---------------------------------------------------------- pattern 8
    cold = {"id": config.COLDSTART_MERCHANT, "name": "New Chai Stall",
            "category": "food_stall", "locality": "Sector 62",
            "locality_type": "college_area", "opened_on": today.isoformat(),
            "volume_band": "mid", "avg_daily": 0.0,
            "hourly_vector": hourly_vector("food_stall", "college_area", rng),
            "has_obligations": 0, "has_stock_feed": 0, "ticket_bias": 1.0}

    con.executemany("INSERT INTO merchants VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(m["id"], m["name"], m["category"], m["locality"], m["locality_type"],
                      m["opened_on"], m["volume_band"], m["avg_daily"],
                      json.dumps(m["hourly_vector"]), m["has_obligations"],
                      m["has_stock_feed"]) for m in merchants + [cold]])

    demo_cell = [m["id"] for m in merchants
                 if m["category"] == "food_stall" and m["locality_type"] == "college_area"]
    # the appended cafes share the cell but are a different trade, so the
    # seeded "6 peers declined, 5 recovered" story must not draw from them
    peers = sorted(set(demo_cell) - {"M001"}
                   - {m["id"] for m in merchants if m.get("subtype") == "cafe"})
    decline_peers = peers[:6]                 # pattern 3: 6 peers hit the same condition
    recovered_peers = set(decline_peers[:5])  #            5 of them recovered
    surge_cell = [m["id"] for m in merchants
                  if m["category"] == "mobile_accessories" and m["locality_type"] == "market_street"]
    market_event_cell = [m["id"] for m in merchants
                         if m["category"] == "kirana" and m["locality_type"] == "office_park"]

    # ================================================================
    # PHASE 1 -- plan situations and actions. Nothing is measured yet.
    # ================================================================
    situations, planned = [], []
    sid = 0
    by_merchant: dict[str, list[dict]] = {m["id"]: [] for m in merchants}

    def add_situation(mid, kind, on, severity, band, peer_rel) -> int:
        nonlocal sid
        sid += 1
        situations.append((sid, mid, kind, on.isoformat(), round(severity, 2), band,
                           peer_rel, json.dumps({"seeded": True})))
        return sid

    def plan(mid, atype, params, situation_id, start, length, worked, festive=False):
        """Record the intent. The ledger effect is written in phase 2 and the
        outcome is measured out of those rows in phase 3."""
        spec = ACTION_EFFECT[atype]
        lo, hi = spec["win"] if worked else spec["lose"]
        entry = {
            "merchant_id": mid, "type": atype, "params": params,
            "situation_id": situation_id, "start": start,
            "end": start + timedelta(days=length), "length": length,
            "hours": spec["hours"], "factor": rng.uniform(lo, hi), "festive": festive,
        }
        planned.append(entry)
        by_merchant[mid].append(entry)

    # pattern 2: the demo merchant's own live evening decline
    demo_decline_start = today - timedelta(days=12)

    # pattern 3, the deliberate cohort story: 6 peers declined, all ran an
    # evening offer, 5 recovered. Computed back out as "5 of 6" by the graph.
    for m in merchants:
        if m["id"] in decline_peers:
            d_start = today - timedelta(days=rng.randint(70, 200))
            # 14 days, not 9: the outcome measures `before` over the 12 days
            # preceding the offer, so a shorter gap put pre-decline trading
            # inside that window, inflated the baseline, and turned a genuine
            # recovery into a measured non-event.
            o_start = d_start + timedelta(days=14)
            m["_decline_start"] = d_start
            m["_offer_start"] = o_start
            s = add_situation(m["id"], "evening_decline", d_start,
                              -21.0 + rng.uniform(-3, 3), "19-22", "specific_to_merchant")
            plan(m["id"], "evening_offer",
                 {"discount_rs": rng.choice([5, 10, 10, 15]), "window": "18-21", "days": 3},
                 s, o_start, 3, worked=m["id"] in recovered_peers)

    # the rest of the population: breadth, and honest failure rates per bucket
    for m in merchants:
        for _ in range(rng.randint(1, 4)):
            start = start_day + timedelta(days=rng.randint(20, DAYS - 20))
            roll = rng.random()
            if roll < 0.26:
                s = add_situation(m["id"], "sales_decline", start, -14 + rng.uniform(-6, 4),
                                  None, "unknown")
                plan(m["id"], "deep_discount",
                     {"discount_pct": rng.randint(25, 40), "days": 5},
                     s, start, 5, worked=rng.random() < 0.28)
            elif roll < 0.50:
                s = add_situation(m["id"], "sales_decline", start, -11 + rng.uniform(-5, 4),
                                  None, "unknown")
                plan(m["id"], "moderate_discount",
                     {"discount_pct": rng.randint(10, 15), "days": 5},
                     s, start, 5, worked=rng.random() < 0.68)
            elif roll < 0.66:
                s = add_situation(m["id"], "margin_pressure", start, 0.0, None, "unknown")
                plan(m["id"], "price_increase",
                     {"increase_rs": rng.choice([5, 10, 10, 15])},
                     s, start, 14, worked=rng.random() < 0.40)
            elif roll < 0.80:
                if m["id"] in demo_cell:
                    continue      # the demo cohort's evening story stays seeded, not random
                s = add_situation(m["id"], "evening_decline", start, -18 + rng.uniform(-6, 5),
                                  "19-22", "unknown")
                plan(m["id"], "evening_offer",
                     {"discount_rs": rng.choice([5, 10, 15]), "window": "18-21", "days": 3},
                     s, start, 3, worked=rng.random() < 0.71)
            elif roll < 0.90:
                s = add_situation(m["id"], "demand_surge", start, 22 + rng.uniform(-6, 10),
                                  None, "unknown")
                plan(m["id"], "prep_increase",
                     {"increase_pct": rng.choice([20, 30, 40])},
                     s, start, 7, worked=rng.random() < 0.66)
            else:
                if m["category"] in ("food_stall", "kirana", "mobile_accessories"):
                    s = add_situation(m["id"], "festive_window",
                                      fest_start - timedelta(days=3), 0.0, None, "market_wide")
                    plan(m["id"], "festive_prestock", {"multiplier": 2.0}, s,
                         fest_start - timedelta(days=3), 8,
                         worked=rng.random() < 0.55, festive=True)

    # ================================================================
    # PHASE 2 -- transactions, with every planned effect written in.
    # ================================================================
    rows, payment_rows, pid = [], [], 0
    daily: dict[str, dict[str, float]] = {}      # merchant -> day -> amount (all hours)
    banded: dict[str, dict[str, float]] = {}     # merchant -> day -> amount (18..21)

    for m in merchants:
        mid = m["id"]
        vec = m["hourly_vector"]
        opened = date.fromisoformat(m["opened_on"])
        mean_ticket, sig_ticket = TICKET[m["category"]]
        decline_start = demo_decline_start if mid == "M001" else None
        peer_decline_start = m.get("_decline_start")
        peer_offer_start = m.get("_offer_start")
        surge_start = today - timedelta(days=9) if mid in surge_cell else None
        effects = by_merchant[mid]
        daily[mid], banded[mid] = {}, {}

        for offset in range(DAYS):
            d = start_day + timedelta(days=offset)
            if d < opened:
                continue
            # The demo merchant's decline week must be legible, so its daily
            # noise is damped inside the decline window. Without this the
            # headline figure depends on which seven days "today" happens to
            # land on, and the demo number moves between runs.
            in_demo_decline = decline_start is not None and d >= decline_start
            if rng.random() < (0.004 if in_demo_decline else 0.012):
                continue                                   # closed day
            mult = day_multiplier(d, m["category"], rng,
                                  noise=0.05 if in_demo_decline else 0.15)
            if fest_start <= d <= fest_end and m["category"] in (
                    "food_stall", "kirana", "mobile_accessories"):
                mult *= rng.uniform(3.1, 3.7)
            if mid in market_event_cell and market_event_start <= d <= market_event_end:
                mult *= rng.uniform(0.70, 0.80)
            if surge_start and d >= surge_start:
                mult *= rng.uniform(1.25, 1.45)
            day_total = m["avg_daily"] * mult

            # a per-day average ticket, so avg_ticket is a real observable
            ticket_today = max(6.0, rng.gauss(mean_ticket * m["ticket_bias"],
                                              sig_ticket * 0.45 * m["ticket_bias"]))

            day_amt = band_amt = 0.0
            for i, w in enumerate(vec):
                hour = OPEN_HOUR + i
                amount = day_total * w
                # ------------------------------------------------ pattern 2
                if decline_start and d >= decline_start and 17 <= hour <= 21:
                    amount *= rng.uniform(0.40, 0.50)
                # ------------------------------------------------ pattern 3
                # The decline persists. Whether it lifts is decided ONLY by the
                # planned action below -- applying a recovery here as well made
                # every peer measure as recovered, at roughly double the uplift
                # the action was supposed to have produced.
                if peer_decline_start and d >= peer_decline_start and 17 <= hour <= 21:
                    amount *= rng.uniform(0.74, 0.84)
                # ------- planned actions: the effect the outcome will measure
                for e in effects:
                    if e["start"] <= d < e["end"]:
                        if e["hours"] is None or e["hours"][0] <= hour <= e["hours"][1]:
                            amount *= e["factor"]
                amount *= rng.uniform(0.88, 1.12)
                if amount < 5:
                    continue
                txns = max(1, int(round(amount / ticket_today)))
                rows.append((mid, d.isoformat(), hour, txns, round(amount, 2)))
                day_amt += amount
                if 18 <= hour <= 21:
                    band_amt += amount

                if mid in demo_cell and offset > DAYS - 45:
                    for _ in range(txns):
                        pid += 1
                        status = "success"
                        r = rng.random()
                        if r < 0.012:
                            status = "failed"
                        elif r < 0.018:
                            status = "refunded"
                        payment_rows.append((
                            pid, mid,
                            f"{d.isoformat()}T{hour:02d}:{rng.randint(0, 59):02d}:00",
                            round(max(5.0, rng.gauss(ticket_today, sig_ticket)), 2), status))

            daily[mid][d.isoformat()] = day_amt
            banded[mid][d.isoformat()] = band_amt

        if len(rows) > 200_000:
            con.executemany("INSERT INTO txn_hourly VALUES (?,?,?,?,?)", rows)
            rows = []

    con.executemany("INSERT INTO txn_hourly VALUES (?,?,?,?,?)", rows)
    con.executemany("INSERT INTO payments VALUES (?,?,?,?,?)", payment_rows)

    # ================================================================
    # PHASE 3 -- measure every outcome out of the rows just written.
    # ================================================================
    def mean_over(store: dict, mid: str, start: date, end: date) -> tuple[float, int]:
        vals = [v for k, v in store[mid].items()
                if start.isoformat() <= k <= end.isoformat() and v > 0]
        return (sum(vals) / len(vals) if vals else 0.0), len(vals)

    actions, outcomes = [], []
    for aid, e in enumerate(planned, start=1):
        mid = e["merchant_id"]
        store = banded if e["hours"] else daily
        before, nb = mean_over(store, mid, e["start"] - timedelta(days=12),
                               e["start"] - timedelta(days=1))
        after, na = mean_over(store, mid, e["start"], e["end"] - timedelta(days=1))
        if not nb or not na or before <= 0:
            continue                       # not enough ledger to measure honestly
        delta = (after - before) / before * 100.0
        verdict = classify(delta, e["type"], festive=e["festive"])
        actions.append((aid, mid, e["situation_id"], e["type"], json.dumps(e["params"]),
                        e["start"].isoformat(), e["end"].isoformat(), "historical", None))
        outcomes.append((aid, aid, "evening_gmv" if e["hours"] else "daily_gmv",
                         round(before, 2), round(after, 2), round(delta, 2), verdict,
                         (e["end"] + timedelta(days=2)).isoformat(), 1))

    con.executemany("INSERT INTO situations VALUES (?,?,?,?,?,?,?,?)", situations)
    con.executemany("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)", actions)
    con.executemany("INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?)", outcomes)

    # ----------------------------------------------- optional tier B data
    obligations, stock, eid, stid = [], [], 0, 0
    for m in merchants:
        if m["has_obligations"]:
            rent = round(m["avg_daily"] * rng.uniform(2.2, 4.0), -2)
            anchor = today.replace(day=5)
            for month_offset in range(-12, 2):
                due = add_months(anchor, month_offset)
                eid += 1
                obligations.append((eid, m["id"], due.isoformat(), rent, "rent", 1,
                                    "integration"))
            for _ in range(rng.randint(1, 3)):
                eid += 1
                obligations.append((eid, m["id"],
                                    (today + timedelta(days=rng.randint(1, 21))).isoformat(),
                                    round(m["avg_daily"] * rng.uniform(0.8, 2.4), -1),
                                    "supplier", 0, "integration"))
        if m["has_stock_feed"] and m["category"] in STOCK_ITEMS:
            for label, unit in STOCK_ITEMS[m["category"]]:
                stid += 1
                stock.append((stid, m["id"], label, round(rng.uniform(8, 90)), unit,
                              today.isoformat(), "integration"))

    # M001's upcoming supplier bill is pinned so the money story is stable. Its
    # rent already exists on the monthly schedule above -- adding a second one
    # here previously produced two rents inside the same 30-day window.
    eid += 1
    obligations.append((eid, "M001", (today + timedelta(days=4)).isoformat(), 12000.0,
                        "supplier", 0, "integration"))
    con.executemany("INSERT INTO obligations VALUES (?,?,?,?,?,?,?)", obligations)
    con.executemany("INSERT INTO stock_snapshots VALUES (?,?,?,?,?,?,?)", stock)

    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("generated_on", today.isoformat()), ("today", today.isoformat()),
        ("days", str(DAYS)), ("seed", str(SEED)),
        ("festive_start", fest_start.isoformat()), ("festive_end", fest_end.isoformat()),
        ("market_event_start", market_event_start.isoformat()),
        ("market_event_end", market_event_end.isoformat()),
        ("market_event_cohort", "kirana|office_park"),
        ("surge_cohort", "mobile_accessories|market_street"),
        ("demo_merchant", "M001"), ("coldstart_merchant", config.COLDSTART_MERCHANT),
    ])
    con.commit()

    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("merchants", "txn_hourly", "payments", "situations", "actions",
                        "outcomes", "obligations", "stock_snapshots")}
    with_ob = con.execute("SELECT COUNT(*) FROM merchants WHERE has_obligations=1").fetchone()[0]
    with_st = con.execute("SELECT COUNT(*) FROM merchants WHERE has_stock_feed=1").fetchone()[0]

    # ---- the demo story must be true in the rows, not just intended in code
    cur = con.execute(
        "SELECT COALESCE(SUM(amount),0), COUNT(DISTINCT day) FROM txn_hourly "
        "WHERE merchant_id='M001' AND day>=? AND day<=?",
        ((today - timedelta(days=7)).isoformat(), (today - timedelta(days=1)).isoformat())
    ).fetchone()
    base = con.execute(
        "SELECT COALESCE(SUM(amount),0), COUNT(DISTINCT day) FROM txn_hourly "
        "WHERE merchant_id='M001' AND day>=? AND day<=?",
        ((today - timedelta(days=37)).isoformat(), (today - timedelta(days=8)).isoformat())
    ).fetchone()
    change = ((cur[0] / cur[1]) - (base[0] / base[1])) / (base[0] / base[1]) * 100.0

    subtypes = {}
    for name, count in con.execute(
            "SELECT CASE WHEN name LIKE 'Cafe%' THEN 'cafe' ELSE 'other' END s, "
            "COUNT(*) FROM merchants GROUP BY s"):
        subtypes[name] = count
    con.close()

    print(f"built {db_path}")
    print(f"  generated for      {today}"
          f"{' (pinned by SAATHI_TODAY)' if config.GENERATION_DATE else ' (system clock)'}")
    for k, v in counts.items():
        print(f"  {k:16s} {v:>9,}")
    print(f"  merchants with obligations: {with_ob}  with stock feed: {with_st}")
    print("  (the rest exercise the absence paths on purpose)")
    print(f"  festive window          {fest_start} .. {fest_end}")
    print(f"  demo merchant 7d change {change:+.1f}%")
    print(f"  cafes sharing the food_stall label: {subtypes.get('cafe', 0)}")
    if change > -10:
        raise SystemExit(
            f"demo merchant's decline measured {change:+.1f}%, which is not a "
            f"story the product can tell. The seeded decline did not survive "
            f"into the ledger -- fix the generator, do not lower the bar.")
    # The cohort itself is asserted in scripts/recompute_patterns.py, which is
    # where the behavioural profiles it depends on are computed.


if __name__ == "__main__":
    main()
