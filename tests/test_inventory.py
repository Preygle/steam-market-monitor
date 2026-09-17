"""Inventory import must say WHY it came back empty -- the fixes differ."""
import requests

from steammkt.client import SteamClient
from steammkt.costbasis import CostModel
from steammkt.inventory import import_inventory
from steammkt.store import Store

COST = CostModel(135000, 3, credits_earned=120)


class EmptyInventory:
    def __init__(self, last_error):
        self.last_error = last_error

    def inventory(self, steamid64):
        return []


def test_rate_limit_is_not_blamed_on_privacy(tmp_path):
    res = import_inventory(Store(tmp_path / "m.db"), EmptyInventory("rate_limited"),
                           "1", COST)
    assert "rate-limiting" in res["error"]


def test_empty_inventory_points_at_privacy_settings(tmp_path):
    res = import_inventory(Store(tmp_path / "m.db"), EmptyInventory(None), "1", COST)
    assert "private" in res["error"]


# ---------------------------------------------------------------- endpoints
class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, ""
        self.ok = 200 <= status < 300

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Answers by URL fragment, in order."""

    def __init__(self, routes):
        self.headers, self.cookies = {}, requests.cookies.RequestsCookieJar()
        self.routes, self.urls = routes, []

    def get(self, url, timeout=None):
        self.urls.append(url)
        for fragment, response in self.routes:
            if fragment in url:
                return response
        return FakeResponse(404)


LEGACY = {"success": 1,
          "rgInventory": {"7_0": {"id": "7", "classid": "c", "instanceid": "0"}},
          "rgDescriptions": {"c_0": {"market_hash_name": "Sticker | A",
                                     "marketable": 1,
                                     "descriptions": [{"value": "Auto Racing Stickers"}]}}}


def test_inventory_falls_back_when_steam_rate_limits(tmp_path, monkeypatch):
    """Steam 429s /inventory/ per IP -- home connections and CI runners alike.
    The older endpoint is throttled separately, so it's worth a try."""
    monkeypatch.setattr("steammkt.client.time.sleep", lambda s: None)
    session = FakeSession([("/inventory/765", FakeResponse(429)),
                           ("/inventory/json/", FakeResponse(200, LEGACY))])
    client = SteamClient(Store(tmp_path / "m.db"), _s=session)
    client.rl.interval = 0
    items = client.inventory("765")
    assert [i["assetid"] for i in items] == ["7"]
    assert items[0]["_desc"]["market_hash_name"] == "Sticker | A"
    assert any("/inventory/json/" in u for u in session.urls)
