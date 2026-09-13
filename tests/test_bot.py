"""Bot commands: the right numbers, only to the right chat, never a loss."""
import re

import pytest
from fakes import COST, FakeApi, FakeClient, hold
from steammkt.alerts import AlertRouter
from steammkt.bot import TelegramBot, answer, split_message
from steammkt.client import parse_price_to_paise
from steammkt.events import EventCalendar
from steammkt.fees import WalletConfig, list_price_for_net, net_from_buyer_price
from steammkt.monitor import Monitor
from steammkt.store import Store
from steammkt.strategy import Strategy

CFG = WalletConfig()
FLOOR = list_price_for_net(COST, CFG)
NITRO = "Sticker | NITRO! (Lenticular)"
LAP = "Sticker | First Lap (Holo)"


@pytest.fixture
def mon(tmp_path):
    store = Store(tmp_path / "market.db")
    client = FakeClient()
    m = Monitor(store, client, Strategy(CFG), AlertRouter([], store),
                EventCalendar("config/events.yaml"), CFG)
    hold(store, NITRO, "1", source="Auto Racing Stickers")
    hold(store, LAP, "2")
    hold(store, LAP, "3")
    client.set(NITRO, ask=68300)
    client.set(LAP, ask=FLOOR - 500)
    return m


def list_at(text):
    m = re.search(r"LIST AT\s+(Rs [\d,]+\.\d\d)", text)
    return parse_price_to_paise(m.group(1)) if m else None


# ---------------------------------------------------------------- /price
def test_bare_item_name_is_a_price_query(mon):
    out = answer(mon, "nitro")
    assert NITRO in out and "Lowest ask" in out


def test_price_shows_current_break_even_and_what_to_list_at(mon):
    out = answer(mon, "/price nitro")
    assert "Rs 683.00" in out                     # current ask
    assert f"Rs {FLOOR/100:,.2f}" in out          # break-even list price
    assert "you net" in out
    assert list_at(out) is not None
    assert "Armory batch day" in out
    assert "steamcommunity.com/market/listings/730/" in out


def test_underwater_item_never_gets_a_list_price(mon):
    mon.client.set(LAP, ask=FLOOR // 3)
    out = answer(mon, "/price first lap")
    assert "LIST AT" not in out
    assert "unsellable" in out


def test_a_suggested_list_price_always_clears_cost(mon):
    for ask in (FLOOR - 1000, FLOOR - 1, FLOOR, FLOOR + 1, FLOOR * 3):
        mon.client.set(LAP, ask=ask)
        price = list_at(answer(mon, "/price first lap"))
        if price is not None:
            assert net_from_buyer_price(price, CFG) >= COST, ask


def test_ambiguous_query_lists_candidates(mon):
    out = answer(mon, "/price sticker")
    assert "be more specific" in out and NITRO in out and LAP in out


def test_unknown_item_says_so(mon):
    assert "Nothing you hold matches" in answer(mon, "/price no such thing")


def test_a_name_steam_has_no_prices_for_is_not_found(mon):
    """Steam answers success, with no prices, for names it doesn't know.
    That must read as 'not found', and must not store a quote for a typo."""
    mon.client.set("frobnicate", ask=None, median=None, volume=0)
    assert "Nothing you hold matches" in answer(mon, "/price frobnicate")
    assert not mon.store.q("SELECT 1 FROM quotes WHERE market_hash_name='frobnicate'")


def test_rate_limiting_is_not_reported_as_not_found(mon):
    """A 429 means 'ask later', not 'no such item'."""
    mon.client.last_error = "rate_limited"
    out = answer(mon, "/price AK-47 | Redline (Field-Tested)")
    assert "rate-limiting" in out and "Nothing you hold" not in out


def test_usage_without_an_item(mon):
    assert "Usage" in answer(mon, "/price")


# ---------------------------------------------------------------- the rest
def test_sellable_lists_only_items_clearing_break_even(mon):
    mon.sweep()
    out = answer(mon, "/sellable")
    assert "NITRO" in out and "First Lap" not in out
    assert list_at(out.replace("list at", "LIST AT")) is not None


def test_portfolio_totals_the_cost_basis(mon):
    mon.sweep()
    out = answer(mon, "/portfolio")
    assert f"Rs {COST * 3 / 100:,.2f}" in out
    assert "BREAK-EVEN:" in out


def test_portfolio_says_when_only_free_drops_cover_the_cost(mon):
    """Found on real data: a free Glock made the whole account read
    'BREAK-EVEN: YES' while every paid item was underwater."""
    free = "AK-47 | Redline (Field-Tested)"
    hold(mon.store, free, "9", cost=0, item_type="skin")
    mon.client.set(free, ask=50000)
    mon.client.set(NITRO, ask=FLOOR // 3)
    mon.client.set(LAP, ask=FLOOR // 3)
    mon.sweep()
    out = answer(mon, "/portfolio")
    assert "only by selling free drops" in out
    assert "free drops" in out and "paid items" in out


def test_holdings_are_ordered_by_distance_to_break_even(mon):
    mon.sweep()
    out = answer(mon, "/holdings")
    assert out.index("NITRO") < out.index("First Lap")


def test_fees(mon):
    assert "Rs 78.27" in answer(mon, "/fees 90")
    assert "Usage" in answer(mon, "/fees abc")


def test_command_addressed_to_the_bot_by_name(mon):
    assert "Rs 78.27" in answer(mon, "/fees@SteamMktBot 90")


def test_every_command_answers(mon):
    for cmd in ("/status", "/events", "/alerts", "/help", "/start",
                "/sellable", "/portfolio", "/holdings", "/orders"):
        assert answer(mon, cmd).strip(), cmd


def test_unknown_command_shows_help(mon):
    out = answer(mon, "/frobnicate")
    assert "Unknown command" in out and "/price" in out


def test_errors_are_reported_not_raised(mon, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("steam down")
    monkeypatch.setattr(mon, "refresh_one", boom)
    out = answer(mon, "/price nitro")
    assert "failed" in out and "steam down" in out


# ---------------------------------------------------------------- telegram
def update(chat_id, text, uid):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


def test_only_the_owner_gets_answers(mon):
    """A bot is public. Anyone else messaging it must get nothing."""
    api = FakeApi()
    bot = TelegramBot("token", 111, mon, api=api)
    bot.process(update(999, "/portfolio", uid=1))
    assert api.calls == []
    bot.process(update(111, "/fees 90", uid=2))
    assert api.calls[0][0] == "sendMessage"
    assert "78.27" in api.calls[0][1]["text"]
    assert bot.offset == 3                        # both updates acknowledged


def test_long_replies_are_split_under_the_telegram_limit():
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    assert "\n".join(parts) == text
