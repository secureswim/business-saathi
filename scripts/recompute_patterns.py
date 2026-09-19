"""Derive and cache what does not change between two questions a minute apart.

A merchant's 30-day baseline is stable; recomputing it live is what breaks the
latency budget. Run after generate.py and after any simulated day.

Usage:  python scripts/recompute_patterns.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from backend.analytics import anomaly, patterns, trend  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402
from backend.graph import behaviour  # noqa: E402
from backend.graph.sqlite_store import GOOD, SqliteGraph  # noqa: E402


def main() -> None:
    for warning in config.config_warnings():
        print(f"  !! {warning}\n")
    store = SqliteGraph()
    today = db.today().isoformat()
    merchants = [r for r in repo.all_merchants() if r["avg_daily"] > 0]

    # ---------------------------------------------------- merchant_patterns
    rows = []
    for m in merchants:
        mid = m["id"]
        try:
            rows.append((mid, "baseline_30d", today,
                         json.dumps(trend.sales_trend(mid)["value"])))
            tp = patterns.time_patterns(mid)["value"]
            if tp.get("available"):
                rows.append((mid, "hourly", today, json.dumps(tp)))
            vol = trend.volatility(mid)["value"]
            rows.append((mid, "volatility", today, json.dumps(vol)))
            # measured behaviour: the basis of the cohort, cached because
            # scoring 160 merchants live would cost the latency budget
            rows.append((mid, "behaviour", today,
                         json.dumps(behaviour.compute(mid))))
        except Exception:      # noqa: BLE001
            continue
    db.write_many("INSERT OR REPLACE INTO merchant_patterns VALUES (?,?,?,?)", rows)
    print(f"  merchant_patterns   {len(rows):>6,}")

    # ----------------------------------------------------------- situations
    detected = 0
    for m in merchants:
        try:
            peers = store.peers(m["id"])["value"]["peer_ids"]
            r = anomaly.detect_situation(m["id"], peers, persist=True)["value"]
            if r["situation_id"]:
                detected += 1
        except Exception:      # noqa: BLE001
            continue
    print(f"  situations detected {detected:>6,}")

    # ------------------------------------------------------ learned_patterns
    combos: dict[tuple[str, str, str], list] = {}
    for m in merchants:
        key = behaviour.cohort_key(m["id"], m["locality_type"])
        for exp in repo.cohort_experiences([m["id"]]):
            if not exp.get("situation_kind"):
                continue
            combos.setdefault((key, exp["situation_kind"], exp["type"]), []).append(exp)

    for (key, kind, atype), exps in combos.items():
        tried = len(exps)
        worked = sum(1 for e in exps if e["verdict"] in GOOD)
        deltas = [float(e["delta_pct"]) for e in exps if e["verdict"] in GOOD]
        repo.upsert_learned_pattern(key, kind, atype, tried, worked,
                                    round(statistics.median(deltas), 1) if deltas else None)
    print(f"  learned_patterns    {len(combos):>6,}")

    # ------------------------------------------------- cohort sanity checks
    # The cohort is now MEASURED, so it can only be checked once behaviour has
    # been computed -- which is here, not in the generator.
    problems = []
    for mid in (config.DEMO_MERCHANT, config.PEER_MERCHANT):
        v = store.peers(mid)["value"]
        print(f"  cohort for {mid:<6s}      {v['cohort_size']:>6,}  "
              f"(key {v['cohort_key']})")
        if v["cohort_size"] < config.MIN_COHORT_SIZE:
            problems.append(f"{mid} has {v['cohort_size']} peers, below the "
                            f"privacy floor of {config.MIN_COHORT_SIZE}")

    # pattern 9: the cafes share the demo merchant's LABEL and must not be in
    # its cohort. If they are, the similarity engine is back to trusting the
    # onboarding form and every peer claim on stage is overstated.
    cafes = {r["id"] for r in repo.all_merchants() if r["name"].startswith("Cafe")}
    demo_peers = set(store.peers(config.DEMO_MERCHANT)["value"]["peer_ids"])
    leaked = cafes & demo_peers
    print(f"  cafes in M001 cohort     {len(leaked):>6,}  (want 0)")
    if leaked:
        problems.append(f"cafes leaked into the demo merchant's cohort: "
                        f"{sorted(leaked)}. Same label, different trade -- "
                        f"behaviour should have excluded them.")
    if problems:
        raise SystemExit("  !! " + "\n  !! ".join(problems))


if __name__ == "__main__":
    main()
