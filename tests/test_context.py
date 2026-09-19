"""Conversational facts: parse, TTL, supersede, attribution."""
from datetime import datetime, timedelta

import config
from backend.data import context, db


def test_parses_a_hinglish_range():
    n, unit = context.parse_quantity("bees pachees bottle")
    assert n == 22.5 and unit == "bottle"


def test_parses_digits_and_units():
    n, unit = context.parse_quantity("20-25 pieces")
    assert n == 22.5 and unit == "pieces"


def test_store_and_read_within_ttl():
    context.store(config.DEMO_MERCHANT, "stock_estimate", "20-25 bottles",
                  subject="cold drink")
    got = context.get(config.DEMO_MERCHANT, "stock_estimate", "cold drink")
    assert got and got["value_num"] == 22.5
    assert got["utterance"] == "20-25 bottles"          # kept for attribution


def test_a_new_statement_supersedes_the_old_one():
    context.store(config.DEMO_MERCHANT, "stock_estimate", "40 bottles", subject="cold drink")
    got = context.get(config.DEMO_MERCHANT, "stock_estimate", "cold drink")
    assert got["value_num"] == 40.0


def test_an_expired_fact_is_never_returned():
    context.store(config.DEMO_MERCHANT, "stock_estimate", "99 bottles", subject="expired item")
    db.write("UPDATE merchant_inputs SET expires_at=? WHERE subject='expired item'",
             ((datetime.utcnow() - timedelta(hours=1)).isoformat(),))
    assert context.get(config.DEMO_MERCHANT, "stock_estimate", "expired item") is None
