"""
Steam Market HTTP client.

Design constraints that shape this file:

* Steam rate-limits market endpoints hard (roughly 20 req / minute, and it
  will 429 you into a multi-minute timeout if you push). A 120-item portfolio
  needs ~120 history calls + ~120 quote calls per full refresh, so pacing is
  not optional -- it is the difference between a working monitor and an IP
  that Steam ignores for an hour.
* `/market/pricehistory/` requires a logged-in session cookie. Everything
  else works anonymously.
* All prices come back as localised strings ("Rs 3,656.04") and must be
  parsed to integer minor units before they touch any arithmetic.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import requests

from .store import Store

APPID_CS2 = 730
CURRENCY_INR = 24

BASE = "https://steamcommunity.com"

_PRICE_RE = re.compile(r"[\d.,]+")
# The page embeds the item's internal id (for the order book) and its
# price history, even for a logged-out visitor.
_NAMEID_RE = re.compile(r"Market_LoadOrderSpread\s*\(\s*(\d+)")
_LINE1_RE = re.compile(r"var\s+line1\s*=\s*(\[.*?\])\s*;", re.S)


def parse_price_to_paise(s: Optional[str]) -> Optional[int]:
    """'Rs 3,656.04' -> 365604.  Locale-tolerant, returns minor units.

    Handles both '1,234.56' (comma thousands) and '1.234,56' (euro style)
    by deciding from the position of the last separator.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(round(float(s) * 100))
    m = _PRICE_RE.search(str(s).replace(" ", " "))
    if not m:
        return None
    num = m.group(0)

    last_dot, last_com = num.rfind("."), num.rfind(",")

    if last_dot >= 0 and last_com >= 0:
        # Both present: the RIGHTMOST separator is the decimal point.
        if last_dot > last_com:
            num = num.replace(",", "")
        else:
            num = num.replace(".", "").replace(",", ".")
    elif last_dot >= 0 or last_com >= 0:
        # Exactly one separator kind. Decide by the size of the trailing
        # group: exactly 3 trailing digits means it is a THOUSANDS
        # separator ("Rs 3,656" is 3656, not 3.656). 1-2 trailing digits
        # means it is a decimal point ("0.42").
        sep = "." if last_dot >= 0 else ","
        idx = max(last_dot, last_com)
        tail = num[idx + 1:]
        if len(tail) == 3 and tail.isdigit():
            num = num.replace(sep, "")          # thousands
        else:
            num = num.replace(sep, ".")          # decimal
    # else: plain integer, nothing to strip

    try:
        return int(round(float(num) * 100))
    except ValueError:
        return None


class RateLimiter:
    """Token bucket + jitter. Conservative by default."""

    def __init__(self, per_minute: int = 15):
        self.interval = 60.0 / max(per_minute, 1)
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        gap = now - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap + random.uniform(0, 0.4))
        self._last = time.monotonic()


