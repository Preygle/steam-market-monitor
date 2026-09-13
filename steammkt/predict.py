"""
Price predictor: what an item's price is likely to do, and what that means
for the price to list it at.

Built for the data we actually have -- a daily median series that is often
short (the July 2026 Armory batch has about two months) and noisy -- so
every estimate is robust, and biased toward caution:

* drift      Theil-Sen slope of log price over the last `window` days,
             shrunk toward zero by how little data there is, and capped at
             +/-1% a day whatever the fit says.
* volatility MAD of daily log returns (x1.4826), with a floor.
* spikes     moves beyond `z` robust sigmas, each attributed to the event
             calendar (direction checked) -- or flagged as landing on the
             weekly drop reset, which is a hint, never a cause.
* odds       log price as Brownian motion with that drift and volatility:
             P(price touches X within T days), by the reflection principle.
* ask        the list price maximising net(x) * P(touch x within T), never
             below break-even. A resting listing fills when the market comes
             to it, so this is the price with the best expected payout
             inside the horizon.

"Touches" is optimistic about "fills" -- cheaper listings go first -- so
treat the odds as a ceiling. Seasonality and scheduled events are reported,
not modelled: their magnitudes in events.yaml are single-sourced.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .fees import WalletConfig, net_from_buyer_price

MU_CAP = 0.01            # |drift| at most 1%/day, whatever the fit says
SIGMA_FLOOR = 0.02       # nothing on a thin market is calmer than this
SIGMA_DEFAULT = 0.05     # too little data to measure: assume it's jumpy
SHRINK_DAYS = 30         # a month of data earns half its measured drift
WEEKLY_RESET_WEEKDAY = 2 # Wednesday; see `recurring` in events.yaml


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def daily_series(rows) -> list[tuple[dt.date, int, int]]:
    """One (date, median, volume) per day. Steam's own history wins over our
    priceoverview snapshots when both exist for a day."""
    best: dict[str, tuple] = {}
    for r in rows:
        if not r["median_paise"]:
            continue
        d = r["ts"][:10]
        if d not in best or (r["source"] == "pricehistory"
                             and best[d][2] != "pricehistory"):
            best[d] = (r["median_paise"], r["volume"] or 0, r["source"])
    return [(dt.date.fromisoformat(d), m, v) for d, (m, v, _) in sorted(best.items())]


def theil_sen(xs: list[float], ys: list[float]) -> float:
    """Median of pairwise slopes: one bad day can't drag the trend."""
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i])
              for i in range(len(xs)) for j in range(i + 1, len(xs))
              if xs[j] != xs[i]]
    return statistics.median(slopes) if slopes else 0.0


def drift(series, window: int = 60) -> tuple[float, int]:
    """(daily drift of log price, points used)."""
    pts = series[-window:]
    if len(pts) < 5:
        return 0.0, len(pts)
    t0 = pts[0][0]
    mu = theil_sen([(d - t0).days for d, _, _ in pts],
                   [math.log(m) for _, m, _ in pts])
    mu *= len(pts) / (len(pts) + SHRINK_DAYS)
    return max(-MU_CAP, min(MU_CAP, mu)), len(pts)


def _returns(series) -> list[tuple[dt.date, float, float]]:
    """(date, simple move, log return per sqrt(day)) between neighbours."""
    out = []
    for (d0, p0, _), (d1, p1, _) in zip(series, series[1:]):
        gap = max((d1 - d0).days, 1)
        out.append((d1, p1 / p0 - 1, math.log(p1 / p0) / math.sqrt(gap)))
    return out


def _robust_sigma(values: list[float]) -> tuple[float, float]:
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return med, max(SIGMA_FLOOR, 1.4826 * mad)


def volatility(series, window: int = 60) -> float:
    rets = [r for _, _, r in _returns(series[-window:])]
    if len(rets) < 5:
        return SIGMA_DEFAULT
    return _robust_sigma(rets)[1]


