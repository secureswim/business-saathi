"""Connection management. Thin on purpose -- all SQL lives in repository.py.

Connections are per-thread AND generation-stamped. `reset_connection()` bumps
the generation and closes every open handle, so after the presenter's reset
button replaces the database file, a worker thread that still holds a handle to
the old inode reconnects instead of raising "disk I/O error" mid-demo.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402

_local = threading.local()
_lock = threading.RLock()
_generation = 0
_open: set[sqlite3.Connection] = set()


def connect() -> sqlite3.Connection:
    global _generation
    con = getattr(_local, "con", None)
    gen = getattr(_local, "gen", -1)
    if con is not None and gen == _generation:
        return con
    if con is not None:                       # stale: the file was replaced
        try:
            con.close()
        except Exception:      # noqa: BLE001
            pass
        with _lock:
            _open.discard(con)

    if not config.DB_PATH.exists():
        raise FileNotFoundError(
            f"No database at {config.DB_PATH}. Run: python scripts/generate.py")
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    _local.con = con
    _local.gen = _generation
    with _lock:
        _open.add(con)
    return con


def reset_connection() -> None:
    """Close every handle and invalidate the rest. Call before replacing the file."""
    global _generation
    with _lock:
        _generation += 1
        for con in list(_open):
            try:
                con.close()
            except Exception:      # noqa: BLE001
                pass
        _open.clear()
    _local.con = None
    _local.gen = -1


def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def q1(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def write(sql: str, params: tuple = ()) -> int:
    with _lock:
        con = connect()
        cur = con.execute(sql, params)
        con.commit()
        return int(cur.lastrowid or 0)


def write_many(sql: str, rows: list[tuple]) -> None:
    with _lock:
        con = connect()
        con.executemany(sql, rows)
        con.commit()


def today() -> date:
    row = q1("SELECT value FROM meta WHERE key='today'")
    if row is None:
        return date.today()
    return date.fromisoformat(row["value"])
