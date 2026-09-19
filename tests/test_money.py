"""The honesty test: inflow is not a cash position."""
import config
from backend.analytics import money
from backend.data import db, repository as repo


def _merchant_without_obligations():
    return db.q1("SELECT id FROM merchants WHERE has_obligations=0 AND avg_daily>0 LIMIT 1")["id"]


def test_merchant_with_obligations_gets_a_net_position():
    v = money.cash_view(config.DEMO_MERCHANT)["value"]
    assert v["scope"] == "net_position"
    assert v["obligations_available"] is True
    assert "net_expected" in v


def test_merchant_without_obligations_gets_inflow_only():
    v = money.cash_view(_merchant_without_obligations())["value"]
    assert v["scope"] == "inflow_only"
    assert v["obligations_available"] is False
    assert "net_expected" not in v          # must not imply a position it cannot see


def test_purchase_check_refuses_to_judge_without_outgoings():
    v = money.purchase_check(_merchant_without_obligations(), 25000)["value"]
    assert v["scope"] == "inflow_only"
    assert v["verdict"] == "unknown_without_expenses"


def test_purchase_check_judges_when_obligations_are_known():
    v = money.purchase_check(config.DEMO_MERCHANT, 25000)["value"]
    assert v["scope"] == "net_position"
    assert v["verdict"] in ("unsafe", "tight", "comfortable")
    assert v["buffer_stressed"] <= v["buffer_expected"]
