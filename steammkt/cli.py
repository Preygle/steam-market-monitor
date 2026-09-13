"""Command line entry point.  python -m steammkt.cli <command>"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

from .alerts import (AlertRouter, ConsoleChannel, TelegramChannel,
                     WindowsToastChannel, steam_item_url)
from .client import SteamClient
from .costbasis import CostModel
from .events import EventCalendar
from .fees import WalletConfig, fee_amount, list_price_for_net, net_from_buyer_price
from .inventory import import_inventory
from .monitor import Monitor
from .store import Store
from .strategy import MarketSnapshot, Strategy, evaluate_portfolio

CFG_PATH = Path("config/config.yaml")


def load_cfg() -> dict:
    if not CFG_PATH.exists():
        sys.exit(f"missing {CFG_PATH}. Copy config/config.example.yaml to it "
                 f"and fill it in.")
    return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))


def wallet_from(cfg: dict) -> WalletConfig:
    w = cfg.get("wallet", {})
    return WalletConfig(
        fee_percent=w.get("fee_percent", 0.05),
        publisher_fee=w.get("publisher_fee", 0.10),
        fee_minimum=w.get("fee_minimum", 1),
        min_listing_price=w.get("min_listing_price", 100),
    )


def cost_from(cfg: dict) -> CostModel:
    cb = cfg.get("cost_basis", {})
    if not cb.get("pass_price_paise") or not cb.get("passes_bought"):
        sys.exit("cost_basis.pass_price_paise and passes_bought must be set "
                 "in config/config.yaml -- without them there is no floor "
                 "and I will not generate a sell plan.")
    return CostModel(
        pass_price_paise=cb["pass_price_paise"],
        passes_bought=cb["passes_bought"],
        credits_earned=cb.get("credits_earned"),
    )


def build_router(cfg: dict, store: Store) -> AlertRouter:
    a = cfg.get("alerts", {})
    chans = []
    if a.get("console", True):
        chans.append(ConsoleChannel())
    if a.get("windows_toast"):
        chans.append(WindowsToastChannel())
    tg = a.get("telegram", {})
    if tg.get("enabled") and tg.get("bot_token"):
        chans.append(TelegramChannel(tg["bot_token"], tg["chat_id"]))
    return AlertRouter(chans, store)


# ---------------------------------------------------------------- commands
def cmd_fees(args):
    cfg = wallet_from(load_cfg() if CFG_PATH.exists() else {})
    print(f"{'buyer pays':>14} {'you net':>12} {'fees':>10} {'eff %':>7}")
    print("-" * 46)
    for rs in (1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 5000):
        fb = fee_amount(rs * 100, cfg)
        print(f"{'Rs '+format(rs,','):>14} {fb.seller_receives/100:>12,.2f} "
              f"{fb.total_fees/100:>10,.2f} {fb.effective_rate*100:>6.1f}%")


def cmd_calibrate(args):
    """Verify our fee math against what Steam's own sell dialog shows."""
    cfg = wallet_from(load_cfg() if CFG_PATH.exists() else {})
    p = int(round(args.price * 100))
    fb = fee_amount(p, cfg)
    print(f"For a listing where the BUYER pays Rs {p/100:,.2f}:")
    print(f"  Steam fee      Rs {fb.steam_fee/100:>10,.2f}")
    print(f"  Publisher fee  Rs {fb.publisher_fee/100:>10,.2f}")
    print(f"  YOU RECEIVE    Rs {fb.seller_receives/100:>10,.2f}")
    print()
    print("Now open that item's Sell dialog on Steam, type the same buyer")
    print("price, and compare 'You receive'. If they differ, adjust")
    print("wallet.fee_minimum / min_listing_price in config.yaml until they")
    print("match. Everything downstream depends on this being exact.")


def cmd_costbasis(args):
    cfg = load_cfg()
    print(cost_from(cfg).summary())


def cmd_import(args):
    cfg = load_cfg()
    store = Store()
    client = SteamClient(store,
                         currency=cfg["steam"].get("currency", 24),
                         session_cookie=cfg["steam"].get("session_cookie") or None,
                         per_minute=cfg["steam"].get("requests_per_minute", 15))
    res = import_inventory(store, client, cfg["steam"]["steamid64"],
                           cost_from(cfg), only_armory=not args.all)
    print(res)


