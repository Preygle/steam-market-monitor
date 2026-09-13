"""
Exact reimplementation of Steam Community Market fee arithmetic.

This is a faithful port of Valve's client-side JS
(`CalculateFeeAmount` / `CalculateAmountToSendForDesiredReceivedAmount`
in market JS). Every amount is an INTEGER in MINOR currency units
(paise for INR, cents for USD). Never use floats for money here.

Why the exact port matters
--------------------------
The naive "seller gets buyer_price / 1.15" is wrong at low prices, because
both the Steam fee and the publisher fee are floored and have a per-listing
MINIMUM (1 minor unit each by default). On cheap items -- which is most of a
120-item Armory batch -- the effective fee rate is far above 15%. Valve's
Dec-2025 minimum-price change made this worse in several currencies.

Getting this wrong in the optimistic direction is the single most likely way
to accidentally sell at a loss, so every rounding decision here is
deliberately made in the CONSERVATIVE direction (assume we receive less).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# CS2 / CS:GO publisher (Valve as game publisher) takes 10%.
CS2_PUBLISHER_FEE = 0.10
# Steam itself takes 5%.
DEFAULT_WALLET_FEE_PERCENT = 0.05
DEFAULT_WALLET_FEE_BASE = 0
DEFAULT_WALLET_FEE_MINIMUM = 1  # minor units


@dataclass(frozen=True)
class WalletConfig:
    """Fee parameters. Mirrors Steam's g_rgWalletInfo.

    Defaults match every currency Steam has published. `wallet_fee_minimum`
    is the one that varies and the one worth calibrating -- see
    `steammkt.calibrate`.
    """
    fee_percent: float = DEFAULT_WALLET_FEE_PERCENT
    fee_base: int = DEFAULT_WALLET_FEE_BASE
    fee_minimum: int = DEFAULT_WALLET_FEE_MINIMUM
    publisher_fee: float = CS2_PUBLISHER_FEE
    # Steam refuses listings below this buyer-facing price.
    min_listing_price: int = 100  # paise -> Rs 1.00; calibrate for INR


@dataclass(frozen=True)
class FeeBreakdown:
    """All values in minor units."""
    buyer_pays: int
    seller_receives: int
    steam_fee: int
    publisher_fee: int

    @property
    def total_fees(self) -> int:
        return self.steam_fee + self.publisher_fee

    @property
    def effective_rate(self) -> float:
        """Fees as a fraction of what the buyer pays."""
        return self.total_fees / self.buyer_pays if self.buyer_pays else 0.0


def amount_to_send_for_desired_received(
    received: int, cfg: WalletConfig
) -> FeeBreakdown:
    """Given what the seller wants to RECEIVE, what must the buyer PAY?

    Direct port of CalculateAmountToSendForDesiredReceivedAmount.
    This is the exact direction we care about: we know our break-even
    floor (what we must receive), and we need the price to type into
    the sell box.
    """
    if received < 0:
        raise ValueError("received must be non-negative")

    steam_fee = int(
        (max(received * cfg.fee_percent, cfg.fee_minimum) + cfg.fee_base) // 1
    )
    if cfg.publisher_fee > 0:
        pub_fee = int(max(received * cfg.publisher_fee, 1) // 1)
    else:
        pub_fee = 0

    return FeeBreakdown(
        buyer_pays=received + steam_fee + pub_fee,
        seller_receives=received,
        steam_fee=steam_fee,
        publisher_fee=pub_fee,
    )


def fee_amount(amount: int, cfg: WalletConfig) -> FeeBreakdown:
    """Given what the buyer PAYS, what does the seller RECEIVE?

    Direct port of CalculateFeeAmount, including Valve's iterative
    overshoot/undershoot correction. Valve's own UI uses this, so matching
    it exactly is what makes our numbers agree with the sell dialog.
    """
    if amount <= 0:
        return FeeBreakdown(0, 0, 0, 0)

    # Valve's initial estimate.
    estimate = int(
        (amount - cfg.fee_base) // (cfg.fee_percent + cfg.publisher_fee + 1)
    )
    ever_undershot = False
    fees = amount_to_send_for_desired_received(estimate, cfg)

    iterations = 0
    while fees.buyer_pays != amount and iterations < 32:
        if fees.buyer_pays > amount:
            if ever_undershot:
                # Valve's fudge: back off one unit and absorb the
                # remainder into the Steam fee so the total ties out.
                fees = amount_to_send_for_desired_received(estimate - 1, cfg)
                delta = amount - fees.buyer_pays
                fees = FeeBreakdown(
                    buyer_pays=amount,
                    seller_receives=fees.seller_receives,
                    steam_fee=fees.steam_fee + delta,
                    publisher_fee=fees.publisher_fee,
                )
                break
            estimate -= 1
        else:
            ever_undershot = True
            estimate += 1
        fees = amount_to_send_for_desired_received(estimate, cfg)
        iterations += 1

    return fees


def net_from_buyer_price(buyer_price: int, cfg: WalletConfig) -> int:
    """Convenience: minor units the seller nets from a given listing price."""
    return fee_amount(buyer_price, cfg).seller_receives


def list_price_for_net(
    target_net: int, cfg: WalletConfig, *, conservative: bool = True
) -> int:
    """The price to TYPE INTO THE SELL BOX so we net >= target_net.

    This is the function the whole sell plan calls. `conservative=True`
    walks the price up until the verified round-trip net actually clears
    the target, so floating-point or minimum-fee edge cases can only ever
    err in our favour -- never against us.
    """
    if target_net <= 0:
        return cfg.min_listing_price

    price = amount_to_send_for_desired_received(target_net, cfg).buyer_pays

    if conservative:
        # Verify by round-tripping through Valve's own inverse function.
        # Bump until it genuinely clears. Bounded so it can never spin.
        for _ in range(64):
            if net_from_buyer_price(price, cfg) >= target_net:
                break
            price += 1

    return max(price, cfg.min_listing_price)
