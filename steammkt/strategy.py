"""
The sell decision engine.

THE ONE INVARIANT
-----------------
    net_received(list_price) >= cost_basis      for every listed item

Everything else -- targets, spike detection, patience -- is optimisation on
top. The invariant is enforced in `SellPlan.validate()` and asserted again
before any alert is emitted, so a bug in the clever parts cannot produce a
loss-making listing.

We also enforce a PORTFOLIO invariant: the sum of realistic expected nets
must clear total spend. An item-by-item break-even can still lose money
overall if some items are unsellable, so the portfolio view is what
actually answers "did I break even".
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .fees import WalletConfig, list_price_for_net, net_from_buyer_price


@dataclass
class MarketSnapshot:
    """What we currently know about one item's market."""
    market_hash_name: str
    lowest_paise: Optional[int] = None      # current best ask
    median_paise: Optional[int] = None      # 24h median
    volume_24h: int = 0
    history: list = field(default_factory=list)   # [{ts, median_paise, volume}]

    @property
    def has_liquidity(self) -> bool:
        return self.volume_24h > 0

    def recent_medians(self, n: int = 30) -> list[int]:
        return [h["median_paise"] for h in self.history[-n:] if h.get("median_paise")]

    def fair_value_paise(self) -> Optional[int]:
        """Robust central estimate. Median of recent medians beats last-tick.

        Deliberately ignores the current lowest ask: a single desperate
        seller undercutting into an illiquid book is noise, not value.
        """
        rec = self.recent_medians(30)
        if rec:
            return int(statistics.median(rec))
        if self.median_paise:
            return self.median_paise
        return self.lowest_paise

    def volatility(self) -> float:
        rec = self.recent_medians(30)
        if len(rec) < 5:
            return 0.0
        mu = statistics.mean(rec)
        return statistics.pstdev(rec) / mu if mu else 0.0


@dataclass
class SellPlan:
    market_hash_name: str
    qty: int
    cost_basis_paise: int          # per unit, what we must NET
    item_type: str = "other"

    # computed
    floor_list_paise: int = 0      # absolute minimum list price. Hard rule.
    target_list_paise: int = 0     # what we actually want
    fair_value_paise: Optional[int] = None
    action: str = "hold"           # list_now | list_patient | hold | unsellable
    confidence: float = 0.0
    rationale: str = ""
    expected_net_paise: int = 0

    def validate(self, cfg: WalletConfig) -> None:
        """Hard assertion of the no-loss invariant. Raises rather than warns."""
        if self.action in ("list_now", "list_patient"):
            price = self.target_list_paise
            net = net_from_buyer_price(price, cfg)
            if net < self.cost_basis_paise:
                raise AssertionError(
                    f"NO-LOSS VIOLATION for {self.market_hash_name}: "
                    f"list Rs{price/100:.2f} nets Rs{net/100:.2f} "
                    f"< cost Rs{self.cost_basis_paise/100:.2f}"
                )
            if price < self.floor_list_paise:
                raise AssertionError(
                    f"FLOOR VIOLATION for {self.market_hash_name}: "
                    f"{price} < floor {self.floor_list_paise}"
                )


