"""
Read-only text reports: the answers behind the Telegram bot's commands and
`cli ask`.

Every function returns plain text sized for a phone screen. Nothing here
places a listing. Any price shown as "LIST AT" comes from a plan that has
just passed SellPlan.validate() -- the same gate the monitor uses before an
alert -- so the bot cannot suggest a loss-making price either.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from .alerts import steam_item_url
from .client import parse_price_to_paise
from .events import EventCalendar
from .fees import WalletConfig, fee_amount, list_price_for_net, net_from_buyer_price
from .inventory import ARMORY_2026_07, ARMORY_2026_07_RELEASED
from .monitor import SELLABLE, Monitor

NO_HOLDINGS = "No holdings loaded. Run: python -m steammkt.cli import-inventory"
STEAM_BUSY = "Steam is rate-limiting or unreachable right now. Try again in a few minutes."


def _steam_busy(mon: Monitor) -> bool:
    return getattr(mon.client, "last_error", None) in ("rate_limited", "network")


def rs(paise: Optional[int]) -> str:
    return "-" if paise is None else f"Rs {paise/100:,.2f}"


def _num(paise: Optional[int]) -> str:
    return "-" if paise is None else f"{paise/100:,.2f}"


def short(name: str, width: int = 34) -> str:
    """'Sticker | Please Be Patient' -> 'Please Be Patient', trimmed."""
    for prefix in ("Sticker | ", "Charm | ", "Patch | "):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name if len(name) <= width else name[:width - 2] + ".."


def resolve(mon: Monitor, query: str) -> tuple[Optional[str], list[str]]:
    """Map what the user typed to a held market_hash_name.

    Every word of the query must appear in the name, case-insensitively, so
    "nitro" or "first lap" is enough. Returns (name, []) on a unique match
    and (None, candidates) when ambiguous. When nothing held matches it
    returns (query, []) and the caller treats it as an exact market name.
    """
    names = [r["market_hash_name"] for r in mon.store.q(
        "SELECT DISTINCT market_hash_name FROM holdings")]
    exact = [n for n in names if n.lower() == query.lower()]
    if exact:
        return exact[0], []
    words = query.lower().split()
    hits = sorted(n for n in names if all(w in n.lower() for w in words))
    if len(hits) == 1:
        return hits[0], []
    if hits:
        return None, hits
    return query, []


def _holding(mon: Monitor, name: str) -> Optional[dict]:
    r = mon.store.one(
        "SELECT item_type, COUNT(*) AS qty, MAX(cost_basis_paise) AS cost,"
        " MAX(source) AS source FROM holdings WHERE market_hash_name=?", (name,))
    return dict(r) if r["qty"] else None


def _latest_quotes(mon: Monitor) -> dict[str, dict]:
    rows = mon.store.q(
        "SELECT q.* FROM quotes q JOIN (SELECT market_hash_name, MAX(fetched_at) AS f"
        " FROM quotes GROUP BY market_hash_name) l"
        " ON q.market_hash_name = l.market_hash_name AND q.fetched_at = l.f")
    return {r["market_hash_name"]: dict(r) for r in rows}


def _history_lines(mon: Monitor, name: str, today: dt.date) -> list[str]:
    by_day: dict[str, int] = {}
    for r in mon.store.q(
            "SELECT ts, median_paise FROM price_history WHERE market_hash_name=?"
            " AND median_paise IS NOT NULL ORDER BY ts", (name,)):
        by_day[r["ts"][:10]] = r["median_paise"]
    month = [p for d, p in by_day.items()
             if dt.date.fromisoformat(d) > today - dt.timedelta(days=30)]
    if len(month) < 2:
        return ["History      not enough yet -- the monitor adds a point a day"]
    L = [f"30d range    {rs(min(month))} - {rs(max(month))}  ({len(month)} days)"]
    week_ago = [p for d, p in by_day.items()
                if dt.date.fromisoformat(d) <= today - dt.timedelta(days=7)]
    latest = by_day[max(by_day)]
    if week_ago and week_ago[-1]:
        L.append(f"7d change    {(latest / week_ago[-1] - 1) * 100:+.1f}%")
    return L


def _gap(floor: int, ask: Optional[int]) -> str:
    if not ask:
        return ""
    ratio = floor / ask
    return "  (the ask clears it)" if ratio <= 1 else f"  (needs {ratio:.2f}x the ask)"


# ---------------------------------------------------------------- reports
def price_report(mon: Monitor, query: str, today: Optional[dt.date] = None) -> str:
    """Live price, what you'd net, break-even, and what to list at."""
    today = today or dt.date.today()
    if not query:
        return "Usage: /price <item>    e.g. /price nitro"
    name, candidates = resolve(mon, query)
    if name is None:
        more = (f"\n  ...and {len(candidates) - 10} more"
                if len(candidates) > 10 else "")
        return ("Several items match -- be more specific:\n"
                + "\n".join(f"  {c}" for c in candidates[:10]) + more)

    h = _holding(mon, name)
    if h is None:
        # Steam answers "success" with no prices for a name it doesn't know,
        # so check first -- refresh_one would store a quote for the typo.
        ov = mon.client.price_overview(name, cache_s=mon.quote_cache_s)
        if ov is None and _steam_busy(mon):
            return STEAM_BUSY
        if not ov or not (ov["lowest_paise"] or ov["median_paise"]):
            return (f"Nothing you hold matches '{query}', and Steam has no "
                    f"listings or sales under that exact name. /holdings lists "
                    f"what you hold.")
    snap = mon.refresh_one(name)    # for an unheld item, served from that cache
    if snap is None:
        return f"{name}\nSteam returned no price data just now. Try again shortly."

    cost = (h["cost"] or 0) if h else 0
    plan = mon.strategy.build(
        snap, cost, qty=h["qty"] if h else 1,
        item_type=(h["item_type"] if h else None) or "other",
        month_bias=mon.calendar.month_bias(today.month),
        forecast=mon.forecast_for(name, snap.lowest_paise or snap.fair_value_paise(),
                                  h["item_type"] if h else None))
    plan.validate(mon.cfg)

    L = [name + (f"   x{plan.qty}" if h else "   (not held)"), ""]
    L.append(f"Lowest ask   {rs(snap.lowest_paise)}")
    L.append(f"24h median   {rs(snap.median_paise)}   vol {snap.volume_24h}")
    L.append(f"Fair value   {rs(plan.fair_value_paise)}")
    if snap.lowest_paise:
        net = net_from_buyer_price(snap.lowest_paise, mon.cfg)
        pl = f"  (P/L {(net - cost)/100:+,.2f})" if h and cost else ""
        L.append(f"Sell at ask  you net {rs(net)}{pl}")

    if h:
        L.append("")
        if cost:
            L.append(f"Cost basis   {rs(cost)} each")
            L.append(f"Break-even   list at {rs(plan.floor_list_paise)}"
                     + _gap(plan.floor_list_paise, snap.lowest_paise))
        else:
            L.append("Cost basis   Rs 0 (free drop) -- any sale is profit")

    L.append("")
    if plan.action in SELLABLE:
        net_t = net_from_buyer_price(plan.target_list_paise, mon.cfg)
        pl = f" (P/L {(net_t - cost)/100:+,.2f} each)" if h else ""
        L.append(f"LIST AT      {rs(plan.target_list_paise)} -> you net {rs(net_t)}{pl}")
    L.append(f"Verdict      {plan.action}, {plan.confidence:.0%} confidence")
    L.append(plan.rationale)

    L.append("")
    L.extend(_history_lines(mon, name, today))
    fc = plan.forecast
    if fc and fc.days >= 5:
        L.append(f"Trend        {fc.mu * 30 * 100:+.1f}%/month over {fc.days} days,"
                 f" volatility {fc.sigma * 100:.1f}%/day")
        lo, hi = fc.band(90)
        L.append(f"Next 90 days {rs(lo)} - {rs(hi)} (80% band)")
    if fc and h and cost and plan.floor_list_paise > (snap.lowest_paise or 0):
        L.append("Break-even odds " + "  ".join(
            f"{d}d {fc.p_touch(plan.floor_list_paise, d):.0%}" for d in HORIZONS))
    for d, move, why in (fc.spikes[-3:] if fc else []):
        L.append(f"Spike {d} {move * 100:+.0f}%: {why}")
    if h and h["source"] in ARMORY_2026_07:
        days = (today - ARMORY_2026_07_RELEASED).days
        L.append(f"Armory batch day {days}: {mon.calendar.decay_phase(days)} phase")
    nxt = mon.calendar.upcoming(today, scope=plan.item_type, limit=1)
    if nxt:
        e = nxt[0]
        L.append(f"Next catalyst {e.date} (in {(e.date - today).days}d): {e.name}")
    L.append("")
    L.append(steam_item_url(name))
    return "\n".join(L)


