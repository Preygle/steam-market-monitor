"""The fee engine is what the no-loss guarantee rests on. Test it hard."""
import pytest
from steammkt.fees import (WalletConfig, fee_amount, list_price_for_net,
                           net_from_buyer_price, amount_to_send_for_desired_received)

CFG = WalletConfig()


@pytest.mark.parametrize("buyer_paise,expect_net", [
    (100,       88),    # Rs 1    -> Rs 0.88
    (500,      436),    # Rs 5    -> Rs 4.36
    (10000,   8697),    # Rs 100  -> Rs 86.97
    (100000, 86958),    # Rs 1000 -> Rs 869.58
])
def test_known_net_values(buyer_paise, expect_net):
    assert fee_amount(buyer_paise, CFG).seller_receives == expect_net


def test_effective_rate_is_13_percent_not_15():
    """5% + 10% is levied on the SELLER's net, so the buyer-side bite is
    15/115 = 13.04%. Assuming a flat 15% overstates proceeds, and an
    overstated net is exactly how an automated seller books a loss."""
    fb = fee_amount(100000, CFG)
    assert 0.128 < fb.effective_rate < 0.132


def test_round_trip_never_undershoots_target():
    """The property the whole system depends on: the price we publish must
    always net at least the floor. Checked across four orders of magnitude."""
    for target in list(range(1, 500)) + [1000, 5000, 25000, 100000, 1000000]:
        price = list_price_for_net(target, CFG)
        assert net_from_buyer_price(price, CFG) >= target, \
            f"target={target} price={price} net={net_from_buyer_price(price, CFG)}"


def test_forward_and_inverse_agree():
    for net in (100, 1000, 4500, 13500, 250000):
        price = amount_to_send_for_desired_received(net, CFG).buyer_pays
        assert fee_amount(price, CFG).seller_receives == net


def test_min_listing_price_is_respected():
    assert list_price_for_net(1, CFG) >= CFG.min_listing_price


def test_zero_and_negative():
    assert fee_amount(0, CFG).buyer_pays == 0
    with pytest.raises(ValueError):
        amount_to_send_for_desired_received(-1, CFG)


def test_fees_are_integers():
    """Floats must never touch money."""
    fb = fee_amount(365600, CFG)
    for v in (fb.buyer_pays, fb.seller_receives, fb.steam_fee, fb.publisher_fee):
        assert isinstance(v, int)