@dataclass
class Strategy:
    cfg: WalletConfig
    min_margin: float = 0.0        # 0.0 = pure break-even
    patience_premium: float = 0.06 # ask this much above fair value; we can wait
    spike_threshold: float = 0.15  # >15% above fair value == a spike worth taking
    min_volume: int = 1            # below this, treat as illiquid

    def build(self, snap: MarketSnapshot, cost_basis_paise: int,
              qty: int = 1, item_type: str = "other",
              month: Optional[int] = None, month_bias: float = 0.0) -> SellPlan:

        plan = SellPlan(
            market_hash_name=snap.market_hash_name,
            qty=qty,
            cost_basis_paise=cost_basis_paise,
            item_type=item_type,
        )

        # 1. The floor. Non-negotiable.
        floor_net = int(math.ceil(cost_basis_paise * (1.0 + self.min_margin)))
        plan.floor_list_paise = list_price_for_net(floor_net, self.cfg)

        fv = snap.fair_value_paise()
        plan.fair_value_paise = fv

        # 2. No market data at all -> cannot act.
        if fv is None:
            plan.action = "hold"
            plan.rationale = "no price data yet"
            plan.confidence = 0.0
            return plan

        # 3. Liquidity gate. A price with no buyers is not a price.
        if snap.volume_24h < self.min_volume:
            plan.action = "hold"
            plan.target_list_paise = plan.floor_list_paise
            plan.rationale = (
                f"illiquid (24h volume {snap.volume_24h}). Listing here just "
                f"parks the item at the back of a queue. Hold."
            )
            plan.confidence = 0.2
            return plan

        # 4. Is the floor even achievable?
        current = snap.lowest_paise or fv
        if plan.floor_list_paise > current * 1.5:
            plan.action = "unsellable"
            plan.target_list_paise = plan.floor_list_paise
            plan.rationale = (
                f"break-even needs Rs{plan.floor_list_paise/100:,.2f} but market "
                f"is Rs{current/100:,.2f} -- {plan.floor_list_paise/current:.1f}x away. "
                f"Underwater; hold and wait for supply to dry up."
            )
            plan.confidence = 0.1
            return plan

        # 5. Target price.
        desired = int(fv * (1.0 + self.patience_premium) * (1.0 + month_bias))
        plan.target_list_paise = max(desired, plan.floor_list_paise)

        # 6. Spike detection -- is the market ALREADY paying above our target?
        if snap.lowest_paise and snap.lowest_paise >= fv * (1 + self.spike_threshold):
            # Ride it: undercut the spike slightly to sell into strength fast.
            spike_price = max(snap.lowest_paise - 1, plan.floor_list_paise)
            if net_from_buyer_price(spike_price, self.cfg) >= floor_net:
                plan.target_list_paise = spike_price
                plan.action = "list_now"
                plan.rationale = (
                    f"SPIKE: ask Rs{snap.lowest_paise/100:,.2f} is "
                    f"{(snap.lowest_paise/fv - 1)*100:.0f}% over fair value "
                    f"Rs{fv/100:,.2f}. Sell into strength."
                )
                plan.confidence = 0.8
                plan.expected_net_paise = net_from_buyer_price(
                    plan.target_list_paise, self.cfg)
                return plan

        # 7. Normal patient listing.
        net_at_target = net_from_buyer_price(plan.target_list_paise, self.cfg)
        margin = (net_at_target - cost_basis_paise) / cost_basis_paise \
            if cost_basis_paise else 0.0

        if plan.target_list_paise <= current * 1.02:
            plan.action = "list_patient"
            plan.rationale = (
                f"target Rs{plan.target_list_paise/100:,.2f} sits at/below the "
                f"current ask Rs{current/100:,.2f}; nets "
                f"Rs{net_at_target/100:,.2f} ({margin*100:+.0f}% vs cost). "
                f"Should fill."
            )
            plan.confidence = 0.7
        else:
            plan.action = "list_patient"
            plan.rationale = (
                f"asking Rs{plan.target_list_paise/100:,.2f} above the current "
                f"Rs{current/100:,.2f} ask. Nets Rs{net_at_target/100:,.2f} "
                f"({margin*100:+.0f}%). Will sit in the queue -- fine, we can wait."
            )
            plan.confidence = 0.45

        plan.expected_net_paise = net_at_target
        return plan


@dataclass
class PortfolioResult:
    total_cost_paise: int
    expected_net_paise: int
    sellable_count: int
    underwater_count: int
    illiquid_count: int
    plans: list[SellPlan]

    @property
    def profit_paise(self) -> int:
        return self.expected_net_paise - self.total_cost_paise

    @property
    def breaks_even(self) -> bool:
        return self.expected_net_paise >= self.total_cost_paise

    def report(self) -> str:
        L = []
        L.append(f"Total cost basis : Rs {self.total_cost_paise/100:>12,.2f}")
        L.append(f"Expected net     : Rs {self.expected_net_paise/100:>12,.2f}")
        L.append(f"P/L              : Rs {self.profit_paise/100:>12,.2f}"
                 f"  ({self.profit_paise/self.total_cost_paise*100:+.1f}%)"
                 if self.total_cost_paise else "")
        L.append("")
        L.append(f"sellable {self.sellable_count} | underwater "
                 f"{self.underwater_count} | illiquid {self.illiquid_count}")
        L.append("")
        L.append("BREAK-EVEN: " + ("YES" if self.breaks_even else "NOT YET"))
        return "\n".join(L)


def evaluate_portfolio(plans: list[SellPlan]) -> PortfolioResult:
    total_cost = sum(p.cost_basis_paise * p.qty for p in plans)
    exp_net = sum(p.expected_net_paise * p.qty for p in plans
                  if p.action in ("list_now", "list_patient"))
    return PortfolioResult(
        total_cost_paise=total_cost,
        expected_net_paise=exp_net,
        sellable_count=sum(1 for p in plans if p.action in ("list_now", "list_patient")),
        underwater_count=sum(1 for p in plans if p.action == "unsellable"),
        illiquid_count=sum(1 for p in plans if p.action == "hold"),
        plans=plans,
    )