def sellable_report(mon: Monitor) -> str:
    """Items whose current ask clears break-even, best profit first."""
    held = {h["market_hash_name"]: h for h in mon.holdings()}
    if not held:
        return NO_HOLDINGS
    rows = []
    for r in mon.store.q("SELECT * FROM plan WHERE clears_floor=1"
                         " AND action IN ('list_now','list_patient')"):
        h = held.get(r["market_hash_name"])
        if h is None:
            continue
        cost = h["cost"] or 0
        net = net_from_buyer_price(r["target_list_paise"], mon.cfg)
        # The plan passed validate() when the sweep stored it. Re-check
        # against today's cost basis in case holdings were re-imported since.
        if net < cost:
            continue
        rows.append((net - cost, r, h, net))
    if not rows:
        return ("Nothing clears break-even at current asks.\n"
                "/holdings shows how far each item has to go.")

    rows.sort(key=lambda x: -x[0])
    L = [f"{len(rows)} item(s) clear break-even now:", ""]
    for pl, r, h, net in rows:
        flag = "SELL NOW  " if r["action"] == "list_now" else ""
        L.append(f"{flag}{short(r['market_hash_name'])}  x{h['qty']}")
        L.append(f"  list at {rs(r['target_list_paise'])} -> net {rs(net)}"
                 f" (P/L {pl/100:+,.2f} each)")
    L += ["", "From the last sweep. /price <item> re-checks live."]
    return "\n".join(L)


