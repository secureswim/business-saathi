"""Recompute every number the demo says out loud, straight from the database.

Run after any generator change, in CI, and before every rehearsal. If a figure
here moves, the NARRATIVE moves with it -- never the other way around.

Usage:  python scripts/verify_claims.py    (exit code 1 if any claim fails)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from backend.analytics import anomaly, money, patterns, trend  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402
from backend.graph.sqlite_store import SqliteGraph  # noqa: E402

OK, BAD = "  ok  ", " FAIL "


def main() -> int:
    g = SqliteGraph()
    m = config.DEMO_MERCHANT
    failures = 0

    def check(label: str, value, passed: bool):
        nonlocal failures
        if not passed:
            failures += 1
        print(f"{OK if passed else BAD} {label:<46} {value}")

    print(f"\ndatabase : {config.DB_PATH}")
    print(f"today    : {db.today()}    merchants: "
          f"{db.q1('SELECT COUNT(*) c FROM merchants')['c']}\n")

    # ---------------------------------------------------------- demo merchant
    t = trend.sales_trend(m)["value"]
    check("demo merchant decline", f"{t['change_pct']}%", -25 < t["change_pct"] < -10)
    check("decline concentrated in an evening band", t["worst_band"],
          t["worst_band"] in ("16-19", "19-22"))
    check("current daily average", f"Rs {t['current_daily']:,.0f}", t["current_daily"] > 0)
    check("baseline daily average", f"Rs {t['baseline_daily']:,.0f}", t["baseline_daily"] > 0)

    h = trend.business_health(m)["value"]
    check("business health headline", h["headline"], h["headline"] == "needs_attention")

    # ------------------------------------------------------------ the cohort
    p = g.peers(m)["value"]
    check("tight cohort size", p["cohort_size"], p["cohort_size"] >= config.MIN_COHORT_SIZE)
    check("extended ring size", p["extended_size"], p["extended_size"] > 0)
    check("cohort key", p["cohort_key"], "|" in p["cohort_key"])

    a = anomaly.peer_relative_anomaly(m, p["peer_ids"])["value"]
    check("peer-relative verdict", a.get("verdict"), a.get("verdict") == "specific_to_merchant")
    check("gap vs peer median", f"{a.get('gap_pct')} pp", (a.get("gap_pct") or 0) < -5)

    # -------------------------------------------------------- the playbook
    w = g.peer_playbook("evening_decline", p["peer_ids"])["value"]
    best = w["best"]
    check("proven play for an evening decline", best and best["action"],
          bool(best) and best["action"] == "evening_offer")
    check("peers who recovered",
          f"{best['worked']} of {best['tried']} ({best['success_rate']}%)",
          best["success_rate"] >= 70)
    check("median change after the offer", f"{best['median_delta']}%",
          10 < best["median_delta"] < 60)

    f = g.failed_plays("discount", p["peer_ids"] + p["extended_ids"])["value"]
    deep = next((b for b in f["buckets"] if b["range"] == "25%+"), None)
    mod = next((b for b in f["buckets"] if b["range"] == "10-15%"), None)
    check("deep discount failure rate", deep and f"{deep['failure_rate']}%",
          bool(deep) and deep["failure_rate"] > 60)
    check("moderate discount failure rate", mod and f"{mod['failure_rate']}%",
          bool(mod) and mod["failure_rate"] < 60)
    check("safest discount bucket", f["safest_bucket"]["range"],
          f["safest_bucket"]["range"] == "10-15%")

    # ------------------------------------------------------------- forecasts
    d = patterns.demand_forecast(m)["value"]
    check("7-day forecast is a band",
          f"Rs {d['total_low']:,.0f}-{d['total_high']:,.0f}",
          d["total_high"] > d["total_expected"] > d["total_low"])
    r = patterns.rush_forecast(m)["value"]
    check("next rush", f"{r['weekday']} {r['hour']}:00 at {r['multiple']}x",
          r["multiple"] > 1)

    # ----------------------------------------------------------------- money
    cv = money.cash_view(m)["value"]
    check("demo merchant money scope", cv["scope"], cv["scope"] == "net_position")
    no_ob = db.q1("SELECT id FROM merchants WHERE has_obligations=0 AND avg_daily>0 "
                  "LIMIT 1")["id"]
    cv2 = money.cash_view(no_ob)["value"]
    check(f"merchant without obligations ({no_ob})", cv2["scope"],
          cv2["scope"] == "inflow_only")

    # ------------------------------------------------------------ cold start
    cs = g.cohort_profile("food_stall", "Sector 62")["value"]
    check("cold-start peak bands", cs["peak_bands"], len(cs["peak_bands"]) > 0)
    check("cold-start revenue range",
          f"Rs {cs['expected_daily_low']:,.0f}-{cs['expected_daily_high']:,.0f}",
          cs["expected_daily_high"] > cs["expected_daily_low"] > 0)

    # --------------------------------------------------- seeded market event
    cohort = db.q1("SELECT value FROM meta WHERE key='market_event_cohort'")["value"]
    cat, ltype = cohort.split("|")
    ids = [x["id"] for x in db.q("SELECT id FROM merchants WHERE category=? "
                                 "AND locality_type=?", (cat, ltype))]
    start = db.q1("SELECT value FROM meta WHERE key='market_event_start'")["value"]
    end = db.q1("SELECT value FROM meta WHERE key='market_event_end'")["value"]
    marks = ",".join("?" * len(ids))
    ev = db.q1(f"SELECT AVG(amount) a FROM txn_hourly WHERE merchant_id IN ({marks}) "
               f"AND day>=? AND day<=?", (*ids, start, end))["a"]
    norm = db.q1(f"SELECT AVG(amount) a FROM txn_hourly WHERE merchant_id IN ({marks})",
                 tuple(ids))["a"]
    check("seeded market-wide dip", f"{ev / norm:.2f}x normal", ev / norm < 0.92)

    # -------------------------------------------------------- festive window
    fs = db.q1("SELECT value FROM meta WHERE key='festive_start'")["value"]
    fest = db.q1("SELECT AVG(amount) a FROM txn_hourly WHERE day=? AND hour BETWEEN 18 AND 21 "
                 "AND merchant_id IN (SELECT id FROM merchants WHERE category='food_stall')",
                 (fs,))["a"]
    base = db.q1("SELECT AVG(amount) a FROM txn_hourly WHERE hour BETWEEN 18 AND 21 "
                 "AND merchant_id IN (SELECT id FROM merchants WHERE category='food_stall')")["a"]
    check("festive evening multiple", f"{fest / base:.1f}x", fest / base > 2.0)

    # --------------------------------------------- optional data proportions
    total = db.q1("SELECT COUNT(*) c FROM merchants WHERE avg_daily>0")["c"]
    ob = db.q1("SELECT COUNT(*) c FROM merchants WHERE has_obligations=1")["c"]
    st = db.q1("SELECT COUNT(*) c FROM merchants WHERE has_stock_feed=1")["c"]
    check("merchants WITH obligations (tier B)", f"{ob}/{total}", 0.25 < ob / total < 0.55)
    check("merchants WITH a stock feed (tier B)", f"{st}/{total}", 0.15 < st / total < 0.45)
    check("absence paths are exercised", f"{total - ob} / {total - st} without",
          (total - ob) > 0 and (total - st) > 0)

    # ------------------------------------------------------------- cold start
    cold = config.COLDSTART_MERCHANT
    check("cold-start merchant has no history", repo.days_of_history(cold),
          repo.days_of_history(cold) == 0)

    print(f"\n{'all claims reproduce' if not failures else str(failures) + ' CLAIM(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
