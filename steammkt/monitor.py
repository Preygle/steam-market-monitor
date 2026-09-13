"""
The monitor loop.

Runs forever. Each pass:
  1. refresh quotes for every held item (rate-limited)
  2. append the observation to price_history (builds our own dataset even
     without a login cookie)
  3. rebuild the sell plan
  4. emit alerts for anything newly actionable
  5. sleep

Alerts, per item:
  spike         the ask is far enough over fair value to sell into now
  target_hit    the cheapest ask has come up to the break-even list price
  floor_breach  ...and has since fallen back below it

target_hit and floor_breach fire on the TRANSITION, compared with the state
the previous sweep persisted, so an item that sits above its floor for a
month alerts once rather than every hour. Each kind is also capped at one
per item per day, so a price hovering on the floor cannot flap.

Designed to be safe to kill and restart at any moment -- all state is in
SQLite, nothing is held only in memory.
"""
from __future__ import annotations

import datetime as dt
import time
import traceback
from dataclasses import dataclass
from typing import Optional

from .alerts import Alert, AlertRouter, steam_item_url
from .client import SteamClient
from .fees import WalletConfig, net_from_buyer_price
from .events import EventCalendar
from .store import Store
from .strategy import MarketSnapshot, SellPlan, Strategy, evaluate_portfolio

SELLABLE = ("list_now", "list_patient")


