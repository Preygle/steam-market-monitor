"""Armory passes -> INR per credit -> INR per item."""
import pytest
from steammkt.costbasis import CostModel, classify_item, CREDIT_COST


def test_three_passes_at_1350():
    m = CostModel(pass_price_paise=135000, passes_bought=3, credits_earned=120)
    assert m.total_spend_paise == 405000
    assert m.paise_per_credit == pytest.approx(3375)
    assert m.item_cost_paise("sticker") == 3375
    assert m.item_cost_paise("skin") == 13500


def test_partial_grind_raises_the_floor():
    """Earning fewer credits per pass makes each credit -- and therefore
    each item -- cost MORE. Defaulting to the optimistic 40 would understate
    every floor in the portfolio."""
    full = CostModel(135000, 3, credits_earned=120)
    part = CostModel(135000, 3, credits_earned=90)
    assert part.paise_per_credit > full.paise_per_credit


def test_cost_rounds_up_never_down():
    m = CostModel(pass_price_paise=100001, passes_bought=1, credits_earned=3)
    assert m.item_cost_paise("sticker") >= m.paise_per_credit


def test_rejects_zero_credits():
    with pytest.raises(ValueError):
        CostModel(135000, 3, credits_earned=0)


@pytest.mark.parametrize("name,expect", [
    ("Sticker | Bananart", "sticker"),
    ("Charm | Die-cast AK", "charm"),
    ("AWP | Sovereign Flame (Field-Tested)", "skin"),
    ("Revolution Case", "case"),
])
def test_classify(name, expect):
    assert classify_item(name) == expect


def test_credit_table_matches_armory():
    assert CREDIT_COST["sticker"] == 1
    assert CREDIT_COST["case"] == 2
    assert CREDIT_COST["charm"] == 3
    assert CREDIT_COST["skin"] == 4
