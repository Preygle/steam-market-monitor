"""The decision engine must never emit a loss-making listing."""
import pytest
from steammkt.fees import WalletConfig
from steammkt.strategy import MarketSnapshot, Strategy, evaluate_portfolio

CFG = WalletConfig()
ST = Strategy(CFG)


def liquid(name, ask, hist_price, vol=50, n=30):
    return MarketSnapshot(name, lowest_paise=ask, median_paise=hist_price,
                          volume_24h=vol,
                          history=[{"median_paise": hist_price, "volume": vol}
                                   for _ in range(n)])


def test_profitable_item_is_listed_and_validates():
    plan = ST.build(liquid("x", 9000, 8500), 4500)
    plan.validate(CFG)
    assert plan.action in ("list_now", "list_patient")
    assert plan.expected_net_paise >= 4500


def test_spike_is_detected_and_taken():
    plan = ST.build(liquid("x", 15000, 8500), 4500)
    plan.validate(CFG)
    assert plan.action == "list_now"
    assert "SPIKE" in plan.rationale


def test_underwater_item_is_never_listed():
    plan = ST.build(liquid("x", 1200, 1200), 4500)
    plan.validate(CFG)
    assert plan.action == "unsellable"


def test_illiquid_item_is_held():
    snap = MarketSnapshot("x", lowest_paise=500000, median_paise=500000,
                          volume_24h=0, history=[])
    plan = ST.build(snap, 18000)
    plan.validate(CFG)
    assert plan.action == "hold"


def test_validate_raises_on_floor_violation():
    plan = ST.build(liquid("x", 9000, 8500), 4500)
    plan.target_list_paise = 10          # force a loss-making price
    with pytest.raises(AssertionError):
        plan.validate(CFG)


def test_no_listed_plan_ever_loses_money():
    """Fuzz the space: no combination of cost basis and market state may
    produce a listed plan whose net falls below cost."""
    for cost in (100, 1000, 3375, 13500, 90000):
        for ask in (100, 500, 3000, 9000, 50000, 400000):
            plan = ST.build(liquid("x", ask, int(ask * 0.95)), cost)
            plan.validate(CFG)   # raises if the invariant breaks
            if plan.action in ("list_now", "list_patient"):
                assert plan.expected_net_paise >= cost


def test_portfolio_reports_shortfall_honestly():
    plans = [ST.build(liquid("a", 1200, 1200), 4500),      # underwater
             ST.build(liquid("b", 9000, 8500), 4500)]      # fine
    res = evaluate_portfolio(plans)
    assert res.underwater_count == 1
    assert not res.breaks_even      # must not claim success
