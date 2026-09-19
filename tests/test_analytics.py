"""Deterministic analytics against the generated ledger."""
import config
from backend.analytics import anomaly, money, patterns, trend
from backend.data import db, repository as repo

M = config.DEMO_MERCHANT


def test_trend_returns_value_and_basis():
    r = trend.sales_trend(M)
    assert set(r) == {"value", "basis"}
    assert "current_window" in r["basis"] and "baseline_window" in r["basis"]
    assert r["basis"]["days_observed"][0] > 0


def test_demo_merchant_is_declining_in_an_evening_band():
    v = trend.sales_trend(M)["value"]
    assert v["direction"] == "down"
    assert v["change_pct"] < -10
    assert v["worst_band"] in ("16-19", "19-22")


def test_closed_days_are_excluded_not_counted_as_zero():
    b = trend.sales_trend(M)["basis"]
    cur_days, base_days = b["days_observed"]
    assert cur_days <= 7 and base_days <= 30
    assert "closed days excluded" in b["method"]


def test_forecast_is_a_band_never_a_point():
    v = patterns.demand_forecast(M)["value"]
    assert v["total_low"] < v["total_expected"] < v["total_high"]
    for day in v["per_day"]:
        assert day["low"] <= day["expected"] <= day["high"]


def test_rush_forecast_is_in_the_future():
    v = patterns.rush_forecast(M)["value"]
    assert v["available"]
    assert v["day"] > db.today().isoformat()
    assert 7 <= v["hour"] <= 22


def test_time_patterns_have_peaks():
    v = patterns.time_patterns(M)["value"]
    assert v["available"] and len(v["peak_bands"]) >= 1


def test_volatility_is_a_percentage():
    v = trend.volatility(M)["value"]
    assert v["available"] and 0 <= v["cv_pct"] < 200


def test_business_health_flags_the_demo_merchant():
    assert trend.business_health(M)["value"]["headline"] == "needs_attention"
