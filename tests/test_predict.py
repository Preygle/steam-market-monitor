"""The price predictor: robust trend and volatility, spikes explained by the
event calendar, and list prices that can never go below break-even."""
import datetime as dt
import math

import pytest
from steammkt import predict as pr
from steammkt.events import EventCalendar
from steammkt.fees import WalletConfig, list_price_for_net, net_from_buyer_price
from steammkt.strategy import MarketSnapshot, Strategy

CAL = EventCalendar("config/events.yaml")
CFG = WalletConfig()
FLOOR = list_price_for_net(3375, CFG)


def series(prices, start=dt.date(2026, 7, 8)):
    return [(start + dt.timedelta(days=i), p, 50) for i, p in enumerate(prices)]


# ---------------------------------------------------------------- data
def test_daily_series_prefers_steams_own_history():
    rows = [{"ts": "2026-07-09", "median_paise": 900, "volume": 3, "source": "priceoverview"},
            {"ts": "2026-07-09", "median_paise": 1000, "volume": 40, "source": "pricehistory"},
            {"ts": "2026-07-10", "median_paise": 950, "volume": 5, "source": "priceoverview"}]
    assert pr.daily_series(rows) == [(dt.date(2026, 7, 9), 1000, 40),
                                     (dt.date(2026, 7, 10), 950, 5)]


# ---------------------------------------------------------------- trend / vol
def test_theil_sen_recovers_a_line_despite_an_outlier():
    xs = list(range(10))
    ys = [2 * x + 1 for x in xs]
    ys[4] = 100
    assert pr.theil_sen(xs, ys) == pytest.approx(2)


def test_a_decaying_new_release_has_negative_drift_shrunk_not_exaggerated():
    mu, n = pr.drift(series([int(10000 * math.exp(-0.02 * i)) for i in range(60)]))
    assert n == 60 and -0.02 < mu < 0


def test_drift_needs_data_and_is_capped():
    assert pr.drift(series([100, 200]))[0] == 0.0
    assert pr.drift(series([int(100 * math.exp(0.2 * i)) for i in range(30)]))[0] == pr.MU_CAP


def test_volatility_floor_and_default():
    assert pr.volatility(series([1000] * 30)) == pr.SIGMA_FLOOR
    assert pr.volatility(series([1000, 1000])) == pr.SIGMA_DEFAULT


# ---------------------------------------------------------------- spikes
def test_spike_is_explained_by_the_calendar():
    """Austin 2025 capsules were pulled on 2025-10-02; stickers doubled."""
    calm = [1000 + (i % 3) * 10 for i in range(31)]
    s = series(calm + [2000] + [2000 + (i % 3) * 10 for i in range(10)],
               start=dt.date(2025, 9, 1))
    found = pr.spikes(s, CAL, scope="sticker")
    assert [d for d, _, _ in found] == [dt.date(2025, 10, 2)]
    assert "Austin 2025 capsules REMOVED" in found[0][2]


def test_calendar_knows_the_weekly_reset():
    assert CAL.weekly_reset_weekday == 2
    assert any("Trade Protection" in e.name for e in CAL.events)


# ---------------------------------------------------------------- odds
def test_hit_probability_behaves():
    assert pr.hit_probability(1000, 900, 0.0, 0.05, 30) == 1.0
    a, s = math.log(1.5), 0.05 * math.sqrt(90)
    base = pr.hit_probability(1000, 1500, 0.0, 0.05, 90)
    assert base == pytest.approx(2 * pr._phi(-a / s))          # reflection principle
    assert pr.hit_probability(1000, 2000, 0.0, 0.05, 90) < base
    assert base < pr.hit_probability(1000, 1500, 0.0, 0.05, 365)
    assert pr.hit_probability(1000, 1500, -0.01, 0.05, 90) < base \
        < pr.hit_probability(1000, 1500, 0.01, 0.05, 90)


# ---------------------------------------------------------------- the ask
def test_optimal_ask_never_goes_below_break_even():
    fc = pr.Forecast("x", p0=1500, mu=-0.005, sigma=0.04, days=60)
    assert pr.optimal_ask(fc, FLOOR, 180, CFG)[0] >= FLOOR


def test_without_upward_drift_the_best_ask_is_the_market():
    fc = pr.Forecast("x", p0=9000, mu=0.0, sigma=0.05, days=60)
    assert pr.optimal_ask(fc, FLOOR, 180, CFG)[0] == 9000


def test_with_upward_drift_it_pays_to_wait():
    fc = pr.Forecast("x", p0=9000, mu=0.005, sigma=0.03, days=60)
    assert pr.optimal_ask(fc, FLOOR, 180, CFG)[0] > 9000


def test_forecast_driven_plans_never_lose_money():
    st = Strategy(CFG)
    for mu in (-0.01, 0.0, 0.01):
        for sigma in (0.02, 0.1):
            for ask in (500, 3000, 9000, 50000):
                fc = pr.Forecast("x", ask, mu, sigma, 60)
                snap = MarketSnapshot("x", ask, ask, 50,
                                      [{"median_paise": ask, "volume": 50}] * 30)
                plan = st.build(snap, 3375, forecast=fc)
                plan.validate(CFG)
                if plan.action in ("list_now", "list_patient"):
                    assert net_from_buyer_price(plan.target_list_paise, CFG) >= 3375
