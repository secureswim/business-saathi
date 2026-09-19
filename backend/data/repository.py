"""The ONLY module that writes SQL.

Analytics and graph call in here. That keeps the ledger mockable in tests and
stops query fragments spreading through the codebase.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.data import db  # noqa: E402
from backend.models.evidence import MerchantContext  # noqa: E402

OPEN_HOUR, CLOSE_HOUR = 7, 22


# ------------------------------------------------------------------ merchants
def merchant_row(merchant_id: str):
    row = db.q1("SELECT * FROM merchants WHERE id=?", (merchant_id,))
    if row is None:
        raise KeyError(f"unknown merchant {merchant_id}")
    return row


def all_merchants() -> list:
    return db.q("SELECT * FROM merchants ORDER BY id")


def days_of_history(merchant_id: str) -> int:
    row = db.q1("SELECT COUNT(DISTINCT day) c FROM txn_hourly WHERE merchant_id=?",
                (merchant_id,))
    return int(row["c"]) if row else 0


def merchant_context(merchant_id: str) -> MerchantContext:
    r = merchant_row(merchant_id)
    return MerchantContext(
        id=r["id"], name=r["name"], category=r["category"], locality=r["locality"],
        locality_type=r["locality_type"], volume_band=r["volume_band"],
        days_of_history=days_of_history(merchant_id),
        has_obligations=bool(r["has_obligations"]),
        has_stock_feed=bool(r["has_stock_feed"]),
    )


# --------------------------------------------------------------- transactions
def window_totals(merchant_id: str, start: date, end: date,
                  hours: tuple[int, int] | None = None) -> tuple[float, int, int]:
    """(amount, txns, distinct days) over an inclusive day range."""
    sql = ("SELECT COALESCE(SUM(amount),0) a, COALESCE(SUM(txns),0) t, "
           "COUNT(DISTINCT day) d FROM txn_hourly "
           "WHERE merchant_id=? AND day>=? AND day<=?")
    params: list = [merchant_id, start.isoformat(), end.isoformat()]
    if hours:
        sql += " AND hour BETWEEN ? AND ?"
        params += [hours[0], hours[1]]
    r = db.q1(sql, tuple(params))
    return float(r["a"]), int(r["t"]), int(r["d"])


def daily_totals(merchant_id: str, start: date, end: date) -> list[tuple[date, float, int]]:
    rows = db.q("SELECT day, SUM(amount) a, SUM(txns) t FROM txn_hourly "
                "WHERE merchant_id=? AND day>=? AND day<=? GROUP BY day ORDER BY day",
                (merchant_id, start.isoformat(), end.isoformat()))
    return [(date.fromisoformat(r["day"]), float(r["a"]), int(r["t"])) for r in rows]


def hour_grid(merchant_id: str, start: date, end: date) -> list[tuple[date, int, float, int]]:
    rows = db.q("SELECT day, hour, SUM(amount) a, SUM(txns) t FROM txn_hourly "
                "WHERE merchant_id=? AND day>=? AND day<=? GROUP BY day, hour",
                (merchant_id, start.isoformat(), end.isoformat()))
    return [(date.fromisoformat(r["day"]), int(r["hour"]), float(r["a"]), int(r["t"]))
            for r in rows]


def avg_ticket(merchant_id: str, start: date, end: date) -> float | None:
    r = db.q1("SELECT SUM(amount) a, SUM(txns) t FROM txn_hourly "
              "WHERE merchant_id=? AND day>=? AND day<=?",
              (merchant_id, start.isoformat(), end.isoformat()))
    if not r or not r["t"]:
        return None
    return float(r["a"]) / float(r["t"])


def payment_health(merchant_id: str, start: date) -> dict:
    r = db.q1("SELECT COUNT(*) n, SUM(status='failed') f, SUM(status='refunded') rf "
              "FROM payments WHERE merchant_id=? AND ts>=?",
              (merchant_id, start.isoformat()))
    if not r or not r["n"]:
        return {"observed": 0}
    return {"observed": int(r["n"]), "failed": int(r["f"] or 0),
            "refunded": int(r["rf"] or 0)}


def insert_txn_hourly(rows: list[tuple]) -> None:
    db.write_many("INSERT OR REPLACE INTO txn_hourly VALUES (?,?,?,?,?)", rows)


# ---------------------------------------------------------------- situations
def recent_situations(merchant_id: str, days: int = 30) -> list[dict]:
    since = (db.today() - timedelta(days=days)).isoformat()
    rows = db.q("SELECT * FROM situations WHERE merchant_id=? AND detected_on>=? "
                "ORDER BY detected_on DESC", (merchant_id, since))
    return [dict(r) for r in rows]


def latest_situation(merchant_id: str, kind: str | None = None) -> dict | None:
    if kind:
        r = db.q1("SELECT * FROM situations WHERE merchant_id=? AND kind=? "
                  "ORDER BY detected_on DESC LIMIT 1", (merchant_id, kind))
    else:
        r = db.q1("SELECT * FROM situations WHERE merchant_id=? "
                  "ORDER BY detected_on DESC LIMIT 1", (merchant_id,))
    return dict(r) if r else None


def insert_situation(merchant_id: str, kind: str, detected_on: str, severity: float,
                     band: str | None, peer_relative: str, payload: dict) -> int:
    return db.write(
        "INSERT INTO situations (merchant_id, kind, detected_on, severity, band, "
        "peer_relative, payload) VALUES (?,?,?,?,?,?,?)",
        (merchant_id, kind, detected_on, severity, band, peer_relative,
         json.dumps(payload)))


# ------------------------------------------------------------ actions/outcomes
def insert_action(merchant_id: str, atype: str, params: dict, situation_id: int | None,
                  started_on: str, ended_on: str, source: str = "live",
                  run_id: str | None = None) -> int:
    return db.write(
        "INSERT INTO actions (merchant_id, situation_id, type, params, started_on, "
        "ended_on, source, run_id) VALUES (?,?,?,?,?,?,?,?)",
        (merchant_id, situation_id, atype, json.dumps(params), started_on, ended_on,
         source, run_id))


def insert_outcome(action_id: int, metric: str, before: float, after: float,
                   verdict: str, measured_on: str, simulated: bool = True) -> int:
    delta = (after - before) / before * 100.0 if before else 0.0
    return db.write(
        "INSERT INTO outcomes (action_id, metric, before, after, delta_pct, verdict, "
        "measured_on, simulated) VALUES (?,?,?,?,?,?,?,?)",
        (action_id, metric, before, after, round(delta, 2), verdict, measured_on,
         1 if simulated else 0))


def action_history(merchant_id: str, limit: int = 3) -> list[dict]:
    rows = db.q(
        "SELECT a.id, a.type, a.params, a.started_on, a.ended_on, a.source, "
        "o.verdict, o.delta_pct, o.measured_on, o.simulated "
        "FROM actions a LEFT JOIN outcomes o ON o.action_id=a.id "
        "WHERE a.merchant_id=? ORDER BY a.started_on DESC LIMIT ?",
        (merchant_id, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        out.append(d)
    return out


def cohort_experiences(peer_ids: list[str], situation_kind: str | None = None,
                       action_types: list[str] | None = None) -> list[dict]:
    """The action-outcome chains the graph aggregates. Never returns merchant ids."""
    if not peer_ids:
        return []
    marks = ",".join("?" * len(peer_ids))
    sql = ("SELECT a.type, a.params, s.kind AS situation_kind, s.severity, s.band, "
           "o.verdict, o.delta_pct, o.simulated "
           "FROM actions a JOIN outcomes o ON o.action_id=a.id "
           "LEFT JOIN situations s ON s.id=a.situation_id "
           f"WHERE a.merchant_id IN ({marks})")
    params: list = list(peer_ids)
    if situation_kind:
        sql += " AND s.kind = ?"
        params.append(situation_kind)
    if action_types:
        sql += " AND a.type IN (" + ",".join("?" * len(action_types)) + ")"
        params += action_types
    rows = db.q(sql, tuple(params))
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        out.append(d)
    return out


def cohort_merchants_per_action(peer_ids: list[str],
                                situation_kind: str | None = None) -> dict[str, int]:
    """How many DISTINCT merchants tried each action. Counts only, never ids.

    cohort_experiences returns one row per attempt, so counting its rows tells
    you attempts, not merchants -- and one merchant who tried the same play
    three times would otherwise be reported to another merchant as "three
    merchants did this". This is the honest denominator for that sentence.
    """
    if not peer_ids:
        return {}
    marks = ",".join("?" * len(peer_ids))
    sql = ("SELECT a.type AS action_type, COUNT(DISTINCT a.merchant_id) AS merchants "
           "FROM actions a JOIN outcomes o ON o.action_id=a.id "
           "LEFT JOIN situations s ON s.id=a.situation_id "
           f"WHERE a.merchant_id IN ({marks})")
    params: list = list(peer_ids)
    if situation_kind:
        sql += " AND s.kind = ?"
        params.append(situation_kind)
    sql += " GROUP BY a.type"
    return {r["action_type"]: r["merchants"] for r in db.q(sql, tuple(params))}


# ----------------------------------------------------------- learned patterns
def learned_pattern(cohort_key: str, situation_kind: str, action_type: str) -> dict | None:
    r = db.q1("SELECT * FROM learned_patterns WHERE cohort_key=? AND situation_kind=? "
              "AND action_type=?", (cohort_key, situation_kind, action_type))
    return dict(r) if r else None


def upsert_learned_pattern(cohort_key: str, situation_kind: str, action_type: str,
                           tried: int, worked: int, median_delta: float | None) -> None:
    db.write(
        "INSERT INTO learned_patterns (cohort_key, situation_kind, action_type, tried, "
        "worked, median_delta, updated_on) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(cohort_key, situation_kind, action_type) DO UPDATE SET "
        "tried=excluded.tried, worked=excluded.worked, median_delta=excluded.median_delta, "
        "updated_on=excluded.updated_on",
        (cohort_key, situation_kind, action_type, tried, worked, median_delta,
         db.today().isoformat()))


def learned_patterns_for(cohort_key: str) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT * FROM learned_patterns WHERE cohort_key=? ORDER BY tried DESC",
        (cohort_key,))]


# ------------------------------------------------------ optional (tier B) data
def obligations(merchant_id: str, start: date, end: date) -> list[dict]:
    rows = db.q("SELECT due_on, amount, label, source FROM obligations "
                "WHERE merchant_id=? AND due_on>=? AND due_on<=? ORDER BY due_on",
                (merchant_id, start.isoformat(), end.isoformat()))
    return [dict(r) for r in rows]


def has_any_obligations(merchant_id: str) -> bool:
    r = db.q1("SELECT 1 FROM obligations WHERE merchant_id=? LIMIT 1", (merchant_id,))
    return r is not None


def stock_snapshot(merchant_id: str, subject: str | None = None) -> dict | None:
    if subject:
        r = db.q1("SELECT * FROM stock_snapshots WHERE merchant_id=? AND item_label=? "
                  "ORDER BY as_of DESC LIMIT 1", (merchant_id, subject))
        if r:
            return dict(r)
    r = db.q1("SELECT * FROM stock_snapshots WHERE merchant_id=? ORDER BY as_of DESC "
              "LIMIT 1", (merchant_id,))
    return dict(r) if r else None


# ------------------------------------------------------------------- alerts
def insert_alert(merchant_id: str, alert_type: str, severity: float, payload: dict) -> int:
    return db.write(
        "INSERT INTO alerts (merchant_id, alert_type, severity, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        (merchant_id, alert_type, severity, json.dumps(payload),
         datetime.utcnow().isoformat()))


def alerted_recently(merchant_id: str, hours: int) -> bool:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    r = db.q1("SELECT 1 FROM alerts WHERE merchant_id=? AND created_at>=? LIMIT 1",
              (merchant_id, cutoff))
    return r is not None


def recent_alerts(limit: int = 20) -> list[dict]:
    rows = db.q("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


# ------------------------------------------------------------------- events
def record_event(query_id: str | None, type_: str, payload: dict) -> None:
    db.write("INSERT INTO events (query_id, type, payload, created_at) VALUES (?,?,?,?)",
             (query_id, type_, json.dumps(payload, default=str),
              datetime.utcnow().isoformat()))


def events_for(query_id: str) -> list[dict]:
    rows = db.q("SELECT type, payload, created_at FROM events WHERE query_id=? "
                "ORDER BY id", (query_id,))
    return [{"type": r["type"], "payload": json.loads(r["payload"]),
             "created_at": r["created_at"]} for r in rows]
