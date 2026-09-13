"""
Decision to action: turning sell plans into orders.

    Monitor.sweep()    -> SellPlans (strategy + no-loss floor)
    Seller.reconcile() -> what happened to earlier listings (live mode)
    Seller.reprice()   -> move open listings UP when the target rises
    Seller.run()       -> one listing per held asset, through an Executor

The Seller re-checks the money rules itself rather than trusting its input:
every order must net at least its cost basis, or -- only with allow_subsidy
-- be covered by profit already banked from SOLD items. A violation raises,
like SellPlan.validate(): a bug stops the run, it never becomes a listing.

Steam listings don't expire, and waiting months is fine, so an item whose
market is far under break-even still gets a resting listing AT its
break-even price (rest_at_floor). It fills if the market ever gets there
and costs nothing if it never does.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from .executor import SellOrder
from .fees import WalletConfig, net_from_buyer_price
from .ledger import Ledger
from .monitor import SELLABLE
from .strategy import MarketSnapshot, SellPlan


@dataclass
class Seller:
    store: object
    ledger: Ledger
    executor: object              # DryRunExecutor | SteamExecutor
    cfg: WalletConfig
    max_per_run: int = 10
    rest_at_floor: bool = True
    allow_subsidy: bool = False
    reprice_above: float = 0.05   # relist once the target is this much higher

    def price_for(self, plan: SellPlan) -> Optional[int]:
        if plan.action in SELLABLE:
            return plan.target_list_paise
        if plan.action == "unsellable" and self.rest_at_floor:
            return plan.floor_list_paise
        return None               # no price data, or no buyers: wait

    def check(self, order: SellOrder) -> None:
        """The no-loss gate, on the exact numbers that go to Steam."""
        net = net_from_buyer_price(order.price_paise, self.cfg)
        if net != order.net_paise:
            raise AssertionError(f"net mismatch for {order.market_hash_name}: "
                                 f"{net} != {order.net_paise}")
        short = order.cost_basis_paise - net
        if short <= 0:
            return
        if self.allow_subsidy and short <= self.ledger.subsidy_available_paise(
                self.executor.mode):
            return
        raise AssertionError(
            f"NO-LOSS VIOLATION for {order.market_hash_name}: nets "
            f"Rs{net/100:.2f} < cost Rs{order.cost_basis_paise/100:.2f}")

    def _profit(self, plan: SellPlan) -> float:
        price = self.price_for(plan)
        if price is None:
            return float("-inf")
        return net_from_buyer_price(price, self.cfg) - plan.cost_basis_paise

    def _free_assets(self, name: str, busy: set[str]) -> list[str]:
        """Held assets of this item that aren't listed and aren't trade-held."""
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.store.q(
            "SELECT asset_id FROM holdings WHERE market_hash_name=?"
            " AND (tradable_after IS NULL OR tradable_after='' OR tradable_after<=?)"
            " ORDER BY asset_id", (name, now))
        return [r["asset_id"] for r in rows if r["asset_id"] not in busy]

    def run(self, plans: list[SellPlan]) -> list[dict]:
        mode = self.executor.mode
        busy = self.ledger.busy_assets(mode)
        done: list[dict] = []
        # Most profitable first: if the per-run cap bites, the best go out first.
        for plan in sorted(plans, key=self._profit, reverse=True):
            price = self.price_for(plan)
            if price is None:
                continue
            net = net_from_buyer_price(price, self.cfg)
            for asset_id in self._free_assets(plan.market_hash_name, busy):
                if len(done) >= self.max_per_run:
                    return done
                order = SellOrder(plan.market_hash_name, asset_id, price, net,
                                  plan.cost_basis_paise)
                self.check(order)
                res = self.executor.sell(order)
                oid = self.ledger.record("sell", order.market_hash_name, asset_id,
                                         price, net, order.cost_basis_paise,
                                         res.status, mode, res.listing_id, res.note)
                busy.add(asset_id)
                done.append({"id": oid, "name": order.market_hash_name,
                             "price": price, "net": net,
                             "cost": order.cost_basis_paise, "ok": res.ok,
                             "status": res.status, "note": res.note})
        return done

    def reprice(self, plans: list[SellPlan]) -> list[dict]:
        """Move open orders UP when the target has risen past reprice_above.

        Never down: a lower price is a fresh decision for run(), and still
        has to get through check(). A live listing is cancelled here and
        relisted at the new price by the next run()."""
        targets = {p.market_hash_name: self.price_for(p) for p in plans}
        moved = []
        for o in self.ledger.orders(mode=self.executor.mode):
            new = targets.get(o["market_hash_name"])
            if not new or new <= o["price_paise"] * (1 + self.reprice_above):
                continue
            if o["status"] == "planned":
                self.ledger.update(o["id"], "planned", price_paise=new,
                                   net_paise=net_from_buyer_price(new, self.cfg))
            elif o["listing_id"] and self.executor.cancel(o["listing_id"]):
                self.ledger.update(o["id"], "cancelled", note=f"repriced to {new}")
            else:
                continue
            moved.append({"name": o["market_hash_name"], "old": o["price_paise"],
                          "new": new})
        return moved

    def reconcile(self) -> dict[str, int]:
        """Match open live orders against Steam's list of our listings.

        Sold vs cancelled is inferred: a listing that's gone while its asset
        is no longer in the inventory is taken as sold. /market/myhistory
        would make that exact; it's the next thing to add here."""
        active = self.executor.my_listings()
        if active is None:
            return {}
        held = {r["asset_id"] for r in self.store.q("SELECT asset_id FROM holdings")}
        counts = {"listed": 0, "sold": 0, "cancelled": 0}
        for o in self.ledger.orders(mode="live"):
            aid = o["asset_id"]
            if aid in active:
                if o["status"] != "listed" or o["listing_id"] != active[aid]:
                    self.ledger.update(o["id"], "listed", listing_id=active[aid])
                    counts["listed"] += 1
            elif aid not in held:
                self.ledger.update(o["id"], "sold")
                counts["sold"] += 1
            else:
                self.ledger.update(o["id"], "cancelled", note="no longer on Steam")
                counts["cancelled"] += 1
        return counts


@dataclass
class Buyer:
    """Scaffold for the buy side. Not wired into the CI run yet.

    Places one buy order for a watched item when its fair value is known,
    bidding `discount` under it, and never lets open plus filled buys exceed
    budget_paise. Deliberately simple until the sell side has run live."""
    ledger: Ledger
    executor: object
    budget_paise: int = 0
    discount: float = 0.20

    def committed_paise(self) -> int:
        r = self.ledger.store.one(
            "SELECT COALESCE(SUM(price_paise), 0) AS s FROM orders WHERE side='buy'"
            " AND status NOT IN ('cancelled','failed')")
        return r["s"]

    def consider(self, snap: MarketSnapshot) -> Optional[dict]:
        fv = snap.fair_value_paise()
        if not fv:
            return None
        if any(o["market_hash_name"] == snap.market_hash_name
               for o in self.ledger.orders(side="buy")):
            return None           # one open bid per item
        bid = int(fv * (1 - self.discount))
        if bid <= 0 or self.committed_paise() + bid > self.budget_paise:
            return None
        res = self.executor.buy(snap.market_hash_name, bid, 1)
        oid = self.ledger.record("buy", snap.market_hash_name, None, bid, 0, 0,
                                 res.status, self.executor.mode, res.listing_id,
                                 res.note)
        return {"id": oid, "name": snap.market_hash_name, "bid": bid,
                "status": res.status}
