"""The reference Business Experience Graph, over SQLite.

This is what the demo runs on. It is also the fixture the real Cognee adapter
is checked against: if CogneeGraph returns a different cohort, that is a bug to
find rather than a demo to rescue.

Aggregation happens in Python rather than in the store, because the number that
appears on screen must be exactly reproducible and checkable.
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.data import db, repository as repo  # noqa: E402
from backend.graph import behaviour, privacy, similarity  # noqa: E402
from backend.graph.store import GraphStore  # noqa: E402

GOOD = {"recovered", "sustained", "captured_festive"}
BAD = {"temporary_spike", "worse", "minor_loss", "no_change"}

ACTION_FAMILIES = {
    "discount": ["deep_discount", "moderate_discount"],
    "offer": ["evening_offer"],
    "price": ["price_increase"],
    "prep": ["prep_increase", "festive_prestock"],
}


def bucket_label(action_type: str, params: dict) -> str:
    if "discount_pct" in params:
        p = params["discount_pct"]
        return "25%+" if p >= 25 else ("16-24%" if p >= 16 else "10-15%")
    if "discount_rs" in params:
        r = params["discount_rs"]
        return "Rs15+" if r >= 15 else ("Rs10" if r >= 10 else "Rs5")
    if "increase_rs" in params:
        return "Rs10+" if params["increase_rs"] >= 10 else "Rs5-9"
    if "increase_pct" in params:
        return f"{params['increase_pct']}%"
    return "all"


def _modal_params(param_list: list[dict]) -> dict:
    out: dict = {}
    for key in {k for p in param_list for k in p}:
        vals = [p[key] for p in param_list if key in p]
        if not vals:
            continue
        if all(isinstance(v, (int, float)) for v in vals):
            out[key] = round(statistics.median(vals), 1)
        else:
            out[key] = statistics.mode(vals)
    return out


def _contiguous(hours: list[int]) -> list[str]:
    if not hours:
        return []
    hours = sorted(hours)
    out, start, prev = [], hours[0], hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
            continue
        out.append(f"{start}-{prev + 1}")
        start = prev = h
    out.append(f"{start}-{prev + 1}")
    return out


class SqliteGraph(GraphStore):
    name = "sqlite"

    # -------------------------------------------------------------- cohort
    def peers(self, merchant_id: str) -> dict:
        t0 = time.time()
        me = repo.merchant_row(merchant_id)
        candidates = [r for r in repo.all_merchants()
                      if r["id"] != merchant_id and r["avg_daily"] > 0]
        # one query for every behavioural profile, then score in memory
        profiles = behaviour.load_many([merchant_id] + [r["id"] for r in candidates])
        mine = profiles.get(merchant_id, {})

        tight, extended = [], []
        for r in candidates:
            s = similarity.score(me, r, mine, profiles.get(r["id"]))
            if s["total"] < config.EXTENDED_THRESHOLD:
                continue
            entry = {"id": r["id"], "category": r["category"], "locality": r["locality"],
                     "volume_band": r["volume_band"], "similarity": s["total"],
                     "components": s["components"],
                     "behavioural": s["behavioural_total"],
                     "declared": s["declared_total"]}
            (tight if s["total"] >= config.SIMILARITY_THRESHOLD else extended).append(entry)

        tight.sort(key=lambda x: -x["similarity"])
        extended.sort(key=lambda x: -x["similarity"])
        tight = tight[:config.COHORT_CAP]
        extended = extended[:config.COHORT_CAP]

        # keyed on measured behaviour, not on the onboarding label: see
        # backend/graph/behaviour.cohort_key
        cohort_key = behaviour.cohort_key(merchant_id, me["locality_type"], mine)
        return {
            "value": {
                "merchant": {"id": me["id"], "category": me["category"],
                             "locality": me["locality"],
                             "locality_type": me["locality_type"],
                             "volume_band": me["volume_band"]},
                "cohort_key": cohort_key,
                "cohort_size": len(tight),
                "extended_size": len(extended),
                "peer_ids": [p["id"] for p in tight],
                "extended_ids": [p["id"] for p in extended],
                "sufficient": len(tight) >= config.MIN_COHORT_SIZE,
            },
            "basis": {
                "source": "graph.peers", "store": self.name,
                "threshold": config.SIMILARITY_THRESHOLD,
                "extended_threshold": config.EXTENDED_THRESHOLD,
                "weights": config.SIMILARITY_WEIGHTS,
                "min_cohort_size": config.MIN_COHORT_SIZE,
                "retrieval_ms": int((time.time() - t0) * 1000),
                "peers": tight, "extended": extended,
            },
        }

    # ------------------------------------------------------- what worked
    def peer_playbook(self, situation_kind: str, peer_ids: list[str]) -> dict:
        t0 = time.time()
        try:
            privacy.check_cohort(peer_ids)
        except privacy.CohortTooSmall:
            return privacy.unavailable_result("graph.peer_playbook", peer_ids)

        rows = repo.cohort_experiences(peer_ids, situation_kind=situation_kind)
        by_type: dict[str, dict] = {}
        for r in rows:
            t = by_type.setdefault(r["type"], {"tried": 0, "worked": 0,
                                               "deltas": [], "params": []})
            t["tried"] += 1
            t["params"].append(r["params"])
            if r["verdict"] in GOOD:
                t["worked"] += 1
                t["deltas"].append(float(r["delta_pct"]))

        # attempts != merchants. One merchant who tried the same play three
        # times is three rows above, and reporting that as "three merchants"
        # would be a plain factual error in what the merchant is told.
        merchants = repo.cohort_merchants_per_action(peer_ids, situation_kind)

        options = []
        for atype, t in by_type.items():
            options.append({
                "action": atype, "tried": t["tried"], "worked": t["worked"],
                "merchants": merchants.get(atype, 0),
                "success_rate": round(t["worked"] / t["tried"] * 100, 1),
                "median_delta": round(statistics.median(t["deltas"]), 1) if t["deltas"] else None,
                "common_params": _modal_params(t["params"]),
                # strong enough to quote as a pattern, or just context?
                "sufficient": t["tried"] >= config.MIN_ATTEMPTS_TO_RECOMMEND,
            })
        options.sort(key=lambda o: (-o["worked"], -o["success_rate"]))

        # `best` is what gets recommended and spoken. Only options that clear
        # the evidence floor are eligible; thin ones stay in `options` so /ops
        # still shows everything the cohort has tried.
        eligible = [o for o in options if o["sufficient"]]
        thin = len(options) - len(eligible)

        return {
            "value": {"situation_kind": situation_kind, "cohort_size": len(peer_ids),
                      "options": options,
                      "best": eligible[0] if eligible else None,
                      "thin_options_excluded": thin,
                      "min_attempts": config.MIN_ATTEMPTS_TO_RECOMMEND},
            "basis": {"source": "graph.peer_playbook", "store": self.name,
                      "situation_kind": situation_kind, "peer_count": len(peer_ids),
                      "experiences_matched": len(rows),
                      "min_cohort_size": config.MIN_COHORT_SIZE,
                      "min_attempts_to_recommend": config.MIN_ATTEMPTS_TO_RECOMMEND,
                      "thin_options_excluded": thin,
                      "retrieval_ms": int((time.time() - t0) * 1000)},
        }

    # ------------------------------------------------------- what failed
    def failed_plays(self, action_family: str, peer_ids: list[str]) -> dict:
        t0 = time.time()
        try:
            privacy.check_cohort(peer_ids)
        except privacy.CohortTooSmall:
            return privacy.unavailable_result("graph.failed_plays", peer_ids)

        types = ACTION_FAMILIES.get(action_family, [action_family])
        rows = repo.cohort_experiences(peer_ids, action_types=types)
        if not rows:
            return {"value": {"action_family": action_family, "evidence": False,
                              "reason": "no matching actions in this cohort"},
                    "basis": {"source": "graph.failed_plays", "store": self.name,
                              "types": types, "peer_count": len(peer_ids)}}

        buckets: dict[str, dict] = {}
        for r in rows:
            key = bucket_label(r["type"], r["params"])
            b = buckets.setdefault(key, {"tried": 0, "failed": 0, "deltas": []})
            b["tried"] += 1
            b["deltas"].append(float(r["delta_pct"]))
            if r["verdict"] in BAD:
                b["failed"] += 1

        bucket_list = [{"range": k, "tried": v["tried"], "failed": v["failed"],
                        "failure_rate": round(v["failed"] / v["tried"] * 100, 1),
                        "median_delta": round(statistics.median(v["deltas"]), 1)}
                       for k, v in sorted(buckets.items())]
        safest = min(bucket_list, key=lambda b: b["failure_rate"])
        failed_total = sum(b["failed"] for b in bucket_list)

        return {
            "value": {"action_family": action_family, "evidence": True,
                      "tried": len(rows), "failed": failed_total,
                      "failure_rate": round(failed_total / len(rows) * 100, 1),
                      "buckets": bucket_list, "safest_bucket": safest},
            "basis": {"source": "graph.failed_plays", "store": self.name,
                      "types": types, "peer_count": len(peer_ids),
                      "experiences_matched": len(rows),
                      "retrieval_ms": int((time.time() - t0) * 1000)},
        }

    # ------------------------------------------------------ local pattern
    def local_pattern(self, category: str, locality: str) -> dict:
        rows = db.q("SELECT id FROM merchants WHERE category=? AND locality=? "
                    "AND avg_daily>0", (category, locality))
        ids = [r["id"] for r in rows]
        try:
            privacy.check_cohort(ids)
        except privacy.CohortTooSmall:
            return privacy.unavailable_result("graph.local_pattern", ids)

        today = db.today()
        cur_start = today - timedelta(days=7)
        base_start = cur_start - timedelta(days=30)
        marks = ",".join("?" * len(ids))
        cur = db.q1(f"SELECT SUM(amount) a, COUNT(DISTINCT day) d FROM txn_hourly "
                    f"WHERE merchant_id IN ({marks}) AND day>=? AND day<?",
                    (*ids, cur_start.isoformat(), today.isoformat()))
        base = db.q1(f"SELECT SUM(amount) a, COUNT(DISTINCT day) d FROM txn_hourly "
                     f"WHERE merchant_id IN ({marks}) AND day>=? AND day<?",
                     (*ids, base_start.isoformat(), cur_start.isoformat()))
        cur_d = (float(cur["a"] or 0) / (cur["d"] or 1))
        base_d = (float(base["a"] or 0) / (base["d"] or 1))
        change = (cur_d - base_d) / base_d * 100.0 if base_d else 0.0

        hours = db.q(f"SELECT hour, SUM(amount) a FROM txn_hourly "
                     f"WHERE merchant_id IN ({marks}) GROUP BY hour ORDER BY a DESC",
                     tuple(ids))
        top = [int(h["hour"]) for h in hours[:4]]

        return {
            "value": {"category": category, "locality": locality,
                      "cohort_size": len(ids),
                      "cohort_change_pct": round(change, 1),
                      "direction": "down" if change < -2 else ("up" if change > 2 else "flat"),
                      "busiest_bands": _contiguous(top)},
            "basis": {"source": "graph.local_pattern", "store": self.name,
                      "merchants_aggregated": len(ids),
                      "windows": [[cur_start.isoformat(), today.isoformat()],
                                  [base_start.isoformat(), cur_start.isoformat()]],
                      "note": "aggregate only; no individual merchant figures"},
        }

    # ------------------------------------------------------- cold start
    def cohort_profile(self, category: str, locality: str) -> dict:
        rows = db.q("SELECT * FROM merchants WHERE category=? AND locality=? "
                    "AND avg_daily>0", (category, locality))
        ids = [r["id"] for r in rows]
        try:
            privacy.check_cohort(ids)
        except privacy.CohortTooSmall:
            return privacy.unavailable_result("graph.cohort_profile", ids)

        marks = ",".join("?" * len(ids))
        hours = db.q(f"SELECT hour, SUM(amount) a FROM txn_hourly "
                     f"WHERE merchant_id IN ({marks}) GROUP BY hour ORDER BY a DESC",
                     tuple(ids))
        total = sum(float(h["a"]) for h in hours) or 1.0
        top = [{"hour": int(h["hour"]), "share_pct": round(float(h["a"]) / total * 100, 1)}
               for h in hours[:4]]
        top.sort(key=lambda x: x["hour"])

        dailies = sorted(float(r["avg_daily"]) for r in rows)
        lo = dailies[int(len(dailies) * 0.25)]
        hi = dailies[int(len(dailies) * 0.75)]

        worked = self.peer_playbook("evening_decline", ids)["value"]
        failed = self.failed_plays("discount", ids)["value"]

        return {
            "value": {"category": category, "locality": locality,
                      "cohort_size": len(ids),
                      "peak_bands": _contiguous([h["hour"] for h in top]),
                      "peak_hours": top,
                      "expected_daily_low": round(lo, -1),
                      "expected_daily_high": round(hi, -1),
                      "proven_action": worked.get("best"),
                      "common_mistake": ({"action_family": "discount",
                                          "worst_bucket": max(failed["buckets"],
                                                              key=lambda b: b["failure_rate"])}
                                         if failed.get("evidence") else None)},
            "basis": {"source": "graph.cohort_profile", "store": self.name,
                      "merchants_aggregated": len(ids),
                      "note": "aggregate only; the new merchant has no history of their own"},
        }

    # ------------------------------------------------------ seasonality
    def cohort_seasonality(self, category: str, locality_type: str,
                           horizon: int = 7) -> dict:
        rows = db.q("SELECT id FROM merchants WHERE category=? AND locality_type=? "
                    "AND avg_daily>0", (category, locality_type))
        ids = [r["id"] for r in rows]
        try:
            privacy.check_cohort(ids)
        except privacy.CohortTooSmall:
            return privacy.unavailable_result("graph.cohort_seasonality", ids)

        today = db.today()
        marks = ",".join("?" * len(ids))
        rows2 = db.q(f"SELECT day, SUM(amount) a FROM txn_hourly "
                     f"WHERE merchant_id IN ({marks}) GROUP BY day", tuple(ids))
        from datetime import date as _date
        by_wd: dict[int, list[float]] = {}
        for r in rows2:
            by_wd.setdefault(_date.fromisoformat(r["day"]).weekday(), []).append(float(r["a"]))
        overall = statistics.fmean([v for vs in by_wd.values() for v in vs]) if by_wd else 0.0
        multipliers = {w: round(statistics.fmean(v) / overall, 2)
                       for w, v in by_wd.items() if overall}

        fest = db.q1("SELECT value FROM meta WHERE key='festive_start'")
        fest_near = False
        if fest:
            from datetime import date as _d
            fs = _d.fromisoformat(fest["value"])
            fest_near = 0 <= (fs - today).days <= horizon

        return {
            "value": {"category": category, "locality_type": locality_type,
                      "cohort_size": len(ids),
                      "weekday_multipliers": multipliers,
                      "festive_window_near": fest_near},
            "basis": {"source": "graph.cohort_seasonality", "store": self.name,
                      "merchants_aggregated": len(ids),
                      "method": "cohort mean revenue per weekday over all history"},
        }

    # ------------------------------------------------------- write-back
    def record_action(self, merchant_id, atype, params, situation_id, started_on,
                      ended_on, run_id) -> int:
        return repo.insert_action(merchant_id, atype, params, situation_id,
                                  started_on, ended_on, source="live", run_id=run_id)

    def record_outcome(self, action_id, metric, before, after, verdict, measured_on,
                       cohort_key, situation_kind, action_type) -> dict:
        repo.insert_outcome(action_id, metric, before, after, verdict, measured_on,
                            simulated=True)
        pattern = self.refresh_learned_pattern(cohort_key, situation_kind, action_type)
        # Report what was WRITTEN, not a node/edge count nobody measured.
        # This is a relational write: one outcome row, one recomputed aggregate.
        return {"store": self.name, "outcome_rows": 1, "pattern_rows": 1,
                "cards_submitted": 0,
                "detail": "1 outcome row, 1 cohort pattern recomputed",
                "cohort_key": cohort_key, "degraded": False, "pattern": pattern}

    def refresh_learned_pattern(self, cohort_key: str, situation_kind: str,
                                action_type: str) -> dict:
        """Recompute the aggregate from the ledger, so it is always exact."""
        ids = behaviour.members_of(cohort_key)
        exps = [e for e in repo.cohort_experiences(ids, situation_kind=situation_kind)
                if e["type"] == action_type]
        tried = len(exps)
        worked = sum(1 for e in exps if e["verdict"] in GOOD)
        deltas = [float(e["delta_pct"]) for e in exps if e["verdict"] in GOOD]
        median = round(statistics.median(deltas), 1) if deltas else None
        repo.upsert_learned_pattern(cohort_key, situation_kind, action_type,
                                    tried, worked, median)
        return {"cohort_key": cohort_key, "situation_kind": situation_kind,
                "action_type": action_type, "tried": tried, "worked": worked,
                "success_rate": round(worked / tried * 100, 1) if tried else None,
                "median_delta": median}
