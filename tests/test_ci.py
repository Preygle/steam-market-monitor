"""The GitHub Actions path: config from secrets, a bounded listen window,
an inventory refresh that notices sold items, and logs that don't leak."""
import datetime as dt
import json

import pytest
from fakes import FakeApi, FakeClient, hold
from steammkt import cli
from steammkt.alerts import AlertRouter
from steammkt.bot import TelegramBot
from steammkt.costbasis import CostModel
from steammkt.events import EventCalendar
from steammkt.fees import WalletConfig
from steammkt.inventory import import_inventory
from steammkt.monitor import Monitor
from steammkt.store import Store
from steammkt.strategy import Strategy

CFG = WalletConfig()
COSTS = CostModel(135000, 3, credits_earned=120)
SECRETS = {"STEAM_ID64": "76561190000000000", "PASS_PRICE_PAISE": "135000",
           "PASSES_BOUGHT": "3", "CREDITS_EARNED": "120",
           "TELEGRAM_BOT_TOKEN": "t0ken", "TELEGRAM_CHAT_ID": "42"}


# ---------------------------------------------------------------- config
def test_secrets_fill_the_config():
    cfg = {"steam": {"steamid64": "X"}, "cost_basis": {"pass_price_paise": 0},
           "alerts": {"telegram": {"enabled": False}}}
    cli.apply_env(cfg, SECRETS)
    assert cfg["steam"]["steamid64"] == "76561190000000000"
    assert cfg["cost_basis"] == {"pass_price_paise": 135000, "passes_bought": 3,
                                 "credits_earned": 120}
    assert cfg["alerts"]["telegram"] == {"enabled": True, "bot_token": "t0ken",
                                         "chat_id": "42"}


def test_unset_secrets_change_nothing():
    cfg = {"steam": {"steamid64": "X"}}
    assert cli.apply_env(cfg, {"STEAM_ID64": ""}) == {"steam": {"steamid64": "X"}}


def test_ci_builds_its_config_from_the_example_plus_secrets(tmp_path, monkeypatch):
    """In CI there is no config.yaml -- it is git-ignored -- only secrets."""
    monkeypatch.setattr(cli, "CFG_PATH", tmp_path / "absent.yaml")
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    cfg = cli.load_cfg()
    assert cli.cost_from(cfg).item_cost_paise("sticker") == 3375
    assert cli.telegram_cfg(cfg)["enabled"] is True


# ---------------------------------------------------------------- telegram
def update(chat_id, text, uid):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


@pytest.fixture
def mon(tmp_path):
    return Monitor(Store(tmp_path / "m.db"), FakeClient(), Strategy(CFG),
                   AlertRouter([]), EventCalendar("config/events.yaml"), CFG)


def test_commands_fill_the_telegram_menu(mon):
    """The Menu button / "/" list in the chat is what setMyCommands fills."""
    api = FakeApi()
    TelegramBot("t", 42, mon, api=api).register_commands()
    menus = [p for m, p in api.calls if m == "setMyCommands"]
    assert {json.loads(p["scope"])["type"] for p in menus} == \
        {"default", "all_private_chats"}
    names = {c["command"] for c in json.loads(menus[0]["commands"])}
    assert {"price", "sellable", "portfolio", "holdings", "login"} <= names
    button = next(p for m, p in api.calls if m == "setChatMenuButton")
    assert button["chat_id"] == "42"
    assert json.loads(button["menu_button"]) == {"type": "commands"}


def test_listen_answers_waiting_messages_then_confirms_them(mon):
    api = FakeApi([update(42, "/fees 90", uid=7)])
    bot = TelegramBot("t", 42, mon, api=api)
    assert bot.listen(0) == 1
    sent = [p["text"] for m, p in api.calls if m == "sendMessage"]
    assert "78.27" in sent[0]
    assert api.calls[-1] == ("getUpdates", {"offset": 8, "timeout": 0})
    # The next CI run starts fresh and must not answer it a second time.
    again = TelegramBot("t", 42, mon, api=api)
    assert again.listen(0) == 0
    assert len([m for m, _ in api.calls if m == "sendMessage"]) == 1


# ---------------------------------------------------------------- inventory
def asset(aid, name):
    return {"assetid": aid, "classid": "c" + aid, "instanceid": "0",
            "_desc": {"market_hash_name": name, "marketable": 1,
                      "descriptions": [{"value": "Auto Racing Stickers"}]}}


class Inventory:
    def __init__(self, items, last_error=None):
        self.items, self.last_error = items, last_error

    def inventory(self, steamid64):
        return self.items


def held_ids(store):
    return sorted(r["asset_id"] for r in store.q("SELECT asset_id FROM holdings"))


def test_reimport_drops_items_that_were_sold(tmp_path):
    store = Store(tmp_path / "m.db")
    import_inventory(store, Inventory([asset("1", "Sticker | A"),
                                       asset("2", "Sticker | B")]), "x", COSTS)
    res = import_inventory(store, Inventory([asset("1", "Sticker | A")]), "x", COSTS)
    assert held_ids(store) == ["1"]
    assert res["removed"] == 1


def test_a_partial_fetch_never_drops_items(tmp_path):
    store = Store(tmp_path / "m.db")
    import_inventory(store, Inventory([asset("1", "Sticker | A"),
                                       asset("2", "Sticker | B")]), "x", COSTS)
    import_inventory(store, Inventory([asset("1", "Sticker | A")],
                                      last_error="rate_limited"), "x", COSTS)
    assert held_ids(store) == ["1", "2"]


# ---------------------------------------------------------------- scheduling
def test_import_backs_off_while_steam_rate_limits(tmp_path):
    store = Store(tmp_path / "m.db")
    assert cli._import_due(store)
    store.set_meta("inventory_attempt", dt.datetime.now().isoformat())
    assert not cli._import_due(store)     # tried under an hour ago


def test_sweep_runs_once_per_interval(tmp_path):
    store = Store(tmp_path / "m.db")
    assert cli._sweep_due(store, 3600)
    with store.tx() as c:
        c.execute("INSERT INTO plan(market_hash_name, updated_at) VALUES ('x', ?)",
                  (dt.datetime.now().isoformat(timespec="seconds"),))
    assert not cli._sweep_due(store, 3600)


def test_quiet_sweep_keeps_holdings_out_of_public_logs(mon, capsys):
    hold(mon.store, "Sticker | Secret Holding", "1")
    mon.client.set("Sticker | Secret Holding", ask=5000)
    mon.quiet = True
    mon.sweep()
    out = capsys.readouterr().out
    assert "Secret Holding" not in out
    assert "swept 1 of 1 items" in out