@dataclass
class Monitor:
    store: Store
    client: SteamClient
    strategy: Strategy
    router: AlertRouter
    calendar: EventCalendar
    cfg: WalletConfig
    interval_s: int = 3600          # one full sweep per hour is plenty
    quote_cache_s: int = 1800
    quiet: bool = False             # CI: counts only -- Actions logs can be public

    def holdings(self) -> list[dict]:
        rows = self.store.q(
            "SELECT market_hash_name, item_type, COUNT(*) AS qty,"
            "       MAX(cost_basis_paise) AS cost"
            " FROM holdings GROUP BY market_hash_name, item_type"
        )
        return [dict(r) for r in rows]

    def refresh_one(self, name: str) -> Optional[MarketSnapshot]:
        ov = self.client.price_overview(name, cache_s=self.quote_cache_s)
        if ov is None:
            return None
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self.store.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO quotes"
                "(market_hash_name,fetched_at,lowest_paise,median_paise,volume)"
                " VALUES (?,?,?,?,?)",
                (name, now, ov["lowest_paise"], ov["median_paise"], ov["volume"]),
            )
            # Also accumulate a daily history point of our own.
            c.execute(
                "INSERT OR REPLACE INTO price_history"
                "(market_hash_name,ts,median_paise,volume,source)"
                " VALUES (?,?,?,?,'priceoverview')",
                (name, dt.date.today().isoformat(),
                 ov["median_paise"], ov["volume"]),
            )

        hist = [
            {"ts": r["ts"], "median_paise": r["median_paise"], "volume": r["volume"]}
            for r in self.store.q(
                "SELECT ts,median_paise,volume FROM price_history"
                " WHERE market_hash_name=? ORDER BY ts", (name,))
        ]
        return MarketSnapshot(
            market_hash_name=name,
            lowest_paise=ov["lowest_paise"],
            median_paise=ov["median_paise"],
            volume_24h=ov["volume"],
            history=hist,
        )

    def clears_floor(self, snap: MarketSnapshot, plan: SellPlan) -> Optional[bool]:
        """Could this item be listed at the cheapest current ask and still
        net its cost?

        None when there is no ask to compare against: an empty sell side
        is not evidence either way, so it must not trigger a transition.
        """
        if not snap.lowest_paise:
            return None
        return (snap.volume_24h >= self.strategy.min_volume
                and snap.lowest_paise >= plan.floor_list_paise)

    def sweep(self) -> None:
        hold = self.holdings()
        if not hold:
            print("no holdings loaded -- run `import-inventory` first")
            return

        if not self.quiet:
            print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] sweeping {len(hold)} distinct items")
        plans = []
        month_bias = self.calendar.month_bias(dt.date.today().month)

        for i, h in enumerate(hold, 1):
            name = h["market_hash_name"]
            try:
                snap = self.refresh_one(name)
            except Exception:
                traceback.print_exc()
                continue
            if snap is None:
                if not self.quiet:
                    print(f"  [{i}/{len(hold)}] {name[:48]:<48} no data")
                continue

            plan = self.strategy.build(
                snap, h["cost"] or 0, qty=h["qty"],
                item_type=h["item_type"] or "other",
                month_bias=month_bias,
            )
            # Hard safety gate. A bug here must crash, not lose money.
            plan.validate(self.cfg)
            plans.append(plan)

            was_clear = self._previous_clears(name)
            clears = self.clears_floor(snap, plan)
            if clears is None:
                clears = was_clear      # no ask this pass: keep last known state
            self._persist(plan, clears)

            if not self.quiet:
                flag = {"list_now": "!!", "list_patient": " >",
                        "unsellable": "xx", "hold": "  "}.get(plan.action, "  ")
                ask = f"{snap.lowest_paise/100:,.2f}" if snap.lowest_paise else "-"
                print(f"  [{i}/{len(hold)}] {flag} {name[:44]:<44} "
                      f"ask={ask:>9} vol={snap.volume_24h:<5} {plan.action}")

            self._alert(plan, snap, was_clear, clears)

        res = evaluate_portfolio(plans)
        if self.quiet:
            print(f"swept {len(plans)} of {len(hold)} items: "
                  f"{res.sellable_count} sellable, {res.underwater_count} "
                  f"underwater, {res.illiquid_count} held")
        else:
            print("\n" + res.report())

    def _alert(self, plan: SellPlan, snap: MarketSnapshot,
               was_clear: Optional[bool], clears: Optional[bool]) -> None:
        name = plan.market_hash_name
        # validate() has already passed, so a sellable plan's target is safe
        # to put in front of the user as the price to type. Nothing else is.
        price = (f"Rs {plan.target_list_paise/100:,.2f}"
                 if plan.action in SELLABLE else "")

        if plan.action == "list_now":
            kind, title = "spike", f"SELL NOW: {name}"
            body = plan.rationale
        elif clears and not was_clear:
            kind, title = "target_hit", f"BREAK-EVEN REACHED: {name}"
            body = (f"Cheapest ask Rs{snap.lowest_paise/100:,.2f} now clears the "
                    f"break-even list price Rs{plan.floor_list_paise/100:,.2f}.\n"
                    + plan.rationale)
        elif clears is False and was_clear:
            kind, title = "floor_breach", f"BELOW BREAK-EVEN: {name}"
            body = (f"No longer clears the break-even list price "
                    f"Rs{plan.floor_list_paise/100:,.2f}. Hold.\n"
                    + plan.rationale)
            price = ""
        else:
            return

        self.router.send(
            Alert(
                kind=kind,
                title=title,
                body=body + f"\nQty held: {plan.qty}",
                url=steam_item_url(name),
                price_to_type=price,
                market_hash_name=name,
            ),
            dedupe_key=f"{kind}:{name}:{dt.date.today()}",
        )

    def _previous_clears(self, name: str) -> Optional[bool]:
        row = self.store.one(
            "SELECT clears_floor FROM plan WHERE market_hash_name=?", (name,))
        if row is None or row["clears_floor"] is None:
            return None
        return bool(row["clears_floor"])

    def _persist(self, plan, clears: Optional[bool]) -> None:
        with self.store.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO plan(market_hash_name,qty,"
                "cost_basis_paise,breakeven_list_paise,target_list_paise,"
                "floor_list_paise,confidence,rationale,action,clears_floor,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (plan.market_hash_name, plan.qty, plan.cost_basis_paise,
                 plan.floor_list_paise, plan.target_list_paise,
                 plan.floor_list_paise, plan.confidence, plan.rationale,
                 plan.action, None if clears is None else int(clears),
                 dt.datetime.now().isoformat(timespec="seconds")),
            )

    def run_forever(self) -> None:
        print("monitor started. ctrl-c to stop.")
        while True:
            try:
                self.sweep()
            except KeyboardInterrupt:
                print("\nstopped.")
                return
            except Exception:
                traceback.print_exc()
            time.sleep(self.interval_s)
