"""Synthetic dataset for Business Saathi.

Deterministic: one seed, one Random instance, no use of the global random
module, one `today` captured once and written to meta. Regenerating produces
an identical database.

Eight patterns are seeded deliberately (see docs/DESIGN.md "Synthetic data
generator"). Nothing the demo says is a literal in the code -- every figure is
computed back out by scripts/verify_claims.py.

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


# --------------------------------------------------------------------------
def hourly_vector(category: str, locality_type: str, rng: random.Random) -> list[float]:
    peaks = PEAKS[(category, locality_type)]
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


# ---------------------------------------------------------------- pattern 7
def day_multiplier(d: date, category: str, rng: random.Random) -> float:
    m = 1.0
    if d.weekday() >= 5:
        m *= 1.18 if category in ("food_stall", "salon", "mobile_accessories") else 0.94
    if d.day >= 27:
        m *= 0.90          # month-end squeeze
    elif d.day <= 5:
        m *= 1.08          # post-payday
    m *= rng.gauss(1.0, 0.15)
    return max(0.15, m)


def main() -> None:
    rng = random.Random(SEED)
    today = date.today()
    start_day = today - timedelta(days=DAYS)
    fest_start = today - timedelta(days=332)          # pattern 6: last Diwali
    fest_end = fest_start + timedelta(days=4)
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
                    # tier B availability: ~40% obligations, ~30% stock feed
                    "has_obligations": 1 if rng.random() < 0.40 else 0,
                    "has_stock_feed": 1 if rng.random() < 0.30 else 0,
                })

    demo = merchants[0]
    assert demo["category"] == "food_stall" and demo["locality_type"] == "college_area"
    demo.update({"name": "Sharma Chai Corner", "avg_daily": 5300.0,
                 "volume_band": band_for(5300.0, "food_stall"),
                 "has_obligations": 1, "has_stock_feed": 0})
    # M001 has obligations (so the money answer is a net position) but NO stock
    # feed (so the planning question must ask the merchant). Both are demo beats.

    # ---------------------------------------------------------- pattern 8
    cold = {"id": config.COLDSTART_MERCHANT, "name": "New Chai Stall",
            "category": "food_stall", "locality": "Sector 62",
            "locality_type": "college_area", "opened_on": today.isoformat(),
            "volume_band": "mid", "avg_daily": 0.0,
            "hourly_vector": hourly_vector("food_stall", "college_area", rng),
            "has_obligations": 0, "has_stock_feed": 0}

    con.executemany("INSERT INTO merchants VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(m["id"], m["name"], m["category"], m["locality"], m["locality_type"],
                      m["opened_on"], m["volume_band"], m["avg_daily"],
                      json.dumps(m["hourly_vector"]), m["has_obligations"],
                      m["has_stock_feed"]) for m in merchants + [cold]])

    # ------------------------------------------------------- transactions
    demo_cell = [m["id"] for m in merchants
                 if m["category"] == "food_stall" and m["locality_type"] == "college_area"]
    peers = sorted(set(demo_cell) - {"M001"})
    decline_peers = peers[:6]                 # pattern 3: 6 peers hit the same condition
    recovered_peers = set(decline_peers[:5])  #            5 of them recovered
    # pattern 5: a demand surge in one cell, deliberately without stock data
    surge_cell = [m["id"] for m in merchants
                  if m["category"] == "mobile_accessories" and m["locality_type"] == "market_street"]
    # pattern 4: a market-wide dip for one whole locality cohort
    market_event_cell = [m["id"] for m in merchants
                         if m["category"] == "kirana" and m["locality_type"] == "office_park"]

    rows, payment_rows, pid = [], [], 0
    for m in merchants:
        vec = m["hourly_vector"]
        opened = date.fromisoformat(m["opened_on"])
        mean_ticket, sig_ticket = TICKET[m["category"]]
        decline_start = today - timedelta(days=12) if m["id"] == "M001" else None
        peer_decline_start = peer_offer_start = None
        if m["id"] in decline_peers:
            peer_decline_start = today - timedelta(days=rng.randint(70, 200))
            peer_offer_start = peer_decline_start + timedelta(days=9)
            m["_decline_start"] = peer_decline_start
            m["_offer_start"] = peer_offer_start
        surge_start = today - timedelta(days=9) if m["id"] in surge_cell else None

        for offset in range(DAYS):
            d = start_day + timedelta(days=offset)
            if d < opened or rng.random() < 0.012:      # closed day
                continue
            mult = day_multiplier(d, m["category"], rng)
            if fest_start <= d <= fest_end and m["category"] in (
                    "food_stall", "kirana", "mobile_accessories"):
                mult *= rng.uniform(3.1, 3.7)
            if m["id"] in market_event_cell and market_event_start <= d <= market_event_end:
                mult *= rng.uniform(0.70, 0.80)
            if surge_start and d >= surge_start:
                mult *= rng.uniform(1.25, 1.45)
            day_total = m["avg_daily"] * mult

            for i, w in enumerate(vec):
                hour = OPEN_HOUR + i
                amount = day_total * w
                # ------------------------------------------------ pattern 2
                if decline_start and d >= decline_start and 17 <= hour <= 21:
                    amount *= rng.uniform(0.50, 0.62)
                # ------------------------------------------------ pattern 3
                if peer_decline_start and d >= peer_decline_start and 17 <= hour <= 21:
                    if d < peer_offer_start:
                        amount *= rng.uniform(0.74, 0.84)
                    elif m["id"] in recovered_peers:
                        amount *= rng.uniform(0.97, 1.10)
                    else:
                        amount *= rng.uniform(0.80, 0.90)
                amount *= rng.uniform(0.88, 1.12)
                if amount < 5:
                    continue
                txns = max(1, int(round(amount / max(8.0, mean_ticket))))
                rows.append((m["id"], d.isoformat(), hour, txns, round(amount, 2)))

                if m["id"] in demo_cell and offset > DAYS - 45:
                    for _ in range(txns):
                        pid += 1
                        status = "success"
                        r = rng.random()
                        if r < 0.012:
                            status = "failed"
                        elif r < 0.018:
                            status = "refunded"
                        payment_rows.append((
                            pid, m["id"],
                            f"{d.isoformat()}T{hour:02d}:{rng.randint(0, 59):02d}:00",
                            round(max(5.0, rng.gauss(mean_ticket, sig_ticket)), 2), status))

        if len(rows) > 200_000:
            con.executemany("INSERT INTO txn_hourly VALUES (?,?,?,?,?)", rows)
            rows = []

    con.executemany("INSERT INTO txn_hourly VALUES (?,?,?,?,?)", rows)
    con.executemany("INSERT INTO payments VALUES (?,?,?,?,?)", payment_rows)

    # -------------------------------------------- situations, actions, outcomes
    situations, actions, outcomes = [], [], []
    sid = aid = oid = 0

    def add_situation(mid, kind, on, severity, band, peer_rel) -> int:
        nonlocal sid
        sid += 1
        situations.append((sid, mid, kind, on.isoformat(), round(severity, 2), band,
                           peer_rel, json.dumps({"seeded": True})))
        return sid

    def add(mid, atype, params, situation_id, start, length, before, after, verdict):
        nonlocal aid, oid
        aid += 1
        oid += 1
        actions.append((aid, mid, situation_id, atype, json.dumps(params),
                        start.isoformat(),
                        (start + timedelta(days=length)).isoformat(), "historical", None))
        delta = (after - before) / before * 100.0 if before else 0.0
        outcomes.append((oid, aid, "evening_gmv" if "evening" in atype else "daily_gmv",
                         round(before, 2), round(after, 2), round(delta, 2), verdict,
                         (start + timedelta(days=length + 2)).isoformat(), 1))

    # pattern 3, the deliberate cohort story: 6 peers declined, all ran an
    # evening offer, 5 recovered. Computed back out as "5 of 6" by the graph.
    for m in merchants:
        if m["id"] in decline_peers:
            base = m["avg_daily"] * 0.34
            s = add_situation(m["id"], "evening_decline", m["_decline_start"],
                              -21.0 + rng.uniform(-3, 3), "19-22", "specific_to_merchant")
            if m["id"] in recovered_peers:
                after, verdict = base * rng.uniform(1.00, 1.10), "recovered"
            else:
                after, verdict = base * rng.uniform(0.80, 0.88), "no_change"
            add(m["id"], "evening_offer",
                {"discount_rs": rng.choice([5, 10, 10, 15]), "window": "18-21", "days": 3},
                s, m["_offer_start"], 3, base * 0.80, after, verdict)

    # the rest of the population: breadth, and honest failure rates per bucket
    for m in merchants:
        for _ in range(rng.randint(1, 4)):
            start = start_day + timedelta(days=rng.randint(20, DAYS - 20))
            base = m["avg_daily"] * rng.uniform(0.8, 1.1)
            roll = rng.random()
            if roll < 0.26:
                s = add_situation(m["id"], "sales_decline", start, -14 + rng.uniform(-6, 4),
                                  None, "unknown")
                if rng.random() < 0.28:
                    after, verdict = base * rng.uniform(1.10, 1.25), "sustained"
                else:
                    after, verdict = base * rng.uniform(0.95, 1.06), "temporary_spike"
                add(m["id"], "deep_discount", {"discount_pct": rng.randint(25, 40), "days": 5},
                    s, start, 5, base, after, verdict)
            elif roll < 0.50:
                s = add_situation(m["id"], "sales_decline", start, -11 + rng.uniform(-5, 4),
                                  None, "unknown")
                if rng.random() < 0.68:
                    after, verdict = base * rng.uniform(1.08, 1.22), "sustained"
                else:
                    after, verdict = base * rng.uniform(0.94, 1.04), "no_change"
                add(m["id"], "moderate_discount", {"discount_pct": rng.randint(10, 15), "days": 5},
                    s, start, 5, base, after, verdict)
            elif roll < 0.66:
                s = add_situation(m["id"], "margin_pressure", start, 0.0, None, "unknown")
                r2 = rng.random()
                if r2 < 0.50:
                    after, verdict = base * rng.uniform(0.98, 1.03), "no_change"
                elif r2 < 0.75:
                    after, verdict = base * rng.uniform(0.94, 0.98), "minor_loss"
                else:
                    after, verdict = base * rng.uniform(0.86, 0.90), "worse"
                add(m["id"], "price_increase", {"increase_rs": rng.choice([5, 10, 10, 15])},
                    s, start, 14, base, after, verdict)
            elif roll < 0.80:
                if m["id"] in demo_cell:
                    continue      # the demo cohort's evening story stays seeded, not random
                s = add_situation(m["id"], "evening_decline", start, -18 + rng.uniform(-6, 5),
                                  "19-22", "unknown")
                base_e = m["avg_daily"] * 0.34
                if rng.random() < 0.71:
                    after, verdict = base_e * rng.uniform(1.00, 1.10), "recovered"
                else:
                    after, verdict = base_e * rng.uniform(0.88, 0.98), "no_change"
                add(m["id"], "evening_offer",
                    {"discount_rs": rng.choice([5, 10, 15]), "window": "18-21", "days": 3},
                    s, start, 3, base_e * 0.80, after, verdict)
            elif roll < 0.90:
                s = add_situation(m["id"], "demand_surge", start, 22 + rng.uniform(-6, 10),
                                  None, "unknown")
                if rng.random() < 0.66:
                    after, verdict = base * rng.uniform(1.10, 1.28), "sustained"
                else:
                    after, verdict = base * rng.uniform(0.97, 1.05), "no_change"
                add(m["id"], "prep_increase", {"increase_pct": rng.choice([20, 30, 40])},
                    s, start, 7, base, after, verdict)
            else:
                if m["category"] in ("food_stall", "kirana", "mobile_accessories"):
                    s = add_situation(m["id"], "festive_window",
                                      fest_start - timedelta(days=3), 0.0, None, "market_wide")
                    if rng.random() < 0.55:
                        after, verdict = base * rng.uniform(1.95, 2.35), "captured_festive"
                    else:
                        after, verdict = base * rng.uniform(1.05, 1.25), "no_change"
                    add(m["id"], "festive_prestock", {"multiplier": 2.0}, s,
                        fest_start - timedelta(days=3), 8, base, after, verdict)

    con.executemany("INSERT INTO situations VALUES (?,?,?,?,?,?,?,?)", situations)
    con.executemany("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)", actions)
    con.executemany("INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?)", outcomes)

    # ----------------------------------------------- optional tier B data
    obligations, stock, eid, stid = [], [], 0, 0
    for m in merchants:
        if m["has_obligations"]:
            rent = round(m["avg_daily"] * rng.uniform(2.2, 4.0), -2)
            for month_offset in range(-12, 2):
                due = (today.replace(day=1) + timedelta(days=32 * month_offset)).replace(day=5)
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

    # M001's upcoming obligations are pinned so the money story is stable
    eid += 1
    obligations.append((eid, "M001", (today + timedelta(days=4)).isoformat(), 12000.0,
                        "supplier", 0, "integration"))
    eid += 1
    obligations.append((eid, "M001", (today + timedelta(days=9)).isoformat(), 10000.0,
                        "rent", 0, "integration"))
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
    con.close()

    print(f"built {db_path}")
    for k, v in counts.items():
        print(f"  {k:16s} {v:>9,}")
    print(f"  merchants with obligations: {with_ob}  with stock feed: {with_st}")
    print("  (the rest exercise the absence paths on purpose)")


if __name__ == "__main__":
    main()
