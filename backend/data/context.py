"""Conversational facts (tier C): what the merchant tells Saathi by voice.

This is the mechanism that keeps Saathi from becoming a data-entry app. The
merchant supplies one number, in speech, for one decision, and is never asked
to maintain it.

Rules enforced here:
  * TTL is checked in the QUERY, not by a cleanup job, so an expired fact
    cannot be read even if no job ran.
  * A new statement about the same subject supersedes the old row.
  * Nothing here is ever aggregated into the graph -- see docs/DESIGN.md
    "Privacy and safety".
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.data import db  # noqa: E402

NUM_WORDS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10, "dus": 10,
    "bees": 20, "bis": 20, "pachees": 25, "pacchis": 25, "tees": 30, "tis": 30,
    "chalees": 40, "pachas": 50, "pachaas": 50, "sau": 100,
}


def parse_quantity(utterance: str) -> tuple[float | None, str | None]:
    """'bees pachees bottle' -> (22.5, 'bottle'); '20-25 pieces' -> (22.5, 'pieces').

    Ranges are averaged, which is honest: the merchant gave a range and the
    answer should not pretend to more precision than that.
    """
    text = utterance.lower().strip()
    unit = None
    m = re.search(r"\b(bottle|bottles|piece|pieces|packet|packets|kg|litre|liters|"
                  r"litres|box|boxes|plate|plates|cup|cups|unit|units)\b", text)
    if m:
        unit = m.group(1)

    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        words = re.findall(r"[a-z]+", text)
        nums = [float(NUM_WORDS[w]) for w in words if w in NUM_WORDS]
    if not nums:
        return None, unit
    if len(nums) >= 2:
        lo, hi = sorted(nums[:2])
        if hi <= lo * 4:                       # looks like a range, not two facts
            return round((lo + hi) / 2, 2), unit
    return nums[0], unit


def store(merchant_id: str, kind: str, utterance: str, subject: str | None = None,
          value_num: float | None = None, value_text: str | None = None,
          unit: str | None = None, confidence: float = 0.8) -> dict:
    if value_num is None and kind in ("stock_estimate", "capacity", "upcoming_expense"):
        value_num, parsed_unit = parse_quantity(utterance)
        unit = unit or parsed_unit

    now = datetime.utcnow()
    ttl = config.INPUT_TTL_HOURS.get(kind, 12)
    expires = now + timedelta(hours=ttl)

    # a new statement about the same subject supersedes the old row
    db.write("DELETE FROM merchant_inputs WHERE merchant_id=? AND kind=? "
             "AND IFNULL(subject,'')=IFNULL(?,'')", (merchant_id, kind, subject))
    row_id = db.write(
        "INSERT INTO merchant_inputs (merchant_id, kind, subject, value_num, value_text, "
        "unit, stated_at, expires_at, utterance, confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (merchant_id, kind, subject, value_num, value_text, unit,
         now.isoformat(), expires.isoformat(), utterance, confidence))
    return {"id": row_id, "kind": kind, "subject": subject, "value_num": value_num,
            "unit": unit, "utterance": utterance, "stated_at": now.isoformat(),
            "expires_at": expires.isoformat(), "confidence": confidence}


def get(merchant_id: str, kind: str, subject: str | None = None) -> dict | None:
    """Unexpired facts only. An expired fact is dropped silently, never used stale."""
    now = datetime.utcnow().isoformat()
    if subject:
        r = db.q1("SELECT * FROM merchant_inputs WHERE merchant_id=? AND kind=? "
                  "AND subject=? AND expires_at>? ORDER BY stated_at DESC LIMIT 1",
                  (merchant_id, kind, subject, now))
        return dict(r) if r else None
    r = db.q1("SELECT * FROM merchant_inputs WHERE merchant_id=? AND kind=? "
              "AND expires_at>? ORDER BY stated_at DESC LIMIT 1",
              (merchant_id, kind, now))
    return dict(r) if r else None


def live_for(merchant_id: str) -> list[dict]:
    now = datetime.utcnow().isoformat()
    return [dict(r) for r in db.q(
        "SELECT * FROM merchant_inputs WHERE merchant_id=? AND expires_at>? "
        "ORDER BY stated_at DESC", (merchant_id, now))]
