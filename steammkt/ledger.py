"""
The order ledger: every sell (and buy) the system plans, places, or sees
complete, in the `orders` table.

It turns "never make a loss overall" into arithmetic:

    realised P/L = sum over SOLD sells of (net - cost basis)

The per-listing rule (net >= cost) already means that can never go
negative. The ledger also lets a listing net *less* than its own cost when,
and only when, profit already banked from confirmed sales covers the
shortfall -- plus the shortfall of every such listing still open
(Seller.allow_subsidy, off by default). Banked means SOLD, never merely
listed: a listing can still be cancelled, so its profit doesn't exist yet.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

OPEN = ("planned", "confirm_pending", "listed")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, store):
        self.store = store

    def record(self, side: str, name: str, asset_id: Optional[str],
               price_paise: int, net_paise: int, cost_paise: int, status: str,
               mode: str, listing_id: Optional[str] = None, note: str = "") -> int:
        now = _now()
        with self.store.tx() as c:
            cur = c.execute(
                "INSERT INTO orders(created_at,updated_at,side,market_hash_name,"
                "asset_id,price_paise,net_paise,cost_basis_paise,status,listing_id,"
                "mode,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, now, side, name, asset_id, price_paise, net_paise,
                 cost_paise, status, listing_id, mode, note))
            return cur.lastrowid

    def update(self, order_id: int, status: str, **fields) -> None:
        cols = {"status": status, "updated_at": _now(), **fields}
        sets = ",".join(f"{k}=?" for k in cols)
        with self.store.tx() as c:
            c.execute(f"UPDATE orders SET {sets} WHERE id=?", (*cols.values(), order_id))

    def orders(self, side: str = "sell", statuses=OPEN,
               mode: Optional[str] = None) -> list[dict]:
        q = (f"SELECT * FROM orders WHERE side=? AND status IN "
             f"({','.join('?' * len(statuses))})")
        args = [side, *statuses]
        if mode:
            q += " AND mode=?"
            args.append(mode)
        return [dict(r) for r in self.store.q(q + " ORDER BY id", args)]

    def busy_assets(self, mode: str) -> set[str]:
        """Assets with an open sell order in this mode -- never list twice."""
        return {o["asset_id"] for o in self.orders(mode=mode)}

    def realised_paise(self) -> int:
        r = self.store.one(
            "SELECT COALESCE(SUM(net_paise - cost_basis_paise), 0) AS pl FROM orders"
            " WHERE side='sell' AND status='sold' AND mode='live'")
        return r["pl"]

    def committed_shortfall_paise(self, mode: str = "live") -> int:
        r = self.store.one(
            "SELECT COALESCE(SUM(MAX(cost_basis_paise - net_paise, 0)), 0) AS s"
            " FROM orders WHERE side='sell' AND mode=? AND status IN"
            " ('planned','confirm_pending','listed')", (mode,))
        return r["s"]

    def subsidy_available_paise(self, mode: str = "live") -> int:
        """Banked profit not already promised to an open below-cost listing."""
        return max(0, self.realised_paise() - self.committed_shortfall_paise(mode))
