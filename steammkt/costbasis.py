"""
Cost basis: what each Armory item actually cost, in INR.

The Armory model
----------------
You buy an Armory Pass (fixed price, regional). Playing earns Armory
Credits ("stars") -- a maximum of 40 per pass. You then spend credits to
redeem items:

    sticker set        1 credit
    weapon case        2 credits
    charm capsule      3 credits
    weapon collection  4 credits
    limited edition   25-125 credits

So the true unit cost of a credit is simply:

    INR per credit = (pass price in INR) / (credits actually earned)

Note `credits actually earned`, NOT 40. If you bought a pass and only
ground out 30 credits, your credits cost more. Being honest about this is
what keeps the break-even floor truthful -- rounding it down to the
optimistic 40 would quietly understate the floor on every single item.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Armory credit cost by redemption type.
CREDIT_COST = {
    "sticker": 1,
    "case": 2,
    "charm": 3,
    "skin": 4,          # weapon collection redemption
    "collection": 4,
    "limited": 25,      # varies 25-125; override per item
}


@dataclass
class CostModel:
    """Turns Armory passes into a per-item INR cost basis."""

    pass_price_paise: int          # what ONE Armory Pass cost you, in paise
    passes_bought: int             # how many you bought
    credits_earned: Optional[int] = None   # total credits actually earned
    credits_per_pass_max: int = 40

    def __post_init__(self):
        if self.credits_earned is None:
            # Assume fully ground out. Optimistic -- warn about it.
            self.credits_earned = self.passes_bought * self.credits_per_pass_max
        if self.credits_earned <= 0:
            raise ValueError("credits_earned must be positive")

    @property
    def total_spend_paise(self) -> int:
        return self.pass_price_paise * self.passes_bought

    @property
    def paise_per_credit(self) -> float:
        return self.total_spend_paise / self.credits_earned

    def item_cost_paise(self, item_type: str, credits: Optional[int] = None) -> int:
        """Cost basis for one redeemed item. Rounded UP -- never understate."""
        c = credits if credits is not None else CREDIT_COST.get(item_type, 1)
        import math
        return int(math.ceil(self.paise_per_credit * c))

    def summary(self) -> str:
        return (
            f"{self.passes_bought} pass(es) x Rs {self.pass_price_paise/100:,.2f} "
            f"= Rs {self.total_spend_paise/100:,.2f} total\n"
            f"{self.credits_earned} credits earned "
            f"-> Rs {self.paise_per_credit/100:,.2f} per credit\n"
            f"  sticker (1cr) = Rs {self.item_cost_paise('sticker')/100:,.2f}\n"
            f"  case    (2cr) = Rs {self.item_cost_paise('case')/100:,.2f}\n"
            f"  charm   (3cr) = Rs {self.item_cost_paise('charm')/100:,.2f}\n"
            f"  skin    (4cr) = Rs {self.item_cost_paise('skin')/100:,.2f}"
        )


def classify_item(market_hash_name: str, desc: dict | None = None) -> str:
    """Best-effort item type from the market name + inventory description."""
    n = market_hash_name.lower()
    if n.startswith("sticker |"):
        return "sticker"
    if n.startswith("charm |"):
        return "charm"
    if n.startswith("patch |"):
        return "sticker"
    if "case" in n and "hardened" not in n and "|" not in n:
        return "case"
    if n.endswith("capsule") or n.endswith("package") or n.endswith("box"):
        return "case"
    if "|" in n:
        return "skin"
    if desc:
        for t in desc.get("tags", []) or []:
            if t.get("category") == "Type":
                v = (t.get("internal_name") or "").lower()
                if "sticker" in v:
                    return "sticker"
                if "charm" in v or "keychain" in v:
                    return "charm"
                if "crate" in v:
                    return "case"
    return "other"