def portfolio_report(mon: Monitor) -> str:
    """Cost, liquidation value and planned net across all holdings."""
    held = mon.holdings()
    if not held:
        return NO_HOLDINGS
    plans = {r["market_hash_name"]: dict(r) for r in mon.store.q("SELECT * FROM plan")}
    quotes = _latest_quotes(mon)

    cost = at_ask = priced = 0
    planned = {"paid": 0, "free": 0}
    actions: dict[str, int] = {}
    for h in held:
        name, qty = h["market_hash_name"], h["qty"]
        cost += (h["cost"] or 0) * qty
        ask = (quotes.get(name) or {}).get("lowest_paise")
        if ask:
            at_ask += net_from_buyer_price(ask, mon.cfg) * qty
            priced += 1
        p = plans.get(name)
        if p:
            actions[p["action"]] = actions.get(p["action"], 0) + 1
            if p["action"] in SELLABLE:
                planned["paid" if h["cost"] else "free"] += \
                    net_from_buyer_price(p["target_list_paise"], mon.cfg) * qty
    total = planned["paid"] + planned["free"]
    last = max((p["updated_at"] for p in plans.values() if p["updated_at"]),
               default=None)

    L = [f"Portfolio: {len(held)} items, {sum(h['qty'] for h in held)} units",
         f"Last sweep   {last or 'never -- start the monitor'}", ""]
    L.append(f"Cost basis       {rs(cost)}")
    L.append(f"Sell all at ask  {rs(at_ask)}"
             + (f"  ({at_ask / cost:.0%} of cost)" if cost else ""))
    if priced < len(held):
        L.append(f"                 ({len(held) - priced} items have no ask yet)")
    L.append(f"Planned net      {rs(total)}  (sellable items at plan price)")
    if planned["free"] and cost:
        L.append(f"  paid items     {rs(planned['paid'])}"
                 f"  ({planned['paid'] / cost:.0%} of their cost)")
        L.append(f"  free drops     {rs(planned['free'])}")
    L.append(f"P/L vs cost      {(total - cost)/100:+,.2f}")
    if actions:
        L += ["", " | ".join(f"{a} {n}" for a, n in sorted(actions.items()))]

    if total < cost:
        verdict = "NOT YET"
    elif planned["paid"] < cost:
        # Say where the money comes from. Selling free drops to cover the
        # spend is a real option, but the paid items are still underwater.
        verdict = ("YES, but only by selling free drops -- "
                   "the paid items alone are still underwater")
    else:
        verdict = "YES"
    L += ["", "BREAK-EVEN: " + verdict]
    return "\n".join(L)


