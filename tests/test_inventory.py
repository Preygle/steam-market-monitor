"""Inventory import must say WHY it came back empty -- the fixes differ."""
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
