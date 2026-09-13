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
        """Public inventory. Paginates via last_assetid."""
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