def holdings_report(mon: Monitor) -> str:
    """Every item: current ask against its break-even list price."""
    held = mon.holdings()
    if not held:
        return NO_HOLDINGS
    quotes = _latest_quotes(mon)
    paid, free = [], []
    for h in held:
        name, cost = h["market_hash_name"], h["cost"] or 0
        ask = (quotes.get(name) or {}).get("lowest_paise")
        if not cost:
            free.append((-(ask or 0), name, h["qty"], ask))
            continue
        floor = list_price_for_net(mon.strategy.floor_net(cost), mon.cfg)
        need = floor / ask if ask else float("inf")
        paid.append((need, name, h["qty"], ask, floor))

    L = []
    if paid:
        L.append("Paid for, closest to break-even first (Rs):")
        for need, name, qty, ask, floor in sorted(paid):
            gap = ("clears" if need <= 1 else
                   "no ask" if need == float("inf") else f"needs {need:.2f}x")
            L.append(f"{short(name)} x{qty}")
            L.append(f"  ask {_num(ask)} | break-even {_num(floor)} | {gap}")
    if free:
        if L:
            L.append("")
        L.append("Free drops (cost 0), most valuable first (Rs):")
        for _, name, qty, ask in sorted(free):
            L.append(f"{short(name)} x{qty}  ask {_num(ask)}")
    if not quotes:
        L += ["", "No quotes yet -- start the monitor, or /price <item>."]
    return "\n".join(L)


HORIZONS = (90, 180, 365)


