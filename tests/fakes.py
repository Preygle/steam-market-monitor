"""Test doubles shared by the monitor and bot tests."""
from steammkt.alerts import Channel

COST = 3375          # one sticker: Rs 1,350 x 3 passes / 120 credits


class FakeClient:
    """Stands in for SteamClient, serving canned priceoverview quotes."""

    def __init__(self):
        self.quotes = {}
        self.last_error = None

    def set(self, name, ask, median=None, volume=20):
        self.quotes[name] = {"lowest_paise": ask,
                             "median_paise": ask if median is None else median,
                             "volume": volume}

    def price_overview(self, name, cache_s=0):
        return self.quotes.get(name)


class FakeApi:
    """Stands in for the Telegram HTTP API. getUpdates honours `offset` the
    way Telegram does: updates below it are confirmed and never returned."""

    def __init__(self, updates=()):
        self.calls = []
        self.pending = list(updates)

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            off = params.get("offset", 0)
            self.pending = [u for u in self.pending if u["update_id"] >= off]
            return {"ok": True, "result": list(self.pending)}
        return {"ok": True, "result": True}


class Recorder(Channel):
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return True


def hold(store, name, asset_id, cost=COST, item_type="sticker", source=None):
    with store.tx() as c:
        c.execute("INSERT INTO holdings(asset_id,market_hash_name,item_type,"
                  "cost_basis_paise,source) VALUES (?,?,?,?,?)",
                  (asset_id, name, item_type, cost, source))
