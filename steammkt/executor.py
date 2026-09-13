"""
Executors: the only code that changes anything on Steam.

    DryRunExecutor  records what it would do. The default.
    SteamExecutor   lists / cancels / places buy orders with the logged-in
                    session from /login.

Steam holds every new listing until it is confirmed in the Steam mobile app,
so even the live executor can't complete a sale by itself: it queues the
listings and you approve them (Steam Guard -> Confirmations -> select all).
Automating that tap would need the authenticator's identity_secret, which
hands full control of the account to whatever holds it. This project won't
ask for it.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional

APPID_CS2 = 730
CONTEXT_CS2 = 2
COMMUNITY = "https://steamcommunity.com"


@dataclass
class SellOrder:
    market_hash_name: str
    asset_id: str
    price_paise: int        # what the buyer pays -- what Steam's dialog shows
    net_paise: int          # what we receive; the number sellitem is sent
    cost_basis_paise: int


@dataclass
class ExecResult:
    ok: bool
    status: str             # planned | confirm_pending | listed | failed
    listing_id: Optional[str] = None
    note: str = ""


class DryRunExecutor:
    mode = "dry_run"

    def sell(self, order: SellOrder) -> ExecResult:
        return ExecResult(True, "planned", note="dry run: nothing sent to Steam")

    def cancel(self, listing_id: str) -> bool:
        return True

    def buy(self, name: str, price_paise: int, qty: int = 1) -> ExecResult:
        return ExecResult(True, "planned", note="dry run: nothing sent to Steam")

    def my_listings(self) -> Optional[dict[str, str]]:
        return None             # nothing is on Steam in a dry run


class SteamExecutor:
    mode = "live"

    def __init__(self, session, steamid: str, currency: int, rate_limiter=None):
        self.s = session
        self.steamid = steamid
        self.currency = currency
        self.rl = rate_limiter
        # Community POSTs need a sessionid cookie that matches the form field.
        self.sessionid = secrets.token_hex(12)
        self.s.cookies.set("sessionid", self.sessionid, domain="steamcommunity.com")

    def _wait(self) -> None:
        if self.rl:
            self.rl.wait()

    def _post(self, path: str, data: dict) -> tuple[int, dict]:
        self._wait()
        r = self.s.post(COMMUNITY + path, data={"sessionid": self.sessionid, **data},
                        headers={"Referer": f"{COMMUNITY}/profiles/{self.steamid}/inventory/",
                                 "Origin": COMMUNITY}, timeout=30)
        try:
            return r.status_code, r.json() or {}
        except ValueError:
            return r.status_code, {}

    def sell(self, order: SellOrder) -> ExecResult:
        # sellitem takes what the SELLER receives, in minor units, and adds the
        # fees back on to get the buyer price. So the exact number we checked
        # against cost basis is the exact number Steam gets.
        status, j = self._post("/market/sellitem/", {
            "appid": APPID_CS2, "contextid": CONTEXT_CS2, "assetid": order.asset_id,
            "amount": 1, "price": order.net_paise})
        if status == 200 and j.get("success"):
            pending = j.get("requires_confirmation") or j.get("needs_mobile_confirmation")
            return ExecResult(True, "confirm_pending" if pending else "listed")
        return ExecResult(False, "failed", note=str(j.get("message") or f"HTTP {status}"))

    def cancel(self, listing_id: str) -> bool:
        status, _ = self._post(f"/market/removelisting/{listing_id}", {})
        return status == 200

    def buy(self, name: str, price_paise: int, qty: int = 1) -> ExecResult:
        status, j = self._post("/market/createbuyorder/", {
            "currency": self.currency, "appid": APPID_CS2, "market_hash_name": name,
            "price_total": price_paise * qty, "quantity": qty})
        if j.get("success") == 1:
            return ExecResult(True, "listed", listing_id=str(j.get("buy_orderid")))
        return ExecResult(False, "failed", note=str(j.get("message") or f"HTTP {status}"))

    def my_listings(self) -> Optional[dict[str, str]]:
        """asset_id -> listing_id for every listing Steam has for us, confirmed
        or still waiting for confirmation. None if Steam didn't answer."""
        out: dict[str, str] = {}
        start = 0
        while True:
            self._wait()
            r = self.s.get(f"{COMMUNITY}/market/mylistings",
                           params={"start": start, "count": 100, "norender": 1}, timeout=30)
            try:
                j = r.json() or {}
            except ValueError:
                return None
            if r.status_code != 200 or not j.get("success"):
                return None
            page = 0
            for key in ("listings", "listings_on_hold", "listings_to_confirm"):
                for lst in j.get(key) or []:
                    asset = lst.get("asset") or {}
                    if asset.get("id"):
                        out[str(asset["id"])] = str(lst.get("listingid", ""))
                    page += key == "listings"
            start += 100
            if page == 0 or start >= int(j.get("total_count") or 0):
                return out
