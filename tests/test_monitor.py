"""The monitor loop: survives bad data, and alerts on transitions, not hourly."""
import sqlite3

import pytest
from fakes import COST, FakeClient, Recorder, hold
from steammkt.alerts import Alert, AlertRouter
from steammkt.events import EventCalendar
from steammkt.fees import WalletConfig, list_price_for_net
from steammkt.monitor import Monitor
from steammkt.store import Store
from steammkt.strategy import Strategy

CFG = WalletConfig()
FLOOR = list_price_for_net(COST, CFG)    # list price that exactly breaks even
ITEM = "Sticker | First Lap (Holo)"


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "market.db")
    client, rec = FakeClient(), Recorder()

    def monitor():
        # A fresh router every sweep, as after a restart: nothing in memory.
        return Monitor(store, client, Strategy(CFG), AlertRouter([rec], store),
                       EventCalendar("config/events.yaml"), CFG)

    return store, client, rec, monitor


def kinds(rec):
    return [a.kind for a in rec.sent]


def test_item_with_no_listings_does_not_abort_the_sweep(rig):
    """priceoverview omits lowest_price when nobody is selling. That used to
    crash the status line and abandon every item after it in the sweep."""
    store, client, rec, monitor = rig
    hold(store, "Sticker | Aaa", "1")
    hold(store, "Sticker | Zzz", "2")
    client.set("Sticker | Aaa", ask=None, median=3000)
    client.set("Sticker | Zzz", ask=3000)
    monitor().sweep()
    planned = {r["market_hash_name"] for r in store.q("SELECT * FROM plan")}
    assert planned == {"Sticker | Aaa", "Sticker | Zzz"}


def test_break_even_alert_fires_once_when_the_ask_crosses_the_floor(rig):
    store, client, rec, monitor = rig
    hold(store, ITEM, "1")

    client.set(ITEM, ask=FLOOR - 500)
    monitor().sweep()
    assert rec.sent == []                    # underwater: nothing to say

    client.set(ITEM, ask=FLOOR + 100)
    monitor().sweep()
    assert kinds(rec) == ["target_hit"]
    assert rec.sent[0].price_to_type         # tells you what to type

    monitor().sweep()                        # still above: no repeat
    assert kinds(rec) == ["target_hit"]


def test_floor_breach_after_the_ask_falls_back(rig):
    store, client, rec, monitor = rig
    hold(store, ITEM, "1")
    client.set(ITEM, ask=FLOOR + 100)
    monitor().sweep()
    client.set(ITEM, ask=FLOOR - 500)
    monitor().sweep()
    assert kinds(rec) == ["target_hit", "floor_breach"]
    assert rec.sent[1].price_to_type == ""   # never a price to type below floor


def test_missing_ask_keeps_the_last_known_state(rig):
    """An empty sell side is not a price drop."""
    store, client, rec, monitor = rig
    hold(store, ITEM, "1")
    client.set(ITEM, ask=FLOOR + 100)
    monitor().sweep()
    client.set(ITEM, ask=None, median=FLOOR + 100)
    monitor().sweep()
    assert kinds(rec) == ["target_hit"]


def test_illiquid_item_does_not_count_as_clearing(rig):
    """A price with no buyers is not a price."""
    store, client, rec, monitor = rig
    hold(store, ITEM, "1")
    client.set(ITEM, ask=FLOOR + 100, volume=0)
    monitor().sweep()
    assert rec.sent == []


def test_alerts_already_sent_today_survive_a_restart(tmp_path):
    store = Store(tmp_path / "market.db")
    rec = Recorder()
    a = Alert(kind="spike", title="t", body="b", market_hash_name="x")
    assert AlertRouter([rec], store).send(a, dedupe_key="spike:x:2026-09-13")
    assert not AlertRouter([rec], store).send(a, dedupe_key="spike:x:2026-09-13")
    assert len(rec.sent) == 1
    row = store.one("SELECT market_hash_name FROM alerts")
    assert row["market_hash_name"] == "x"


def test_database_from_before_the_new_columns_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE plan (market_hash_name TEXT PRIMARY KEY, qty INTEGER)")
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts TEXT)")
    con.commit()
    con.close()
    store = Store(path)
    plan_cols = {r["name"] for r in store.q("PRAGMA table_info(plan)")}
    alert_cols = {r["name"] for r in store.q("PRAGMA table_info(alerts)")}
    assert {"action", "clears_floor"} <= plan_cols
    assert "dedupe_key" in alert_cols