def plan_report(mon: Monitor) -> str:
    """The whole sell plan, item by item, with the odds on everything that
    has to wait for the market -- the answer to "will this break even?"."""
    held = mon.holdings()
    if not held:
        return NO_HOLDINGS
    plans = {r["market_hash_name"]: dict(r) for r in mon.store.q("SELECT * FROM plan")}
    quotes = _latest_quotes(mon)
    H = mon.strategy.horizon_days
    now, later, idle = [], [], []
    cost_total = expected = 0.0
    for h in held:
        name, qty, cost = h["market_hash_name"], h["qty"], h["cost"] or 0
        cost_total += cost * qty
        p, ask = plans.get(name), (quotes.get(name) or {}).get("lowest_paise")
        if not p or not ask:
            idle.append(f"{short(name)} x{qty}: no market data yet")
            continue
        if p["action"] in SELLABLE:
            price = p["target_list_paise"]
        elif p["action"] == "unsellable":
            price = p["floor_list_paise"]           # rests at break-even
        else:
            idle.append(f"{short(name)} x{qty}: {p['action']} -- "
                        f"{(p['rationale'] or '')[:70]}")
            continue
        net = net_from_buyer_price(price, mon.cfg)
        fc = mon.forecast_for(name, ask, h["item_type"])
        expected += (fc.p_touch(price, H) if fc else float(price <= ask)) * net * qty
        pl = (net - cost) / 100
        if price <= ask:
            now.append((-pl, f"{short(name)} x{qty}: list {_num(price)} -> "
                             f"{_num(net)} ({pl:+,.2f} each)"))
        else:
            odds = "/".join(f"{fc.p_touch(price, d) if fc else 0:.0%}" for d in HORIZONS)
            later.append((price / ask,
                          f"{short(name)} x{qty}: list {_num(price)} (now {_num(ask)}, "
                          f"{price / ask:.2f}x) {pl:+,.2f} each; "
                          f"odds 90d/180d/1y {odds}"))
    L = [f"Sell plan: {len(held)} items, {sum(h['qty'] for h in held)} units, "
         f"horizon {H} days.",
         "Every price nets at least your cost after Steam's fee.", ""]
    if now:
        L.append(f"SELLS AT TODAY'S PRICES ({len(now)})")
        L += [t for _, t in sorted(now)]
        L.append("")
    if later:
        L.append(f"WAITS FOR THE MARKET ({len(later)}) -- fills only if it gets there")
        L += [t for _, t in sorted(later)]
        L.append("")
    if idle:
        L += ["NOT LISTED", *idle, ""]
    L.append(f"Cost of everything held   {rs(int(cost_total))}")
    L.append(f"Expected back in {H} days {rs(int(expected))}  (odds x net, summed)")
    L.append("Nothing is ever sold below cost, so selling can't add to a loss. "
             "Items that never reach break-even simply stay in your inventory.")
    return "\n".join(L)


ORDER_LABELS = [
    ("planned", "Planned (dry run -- nothing sent to Steam)"),
    ("confirm_pending", "Waiting for you: Steam app -> Confirmations"),
    ("listed", "Listed on Steam"),
    ("sold", "Sold"),
    ("failed", "Failed"),
]


def orders_report(mon: Monitor) -> str:
    """Sell orders by state, identical ones grouped, best profit first."""
    rows = mon.store.q("SELECT * FROM orders WHERE side='sell' ORDER BY id")
    if not rows:
        return ("No sell orders yet. The next price sweep plans them "
                "(dry run until SELL_MODE=live).")
    L = []
    for status, label in ORDER_LABELS:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        L.append(f"{label}: {len(group)}")
        agg: dict[tuple, list] = {}
        for r in group:
            a = agg.setdefault((r["market_hash_name"], r["price_paise"]),
                               [0, r["net_paise"], r["cost_basis_paise"]])
            a[0] += 1
        for (name, price), (n, net, cost) in sorted(
                agg.items(), key=lambda kv: kv[1][2] - kv[1][1]):
            L.append(f"  {short(name)} x{n}  {_num(price)} -> {_num(net)}"
                     f" ({(net - cost)/100:+,.2f} each)")
        L.append("")
    sold = [r for r in rows if r["status"] == "sold"]
    pl = sum(r["net_paise"] - r["cost_basis_paise"] for r in sold)
    L.append(f"Realised P/L: {pl/100:+,.2f} on {len(sold)} sold")
    return "\n".join(L)


def placed_summary(placed: list[dict], moved: list[dict], mode: str) -> str:
    """The Telegram note after a run places or reprices orders."""
    if mode == "live":
        head = ("Listed on Steam. Approve them in the Steam app: "
                "Steam Guard -> Confirmations -> select all.")
    else:
        head = "Dry run -- nothing sent to Steam. With SELL_MODE=live these get listed:"
    L = [head, ""]
    agg: dict[tuple, list] = {}
    for d in placed:
        a = agg.setdefault((d["name"], d["price"], d["ok"], d["note"]),
                           [0, d["net"], d["cost"]])
        a[0] += 1
    for (name, price, ok, note), (n, net, cost) in agg.items():
        line = f"{short(name)} x{n}: {rs(price)} -> you get {rs(net)} ({(net - cost)/100:+,.2f} each)"
        L.append(line if ok else f"{line}  FAILED: {note}")
    if moved:
        L += ["", f"{len(moved)} order(s) repriced upward."]
    L += ["", "/orders shows everything."]
    return "\n".join(L)