def spikes(series, calendar=None, scope: Optional[str] = None,
           z: float = 3.0, min_move: float = 0.08) -> list[tuple[dt.date, float, str]]:
    """Unusual one-step moves, each with its best explanation."""
    rets = _returns(series)
    if len(rets) < 5:
        return []
    med, s = _robust_sigma([r for _, _, r in rets])
    reset_day = getattr(calendar, "weekly_reset_weekday", WEEKLY_RESET_WEEKDAY)
    reset_scope = getattr(calendar, "weekly_reset_scope", ["case", "skin"])
    out = []
    for d, move, r in rets:
        if abs(r - med) <= z * s or abs(move) < min_move:
            continue
        why = calendar.attribute(d, move, scope) if calendar else "unexplained"
        if why == "unexplained" and d.weekday() in (reset_day, (reset_day + 1) % 7) \
                and (scope is None or scope in reset_scope):
            why = "unexplained -- lands on a weekly drop reset"
        out.append((d, move, why))
    return out


def hit_probability(p0: float, target: float, mu: float, sigma: float,
                    horizon: float) -> float:
    """P(price touches `target` within `horizon` days), starting at p0."""
    if target <= p0:
        return 1.0
    if horizon <= 0 or sigma <= 0:
        return 0.0
    a = math.log(target / p0)
    s = sigma * math.sqrt(horizon)
    m = mu * horizon
    p = _phi((m - a) / s)
    expo = 2 * mu * a / sigma ** 2
    if expo < 700:              # beyond that, dropping the term only under-states
        p += math.exp(expo) * _phi((-a - m) / s)
    return max(0.0, min(1.0, p))


@dataclass
class Forecast:
    name: str
    p0: int                     # where it starts: the ask, else fair value
    mu: float                   # daily drift of log price
    sigma: float                # daily volatility of log price
    days: int                   # daily points behind the estimate
    spikes: list = field(default_factory=list)

    def median(self, horizon: int) -> int:
        return int(self.p0 * math.exp(self.mu * horizon))

    def band(self, horizon: int, z: float = 1.2816) -> tuple[int, int]:
        """80% band (by default) for the price `horizon` days out."""
        centre = math.log(self.p0) + self.mu * horizon
        spread = z * self.sigma * math.sqrt(horizon)
        return int(math.exp(centre - spread)), int(math.exp(centre + spread))

    def p_touch(self, price: int, horizon: int) -> float:
        return hit_probability(self.p0, price, self.mu, self.sigma, horizon)


def build_forecast(name: str, series, p0: Optional[int], calendar=None,
                   scope: Optional[str] = None, window: int = 60) -> Optional[Forecast]:
    if not p0 or p0 <= 0:
        return None
    mu, n = drift(series, window)
    return Forecast(name, int(p0), mu, volatility(series, window), n,
                    spikes(series, calendar, scope))


def optimal_ask(fc: Forecast, floor: int, horizon: int, cfg: WalletConfig,
                steps: int = 80) -> tuple[int, float, int]:
    """(price, P(touch within horizon), expected net) at the price with the
    best expected payout, never below `floor`.

    Below the current price the touch is certain and net only rises with
    price, so the search starts at max(floor, current)."""
    lo = max(floor, fc.p0)
    p = fc.p_touch(lo, horizon)
    best = (lo, p, p * net_from_buyer_price(lo, cfg))
    hi = int(lo * min(20.0, math.exp(3 * fc.sigma * math.sqrt(max(horizon, 1)))))
    if hi > lo:
        ratio = (hi / lo) ** (1 / steps)
        x = lo
        for _ in range(steps):
            x = int(x * ratio) + 1
            p = fc.p_touch(x, horizon)
            ev = p * net_from_buyer_price(x, cfg)
            if ev > best[2]:
                best = (x, p, ev)
    return best[0], best[1], int(best[2])