def cmd_history(args):
    """Backfill full daily price history (needs session_cookie)."""
    cfg = load_cfg()
    store = Store()
    client = SteamClient(store,
                         currency=cfg["steam"].get("currency", 24),
                         session_cookie=cfg["steam"].get("session_cookie") or None,
                         per_minute=cfg["steam"].get("requests_per_minute", 15))
    names = [r["market_hash_name"] for r in store.q(
        "SELECT DISTINCT market_hash_name FROM holdings")]
    if not names:
        sys.exit("no holdings -- run import-inventory first")
    if not cfg["steam"].get("session_cookie"):
        print("WARNING: no session_cookie set. /market/pricehistory/ needs a "
              "logged-in session; falling back to snapshot accumulation only.\n")
    ok = 0
    for i, n in enumerate(names, 1):
        h = client.price_history(n)
        if not h:
            print(f"  [{i}/{len(names)}] {n[:50]:<50} no history")
            continue
        with store.tx() as c:
            for row in h:
                try:
                    d = dt.datetime.strptime(row["ts"][:11], "%b %d %Y").date()
                except ValueError:
                    continue
                c.execute(
                    "INSERT OR REPLACE INTO price_history"
                    "(market_hash_name,ts,median_paise,volume,source)"
                    " VALUES (?,?,?,?,'pricehistory')",
                    (n, d.isoformat(), row["median_paise"], row["volume"]))
        ok += 1
        print(f"  [{i}/{len(names)}] {n[:50]:<50} {len(h)} points")
    print(f"\n{ok}/{len(names)} items have full history")


def cmd_analyze(args):
    """Attribute historical spikes to calendar events."""
    store = Store()
    cal = EventCalendar()
    names = [r["market_hash_name"] for r in store.q(
        "SELECT DISTINCT market_hash_name FROM price_history")]
    if not names:
        sys.exit("no history -- run fetch-history first")
    for n in names[: args.limit]:
        rows = store.q(
            "SELECT ts,median_paise FROM price_history WHERE market_hash_name=?"
            " AND source='pricehistory' ORDER BY ts", (n,))
        if len(rows) < 10:
            continue
        print(f"\n{n}")
        prev = rows[0]["median_paise"]
        for r in rows[1:]:
            cur = r["median_paise"]
            if not prev:
                prev = cur
                continue
            move = (cur - prev) / prev
            if abs(move) >= args.threshold:
                d = dt.date.fromisoformat(r["ts"])
                why = cal.attribute(d, move)
                print(f"  {r['ts']}  {move*100:+6.1f}%  {why}")
            prev = cur


def cmd_plan(args):
    cfg = load_cfg()
    store = Store()
    wcfg = wallet_from(cfg)
    s = cfg.get("strategy", {})
    strat = Strategy(wcfg,
                     min_margin=s.get("min_margin", 0.0),
                     patience_premium=s.get("patience_premium", 0.06),
                     spike_threshold=s.get("spike_threshold", 0.15),
                     min_volume=s.get("min_volume", 1))
    client = SteamClient(store, currency=cfg["steam"].get("currency", 24),
                         session_cookie=cfg["steam"].get("session_cookie") or None,
                         per_minute=cfg["steam"].get("requests_per_minute", 15))
    cal = EventCalendar()
    mon = Monitor(store, client, strat, build_router(cfg, store), cal, wcfg)
    mon.sweep()


def cmd_monitor(args):
    cfg = load_cfg()
    store = Store()
    wcfg = wallet_from(cfg)
    s = cfg.get("strategy", {})
    strat = Strategy(wcfg,
                     min_margin=s.get("min_margin", 0.0),
                     patience_premium=s.get("patience_premium", 0.06),
                     spike_threshold=s.get("spike_threshold", 0.15),
                     min_volume=s.get("min_volume", 1))
    client = SteamClient(store, currency=cfg["steam"].get("currency", 24),
                         session_cookie=cfg["steam"].get("session_cookie") or None,
                         per_minute=cfg["steam"].get("requests_per_minute", 15))
    m = cfg.get("monitor", {})
    mon = Monitor(store, client, strat, build_router(cfg, store),
                  EventCalendar(), wcfg,
                  interval_s=m.get("interval_seconds", 3600),
                  quote_cache_s=m.get("quote_cache_seconds", 1800))
    mon.run_forever()


def main():
    ap = argparse.ArgumentParser(prog="steammkt")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fees", help="show the fee table").set_defaults(fn=cmd_fees)

    c = sub.add_parser("calibrate", help="verify fee math vs Steam's dialog")
    c.add_argument("--price", type=float, required=True, help="buyer price in Rs")
    c.set_defaults(fn=cmd_calibrate)

    sub.add_parser("costbasis", help="show your per-item cost basis") \
        .set_defaults(fn=cmd_costbasis)

    c = sub.add_parser("import-inventory", help="pull inventory into the db")
    c.add_argument("--all", action="store_true",
                   help="import everything, not just the Armory batch")
    c.set_defaults(fn=cmd_import)

    sub.add_parser("fetch-history", help="backfill daily price history") \
        .set_defaults(fn=cmd_history)

    c = sub.add_parser("analyze", help="attribute price spikes to events")
    c.add_argument("--threshold", type=float, default=0.10)
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(fn=cmd_analyze)

    sub.add_parser("plan", help="build and print the sell plan once") \
        .set_defaults(fn=cmd_plan)
    sub.add_parser("monitor", help="run the monitor loop forever") \
        .set_defaults(fn=cmd_monitor)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