def alerts_report(mon: Monitor, arg: str = "") -> str:
    n = int(arg) if arg.isdigit() else 10
    rows = mon.store.q("SELECT ts, kind, market_hash_name, payload FROM alerts"
                       " ORDER BY id DESC LIMIT ?", (n,))
    if not rows:
        return "No alerts sent yet."
    L = [f"Last {len(rows)} alert(s):", ""]
    for r in rows:
        price = json.loads(r["payload"] or "{}").get("price") or ""
        L.append(f"{(r['ts'] or '')[:16].replace('T', ' ')}  {r['kind']}")
        L.append(f"  {short(r['market_hash_name'] or '-')}"
                 + (f"  list at {price}" if price else ""))
    return "\n".join(L)


def fees_report(cfg: WalletConfig, arg: str) -> str:
    p = parse_price_to_paise(arg) if arg else None
    if not p or p <= 0:
        return "Usage: /fees <buyer price in Rs>    e.g. /fees 90"
    fb = fee_amount(p, cfg)
    return "\n".join([
        f"Buyer pays {rs(p)}:",
        f"  Steam fee      {rs(fb.steam_fee)}",
        f"  Publisher fee  {rs(fb.publisher_fee)}",
        f"  YOU RECEIVE    {rs(fb.seller_receives)}  ({fb.effective_rate:.1%} fees)",
        "",
        f"To RECEIVE {rs(p)}, list at {rs(list_price_for_net(p, cfg))}.",
    ])


def events_report(cal: EventCalendar, today: Optional[dt.date] = None) -> str:
    today = today or dt.date.today()
    nxt = cal.upcoming(today, limit=5)
    recent = [e for e in cal.events
              if today - dt.timedelta(days=30) <= e.date <= today]
    L = ["Upcoming:"]
    L += [f"  {e.date} (in {(e.date - today).days}d) {e.name} [{e.effect}]"
          for e in nxt] or ["  nothing on the calendar"]
    L += ["", "Last 30 days:"]
    L += [f"  {e.date} {e.name} [{e.effect}]" for e in recent] or ["  nothing"]
    L += ["", f"Seasonal bias this month: {cal.month_bias(today.month):+.0%}"
              f" (weak prior, tie-breaker only)"]
    return "\n".join(L)


def status_report(mon: Monitor, now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now()
    held = mon.holdings()
    last = mon.store.one("SELECT MAX(updated_at) AS t FROM plan")["t"]
    q = mon.store.one("SELECT COUNT(*) AS n, MAX(fetched_at) AS t FROM quotes")
    sent = mon.store.one("SELECT COUNT(*) AS n FROM alerts WHERE ts >= ?",
                         (now.date().isoformat(),))["n"]
    L = [f"Holdings     {len(held)} items, {sum(h['qty'] for h in held)} units",
         f"Last sweep   {last or 'never'}",
         f"Last quote   {q['t'] or 'never'} ({q['n']} stored)",
         f"Alerts today {sent}",
         "Steam login  " + ("active (price history on)" if mon.store.get_meta("steam_login")
                            else "none -- /login to add one"),
         f"Sweep every  {mon.interval_s // 60} min"]
    if last:
        age = now - dt.datetime.fromisoformat(last)
        if age > dt.timedelta(seconds=2 * mon.interval_s):
            L += ["", f"WARNING: no sweep for {age.total_seconds() / 3600:.1f}h"
                      f" -- is `monitor` running?"]
    return "\n".join(L)
