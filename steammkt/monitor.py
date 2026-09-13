"""
The monitor loop.

Runs forever. Each pass:
  1. refresh quotes for every held item (rate-limited)
  2. append the observation to price_history (builds our own dataset even
     without a login cookie)
  3. rebuild the sell plan
  4. emit alerts for anything newly actionable
  5. sleep

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
from .strategy import MarketSnapshot, Strategy, evaluate_portfolio


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

    def sweep(self) -> None:
        hold = self.holdings()
        if not hold:
            print("no holdings loaded -- run `import-inventory` first")
            return

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
            self._persist(plan)

            flag = {"list_now": "!!", "list_patient": " >",
                    "unsellable": "xx", "hold": "  "}.get(plan.action, "  ")
            print(f"  [{i}/{len(hold)}] {flag} {name[:44]:<44} "
                  f"ask={snap.lowest_paise and snap.lowest_paise/100:>9} "
                  f"vol={snap.volume_24h:<5} {plan.action}")

            if plan.action == "list_now":
                self.router.send(
                    Alert(
                        kind="spike",
                        title=f"SELL NOW: {name}",
                        body=plan.rationale + f"\nQty held: {plan.qty}",
                        url=steam_item_url(name),
                        price_to_type=f"Rs {plan.target_list_paise/100:,.2f}",
                    ),
                    dedupe_key=f"{name}:{dt.date.today()}",
                )

        res = evaluate_portfolio(plans)
        print("\n" + res.report())

    def _persist(self, plan) -> None:
        with self.store.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO plan(market_hash_name,qty,"
                "cost_basis_paise,breakeven_list_paise,target_list_paise,"
                "floor_list_paise,confidence,rationale,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (plan.market_hash_name, plan.qty, plan.cost_basis_paise,
                 plan.floor_list_paise, plan.target_list_paise,
                 plan.floor_list_paise, plan.confidence, plan.rationale,
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
