"""Buy / sell scaffolding: the ledger, the executors, and above all that no
order ever reaches Steam netting less than it cost."""
import pytest
import requests
from fakes import COST, hold
from steammkt.executor import DryRunExecutor, SellOrder, SteamExecutor
from steammkt.fees import WalletConfig, list_price_for_net, net_from_buyer_price
from steammkt.ledger import Ledger
from steammkt.reports import orders_report, placed_summary
from steammkt.store import Store
from steammkt.strategy import MarketSnapshot, Strategy
from steammkt.trader import Buyer, Seller

CFG = WalletConfig()
ST = Strategy(CFG)
FLOOR = list_price_for_net(COST, CFG)


def snap(name, ask, n=30, vol=50):
    return MarketSnapshot(name, lowest_paise=ask, median_paise=ask, volume_24h=vol,
                          history=[{"median_paise": ask, "volume": vol}] * n)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "m.db")
    hold(s, "Sticker | Winner", "1")
    hold(s, "Sticker | Winner", "2")
    hold(s, "Sticker | Cheap", "3")
    return s


def seller(store, executor=None, **kw):
    return Seller(store, Ledger(store), executor or DryRunExecutor(), CFG, **kw)


def plans(winner_ask=9000, cheap_ask=300):
    return [ST.build(snap("Sticker | Winner", winner_ask), COST, qty=2),
            ST.build(snap("Sticker | Cheap", cheap_ask), COST, qty=1)]


# ---------------------------------------------------------------- selling
def test_dry_run_plans_one_order_per_asset_and_never_twice(store):
    s = seller(store)
    first = s.run(plans())
    assert sorted(d["name"] for d in first) == \
        ["Sticker | Cheap", "Sticker | Winner", "Sticker | Winner"]
    assert all(d["status"] == "planned" for d in first)
    assert s.run(plans()) == []                   # all assets already have an order


def test_every_order_nets_at_least_its_cost(store):
    for d in seller(store).run(plans()):
        assert d["net"] >= d["cost"]
        assert net_from_buyer_price(d["price"], CFG) == d["net"]


def test_underwater_item_rests_at_exactly_break_even(store):
    done = {d["name"]: d for d in seller(store).run(plans())}
    assert done["Sticker | Cheap"]["price"] == FLOOR


def test_rest_at_floor_can_be_turned_off(store):
    done = seller(store, rest_at_floor=False).run(plans())
    assert "Sticker | Cheap" not in {d["name"] for d in done}


def test_most_profitable_goes_first_when_capped(store):
    done = seller(store, max_per_run=1).run(plans())
    assert [d["name"] for d in done] == ["Sticker | Winner"]


def test_a_loss_making_order_raises_before_reaching_steam(store):
    s = seller(store)
    bad = SellOrder("Sticker | Cheap", "3", 300, net_from_buyer_price(300, CFG), COST)
    with pytest.raises(AssertionError, match="NO-LOSS"):
        s.check(bad)


def test_subsidy_needs_banked_profit_from_real_sales(store):
    s = seller(store, executor=DryRunExecutor(), allow_subsidy=True)
    s.executor.mode = "live"
    bad = SellOrder("Sticker | Cheap", "3", 300, net_from_buyer_price(300, CFG), COST)
    with pytest.raises(AssertionError):
        s.check(bad)                               # nothing banked yet
    # A confirmed sale that made more than this order's shortfall...
    s.ledger.record("sell", "Sticker | Winner", "1", 9000,
                    net_from_buyer_price(9000, CFG), COST, "sold", "live")
    s.check(bad)                                   # ...covers it


def test_trade_held_assets_wait(store):
    with store.tx() as c:
        c.execute("UPDATE holdings SET tradable_after='2999-01-01T00:00:00Z' WHERE asset_id='3'")
    seller(store).run(plans())
    listed = {r["asset_id"] for r in store.q("SELECT asset_id FROM orders")}
    assert listed == {"1", "2"}                   # "3" is trade-held: not yet


def test_reprice_only_moves_up(store):
    s = seller(store)
    s.run(plans(winner_ask=9000))
    before = {o["asset_id"]: o["price_paise"] for o in s.ledger.orders()}
    assert s.reprice(plans(winner_ask=8000)) == []           # lower: left alone
    moved = s.reprice(plans(winner_ask=12000))
    assert moved and all(m["new"] > m["old"] for m in moved)
    after = {o["asset_id"]: o["price_paise"] for o in s.ledger.orders()}
    assert after["1"] > before["1"]


# ---------------------------------------------------------------- live Steam
class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, post_body=None, get_body=None):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.posts, self.post_body, self.get_body = [], post_body or {}, get_body or {}

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, data))
        return FakeResponse(200, self.post_body)

    def get(self, url, params=None, timeout=None):
        return FakeResponse(200, self.get_body)


def test_sellitem_is_sent_what_we_receive_not_the_buyer_price():
    sess = FakeSession(post_body={"success": True, "requires_confirmation": 1})
    ex = SteamExecutor(sess, "765", 24)
    res = ex.sell(SellOrder("Sticker | Winner", "1", 9000, 7826, COST))
    url, data = sess.posts[0]
    assert url.endswith("/market/sellitem/")
    assert data["price"] == 7826 and data["assetid"] == "1"
    assert data["sessionid"] == sess.cookies.get("sessionid")
    assert (res.ok, res.status) == (True, "confirm_pending")


def test_steam_refusal_is_recorded_as_failed():
    ex = SteamExecutor(FakeSession(post_body={"success": False,
                                              "message": "trade protected"}), "765", 24)
    res = ex.sell(SellOrder("x", "1", 9000, 7826, COST))
    assert (res.ok, res.status, res.note) == (False, "failed", "trade protected")


def test_reconcile_marks_listed_sold_and_cancelled(store):
    body = {"success": True, "total_count": 1,
            "listings": [{"listingid": "L1", "asset": {"id": "1"}}]}
    ex = SteamExecutor(FakeSession(get_body=body), "765", 24)
    led = Ledger(store)
    for aid in ("1", "2", "3"):
        led.record("sell", "Sticker | Winner", aid, 9000, 7826, COST,
                   "confirm_pending", "live")
    with store.tx() as c:
        c.execute("DELETE FROM holdings WHERE asset_id='2'")   # gone from inventory
    counts = Seller(store, led, ex, CFG).reconcile()
    status = {o["asset_id"]: o["status"] for o in led.orders(statuses=(
        "listed", "sold", "cancelled"))}
    assert status == {"1": "listed", "2": "sold", "3": "cancelled"}
    assert counts == {"listed": 1, "sold": 1, "cancelled": 1}


# ---------------------------------------------------------------- buying
def test_buyer_respects_its_budget(store):
    led = Ledger(store)
    b = Buyer(led, DryRunExecutor(), budget_paise=10000, discount=0.2)
    assert b.consider(snap("AK-47 | Redline (Field-Tested)", 10000))["bid"] == 8000
    assert b.consider(snap("AK-47 | Redline (Field-Tested)", 10000)) is None  # one bid per item
    assert b.consider(snap("AWP | Asiimov (Field-Tested)", 10000)) is None    # over budget


# ---------------------------------------------------------------- reports
def test_orders_report_groups_and_totals(store):
    placed = seller(store).run(plans())
    text = orders_report(type("M", (), {"store": store})())
    assert "Planned (dry run" in text and "Winner x2" in text
    assert "Realised P/L: +0.00 on 0 sold" in text
    assert "Dry run" in placed_summary(placed, [], "dry_run")
    assert "Confirmations" in placed_summary(placed, [], "live")