@dataclass
class SteamClient:
    store: Store
    currency: int = CURRENCY_INR
    session_cookie: Optional[str] = None   # steamLoginSecure, for pricehistory
    per_minute: int = 15
    timeout: int = 30
    _s: requests.Session = field(default_factory=requests.Session, repr=False)

    def __post_init__(self):
        self.rl = RateLimiter(self.per_minute)
        self.last_error: Optional[str] = None
        self._s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        })
        if self.session_cookie:
            self._s.cookies.set("steamLoginSecure", self.session_cookie,
                                domain="steamcommunity.com")

    @property
    def session(self) -> requests.Session:
        """The HTTP session, cookies included -- for the live executor."""
        return self._s

    # ---- core fetch -------------------------------------------------
    def _get(self, url: str, *, cache_s: float = 0.0) -> Optional[Any]:
        """Parsed JSON, or None. After a None, `last_error` says why --
        rate_limited | not_found | auth | network | http_<code> | bad_json --
        so callers can tell "no such item" from "Steam said slow down"."""
        self.last_error = None
        if cache_s:
            cached = self.store.cache_get(url, cache_s)
            if cached is not None:
                try:
                    return json.loads(cached)
                except json.JSONDecodeError:
                    pass

        backoff = 5.0
        for attempt in range(5):
            self.rl.wait()
            try:
                r = self._s.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                self.last_error = "network"
                # No URL in the message: it names items or the SteamID,
                # and CI logs on a public repo are public.
                print(f"  [net] {type(e).__name__} -- retrying")
                time.sleep(backoff)
                backoff *= 2
                continue

            if r.status_code == 429:
                self.last_error = "rate_limited"
                print(f"  [429] rate limited, sleeping {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            if r.status_code in (401, 403):
                self.last_error = "auth"
                print(f"  [{r.status_code}] Steam wants a login for this request")
                return None
            if r.status_code == 500:
                # Steam returns 500 for unknown market_hash_name.
                self.last_error = "not_found"
                return None
            if not r.ok:
                self.last_error = f"http_{r.status_code}"
                time.sleep(backoff)
                backoff *= 2
                continue

            try:
                data = r.json()
            except json.JSONDecodeError:
                self.last_error = "bad_json"
                return None
            self.last_error = None
            if cache_s:
                self.store.cache_put(url, r.text)
            return data
        return None

    # ---- endpoints --------------------------------------------------
    def price_overview(self, name: str, cache_s: float = 900) -> Optional[dict]:
        """Current lowest ask + median + 24h volume. Anonymous-friendly."""
        url = (f"{BASE}/market/priceoverview/?appid={APPID_CS2}"
               f"&currency={self.currency}&market_hash_name={quote(name)}")
        d = self._get(url, cache_s=cache_s)
        if not d or not d.get("success"):
            return None
        return {
            "lowest_paise": parse_price_to_paise(d.get("lowest_price")),
            "median_paise": parse_price_to_paise(d.get("median_price")),
            "volume": int(str(d.get("volume", "0")).replace(",", "") or 0),
        }

    def _get_text(self, url: str) -> Optional[str]:
        """HTML, with the same pacing and 429 handling as _get."""
        self.last_error = None
        self.rl.wait()
        try:
            r = self._s.get(url, timeout=self.timeout)
        except requests.RequestException:
            self.last_error = "network"
            return None
        if r.status_code == 429:
            self.last_error = "rate_limited"
            return None
        if not r.ok:
            self.last_error = f"http_{r.status_code}"
            return None
        return r.text

    def page_data(self, name: str) -> Optional[dict]:
        """What the item's market page carries: the internal item_nameid the
        order book needs, and the price history the page embeds.

        Steam's JSON endpoints refuse datacenter IPs outright, but this page
        is served -- so this is the way in from CI."""
        url = (f"{BASE}/market/listings/{APPID_CS2}/{quote(name)}"
               f"?l=english&currency={self.currency}")
        html = self._get_text(url)
        if not html:
            return None
        nameid = _NAMEID_RE.search(html)
        line1 = _LINE1_RE.search(html)
        history = []
        if line1:
            try:
                for row in json.loads(line1.group(1)):
                    history.append({"ts": row[0],
                                    "median_paise": int(round(float(row[1]) * 100)),
                                    "volume": int(row[2])})
            except (ValueError, IndexError, TypeError):
                history = []
        return {"item_nameid": int(nameid.group(1)) if nameid else None,
                "history": history}

    def listing_page_price(self, name: str, cache_s: float = 900) -> Optional[int]:
        """Lowest ask scraped from the item's ordinary market page.

        The JSON endpoints are throttled per IP and refuse datacenter
        addresses outright; the HTML page is served by a different path and
        may answer where priceoverview will not."""
        url = (f"{BASE}/market/listings/{APPID_CS2}/{quote(name)}"
               f"?l=english&currency={self.currency}")
        self.last_error = None
        self.rl.wait()
        try:
            r = self._s.get(url, timeout=self.timeout)
        except requests.RequestException:
            self.last_error = "network"
            return None
        if r.status_code == 429:
            self.last_error = "rate_limited"
            return None
        if not r.ok:
            self.last_error = f"http_{r.status_code}"
            return None
        m = (re.search(r'Starting at:[^<]*<[^>]*>([^<]+)<', r.text)
             or re.search(r'"lowest_price"\s*:\s*"([^"]+)"', r.text))
        if not m:
            self.last_error = "no_price_in_page"
            return None
        return parse_price_to_paise(m.group(1))

    def price_history(self, name: str, cache_s: float = 21600) -> Optional[list]:
        """Full daily series [(date, median, volume)]. REQUIRES login cookie.

        This is the dataset the predictor trains on -- without a session
        cookie Steam returns 400 and we fall back to accumulating our own
        priceoverview snapshots over time.
        """
        url = (f"{BASE}/market/pricehistory/?appid={APPID_CS2}"
               f"&currency={self.currency}&market_hash_name={quote(name)}")
        d = self._get(url, cache_s=cache_s)
        if not d or not d.get("success") or "prices" not in d:
            return None
        out = []
        for row in d["prices"]:
            # row = ["Jul 08 2026 01: +0", 12.34, "57"]
            out.append({
                "ts": row[0],
                "median_paise": int(round(float(row[1]) * 100)),
                "volume": int(row[2]),
            })
        return out

    def order_book(self, item_nameid: str, cache_s: float = 300) -> Optional[dict]:
        """Live bid/ask depth. Needs the internal item_nameid, not the name."""
        url = (f"{BASE}/market/itemordershistogram?country=IN&language=english"
               f"&currency={self.currency}&item_nameid={item_nameid}&two_factor=0")
        return self._get(url, cache_s=cache_s)

    def inventory(self, steamid64: str, count: int = 2000) -> list[dict]:
        """Public inventory, the modern endpoint first, the legacy one as backup.

        Steam throttles /inventory/ hard and per IP -- a home connection and a
        GitHub runner both get 429s from the first request -- so when the
        modern endpoint gives us nothing we try the older
        /profiles/<id>/inventory/json/ route, which is throttled separately."""
        items = self._inventory_modern(steamid64, count)
        if items or self.last_error != "rate_limited":
            return items
        print("  [inv] rate limited: trying the legacy inventory endpoint")
        return self._inventory_legacy(steamid64)

    def _inventory_legacy(self, steamid64: str) -> list[dict]:
        items, start = [], 0
        while True:
            url = (f"{BASE}/profiles/{steamid64}/inventory/json/{APPID_CS2}/2"
                   f"?l=english&start={start}")
            d = self._get(url, cache_s=0)
            if not d or not d.get("success"):
                break
            descs = d.get("rgDescriptions") or {}
            for a in (d.get("rgInventory") or {}).values():
                desc = descs.get(f"{a.get('classid')}_{a.get('instanceid')}", {})
                items.append({"assetid": a.get("id"), "classid": a.get("classid"),
                              "instanceid": a.get("instanceid"), "_desc": desc})
            if d.get("more") and d.get("more_start"):
                start = d["more_start"]
            else:
                break
        return items

    def _inventory_modern(self, steamid64: str, count: int = 2000) -> list[dict]:
        items, start = [], None
        while True:
            url = (f"{BASE}/inventory/{steamid64}/{APPID_CS2}/2"
                   f"?l=english&count={count}")
            if start:
                url += f"&start_assetid={start}"
            d = self._get(url, cache_s=0)
            if not d:
                break
            descs = {(x["classid"], x["instanceid"]): x
                     for x in d.get("descriptions", [])}
            for a in d.get("assets", []):
                desc = descs.get((a["classid"], a["instanceid"]), {})
                items.append({**a, "_desc": desc})
            if d.get("more_items") and d.get("last_assetid"):
                start = d["last_assetid"]
            else:
                break
        return items
