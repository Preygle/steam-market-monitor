"""Command line entry point.  python -m steammkt.cli <command>"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import threading
import time
from pathlib import Path

import yaml

from .alerts import (AlertRouter, ConsoleChannel, TelegramChannel,
                     WindowsToastChannel)
from .bot import BOT_QUOTE_CACHE_S, COMMANDS, HANDLERS, TelegramBot, answer
from .client import SteamClient
from .costbasis import CostModel
from .events import EventCalendar
from .fees import WalletConfig, fee_amount
from .inventory import import_inventory
from .monitor import Monitor
from . import steamauth
from .executor import DryRunExecutor, SteamExecutor
from .ledger import Ledger
from .reports import placed_summary, plan_report, status_report
from .trader import Seller
from .store import Store
from .strategy import Strategy

CFG_PATH = Path("config/config.yaml")
EXAMPLE_PATH = Path("config/config.example.yaml")

# Environment overrides, so CI can build the config from repository secrets
# rather than from a config.yaml that must never be committed.
ENV_OVERRIDES = {
    "STEAM_ID64": (("steam", "steamid64"), str),
    "STEAM_SESSION_COOKIE": (("steam", "session_cookie"), str),
    "PASS_PRICE_PAISE": (("cost_basis", "pass_price_paise"), int),
    "PASSES_BOUGHT": (("cost_basis", "passes_bought"), int),
    "CREDITS_EARNED": (("cost_basis", "credits_earned"), int),
    "TELEGRAM_BOT_TOKEN": (("alerts", "telegram", "bot_token"), str),
    "TELEGRAM_CHAT_ID": (("alerts", "telegram", "chat_id"), str),
    "SELL_MODE": (("sell", "mode"), str),
}


def apply_env(cfg: dict, env) -> dict:
    """Overlay ENV_OVERRIDES onto a loaded config. Unset or blank variables
    are ignored, so a local run with nothing exported is unaffected."""
    for var, (path, cast) in ENV_OVERRIDES.items():
        val = (env.get(var) or "").strip()
        if not val:
            continue
        node = cfg
        for key in path[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[path[-1]] = cast(val)
    if (env.get("TELEGRAM_BOT_TOKEN") or "").strip() \
            and (env.get("TELEGRAM_CHAT_ID") or "").strip():
        cfg["alerts"]["telegram"]["enabled"] = True
    return cfg


def load_cfg() -> dict:
    path = CFG_PATH if CFG_PATH.exists() else EXAMPLE_PATH
    if path == EXAMPLE_PATH and not (os.environ.get("STEAM_ID64")
                                     or os.environ.get("TELEGRAM_BOT_TOKEN")):
        sys.exit(f"missing {CFG_PATH}. Copy config/config.example.yaml to it "
                 f"and fill it in (in CI: set the repository secrets).")
    return apply_env(yaml.safe_load(path.read_text(encoding="utf-8")), os.environ)


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


def telegram_cfg(cfg: dict) -> dict:
    return cfg.get("alerts", {}).get("telegram", {}) or {}


def build_router(cfg: dict, store: Store) -> AlertRouter:
    a = cfg.get("alerts", {})
    chans = []
    if a.get("console", True):
        chans.append(ConsoleChannel())
    if a.get("windows_toast"):
        chans.append(WindowsToastChannel())
    tg = telegram_cfg(cfg)
    if tg.get("enabled") and tg.get("bot_token"):
        chans.append(TelegramChannel(tg["bot_token"], tg["chat_id"]))
    return AlertRouter(chans, store)


def build_client(cfg: dict, store: Store) -> SteamClient:
    st = cfg["steam"]
    return SteamClient(store, currency=st.get("currency", 24),
                       session_cookie=st.get("session_cookie") or None,
                       per_minute=st.get("requests_per_minute", 15))


def build_monitor(cfg: dict, store: Store, router: AlertRouter) -> Monitor:
    wcfg = wallet_from(cfg)
    s = cfg.get("strategy", {})
    m = cfg.get("monitor", {})
    strat = Strategy(wcfg,
                     min_margin=s.get("min_margin", 0.0),
                     patience_premium=s.get("patience_premium", 0.06),
                     spike_threshold=s.get("spike_threshold", 0.15),
                     min_volume=s.get("min_volume", 1))
    return Monitor(store, build_client(cfg, store), strat, router,
                   EventCalendar(), wcfg,
                   interval_s=m.get("interval_seconds", 3600),
                   quote_cache_s=m.get("quote_cache_seconds", 1800))


def build_bot(cfg: dict) -> TelegramBot:
    """Opens its own Store, so its own SQLite connection -- call it inside
    the thread that will run the bot."""
    tg = telegram_cfg(cfg)
    store = Store()
    mon = build_monitor(cfg, store, AlertRouter([]))
    mon.quote_cache_s = BOT_QUOTE_CACHE_S
    return TelegramBot(tg["bot_token"], tg["chat_id"], mon)


def backfill_history(store: Store, client: SteamClient, names: list[str],
                     quiet: bool = False) -> int:
    """Pull Steam's full daily price series for each name. Needs a logged-in
    session cookie. Returns how many items got history."""
    ok = 0
    for i, n in enumerate(names, 1):
        h = client.price_history(n)
        if not h:
            if not quiet:
                print(f"  [{i}/{len(names)}] {n[:50]:<50} no history")
            continue
        # Steam gives hourly points for the last month: fold each day into one
        # volume-weighted median, so a day isn't just its last hour.
        days: dict[str, list[int]] = {}
        for row in h:
            try:
                d = dt.datetime.strptime(row["ts"][:11], "%b %d %Y").date().isoformat()
            except ValueError:
                continue
            w = max(int(row["volume"]), 1)
            a = days.setdefault(d, [0, 0, 0])
            a[0] += row["median_paise"] * w
            a[1] += w
            a[2] += int(row["volume"])
        with store.tx() as c:
            for d, (wsum, w, vol) in days.items():
                c.execute(
                    "INSERT OR REPLACE INTO price_history"
                    "(market_hash_name,ts,median_paise,volume,source)"
                    " VALUES (?,?,?,?,'pricehistory')", (n, d, wsum // w, vol))
        ok += 1
        if not quiet:
            print(f"  [{i}/{len(names)}] {n[:50]:<50} {len(h)} points")
    return ok


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
    res = import_inventory(store, build_client(cfg, store),
                           cfg["steam"]["steamid64"], cost_from(cfg),
                           only_armory=not args.all)
    print(res)


def cmd_history(args):
    """Backfill full daily price history (needs session_cookie)."""
    cfg = load_cfg()
    store = Store()
    client = build_client(cfg, store)
    names = [r["market_hash_name"] for r in store.q(
        "SELECT DISTINCT market_hash_name FROM holdings")]
    if not names:
        sys.exit("no holdings -- run import-inventory first")
    if not cfg["steam"].get("session_cookie"):
        print("WARNING: no session_cookie set. /market/pricehistory/ needs a "
              "logged-in session; falling back to snapshot accumulation only.\n")
    ok = backfill_history(store, client, names)
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
    build_monitor(cfg, store, build_router(cfg, store)).sweep()


def cmd_monitor(args):
    cfg = load_cfg()
    store = Store()
    tg = telegram_cfg(cfg)
    if (tg.get("enabled") and tg.get("commands", True)
            and tg.get("bot_token") and tg.get("chat_id")):
        threading.Thread(target=lambda: build_bot(cfg).poll_forever(),
                         name="telegram-bot", daemon=True).start()
    build_monitor(cfg, store, build_router(cfg, store)).run_forever()


def cmd_bot(args):
    """Answer Telegram commands without running the sweep loop."""
    cfg = load_cfg()
    tg = telegram_cfg(cfg)
    if not (tg.get("bot_token") and tg.get("chat_id")):
        sys.exit("set alerts.telegram.bot_token and chat_id in "
                 "config/config.yaml first")
    try:
        build_bot(cfg).poll_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


def cmd_ask(args):
    """Run any bot command here, no Telegram needed:  ask price nitro"""
    cfg = load_cfg()
    store = Store()
    mon = build_monitor(cfg, store, AlertRouter([]))
    mon.quote_cache_s = BOT_QUOTE_CACHE_S
    text = " ".join(args.words)
    if args.words and args.words[0].lstrip("/") in HANDLERS:
        text = "/" + text.lstrip("/")
    print(answer(mon, text))


def _age(iso: str) -> dt.timedelta:
    return dt.datetime.now() - dt.datetime.fromisoformat(iso)


def _import_due(store: Store) -> bool:
    last_try = store.get_meta("inventory_attempt")
    if last_try and _age(last_try) < dt.timedelta(hours=1):
        return False        # leave the inventory endpoint alone while it 429s
    last_ok = store.one("SELECT MAX(acquired_at) AS t FROM holdings")["t"]
    return not last_ok or _age(last_ok) > dt.timedelta(hours=24)


def _sweep_due(store: Store, interval_s: int) -> bool:
    last = store.one("SELECT MAX(updated_at) AS t FROM plan")["t"]
    return not last or _age(last).total_seconds() >= interval_s


def _daily_due(store: Store, key: str) -> bool:
    last = store.get_meta(key)
    return not last or _age(last) > dt.timedelta(hours=24)


def _real_steamid(s) -> bool:
    return bool(s) and str(s).isdigit()     # not the example's 7656119XXXX...


# Arguments the self-test passes to the commands that take one.
SELFTEST_ARGS = {"price": "nitro", "fees": "90", "alerts": "5"}


def _selftest(bot, mon) -> None:
    """Run every bot command and send each answer to Telegram, so the run
    log says pass/fail and the chat shows what the answers look like."""
    failed = []
    for name, _ in COMMANDS:
        if name in ("login", "logout"):      # these need the phone, not a test
            continue
        text = answer(mon, f"/{name} {SELFTEST_ARGS.get(name, '')}".strip())
        if text.startswith(f"/{name} failed"):
            failed.append(name)
        bot.reply(f"[test /{name}]
{text}")
    print("selftest:", "all ok" if not failed else "FAILED: " + ", ".join(failed))


def cmd_ci(args):
    """One scheduled GitHub Actions run: answer Telegram, import and sweep
    when due, keep answering until the listen window closes, then exit.

    A public repo's Actions logs are public, so this prints only counts --
    never item names, prices or P/L. The details go to Telegram."""
    started = time.monotonic()
    cfg = load_cfg()
    tg = telegram_cfg(cfg)
    if not (tg.get("bot_token") and tg.get("chat_id")):
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set")
    cfg["alerts"].update(console=False, windows_toast=False)
    store = Store()

    # The Steam login from /login, sealed in the cached state with STATE_KEY.
    vault = steamauth.open_vault(store, os.environ.get("STATE_KEY", ""))
    steam = steamauth.use_login(store, vault)
    if steam:
        cfg["steam"]["session_cookie"] = steam.cookie
        if not _real_steamid(cfg["steam"].get("steamid64")):
            cfg["steam"]["steamid64"] = str(steam.steamid)
    print("steam login:", "active" if steam else "none")

    mon = build_monitor(cfg, store, build_router(cfg, store))
    mon.quiet = True
    bot_mon = build_monitor(cfg, store, AlertRouter([]))
    bot_mon.quote_cache_s = BOT_QUOTE_CACHE_S
    bot = TelegramBot(tg["bot_token"], tg["chat_id"], bot_mon, vault=vault)
    bot.register_commands()

    handled = bot.listen(0)                 # whatever is already waiting

    if not _real_steamid(cfg["steam"].get("steamid64")):
        print("inventory: no SteamID yet -- set STEAM_ID64 or send /login")
    elif not (cfg.get("cost_basis") or {}).get("pass_price_paise"):
        print("inventory: cost basis secrets not set -- skipping")
    elif args.force_import or _import_due(store):
        store.set_meta("inventory_attempt",
                       dt.datetime.now().isoformat(timespec="seconds"))
        res = import_inventory(store, build_client(cfg, store),
                               cfg["steam"]["steamid64"], cost_from(cfg))
        print("inventory:", res.get("error")
              or f"{res['imported']} imported, {res.get('removed', 0)} gone")
        if res.get("hint"):
            print("  hint:", res["hint"])
    names = [r["market_hash_name"] for r in store.q(
        "SELECT DISTINCT market_hash_name FROM holdings")]
    if steam and names and (args.force_history or _daily_due(store, "history_at")):
        store.set_meta("history_at", dt.datetime.now().isoformat(timespec="seconds"))
        got = backfill_history(store, mon.client, names, quiet=True)
        print(f"price history: {got} of {len(names)} items")
    plans = []
    if args.force_sweep or _sweep_due(store, mon.interval_s):
        plans = mon.sweep()

    # Sell: dry run unless SELL_MODE=live, and live needs the Steam login.
    sell = cfg.get("sell") or {}
    mode = sell.get("mode", "dry_run")
    if mode == "live" and not steam:
        print("sell: SELL_MODE=live needs a Steam login (/login) -- dry run")
        mode = "dry_run"
    executor = (SteamExecutor(mon.client.session, str(cfg["steam"]["steamid64"]),
                              cfg["steam"].get("currency", 24), mon.client.rl)
                if mode == "live" else DryRunExecutor())
    seller = Seller(store, Ledger(store), executor, mon.cfg,
                    max_per_run=int(sell.get("max_per_run", 10)),
                    rest_at_floor=bool(sell.get("rest_at_floor", True)),
                    allow_subsidy=bool(sell.get("allow_subsidy", False)))
    if mode == "live":
        print("orders reconciled:", seller.reconcile())
    if plans:
        moved = seller.reprice(plans)
        placed = seller.run(plans)
        print(f"sell ({mode}): {len(placed)} new, {len(moved)} repriced")
        if placed or moved:
            bot.reply(placed_summary(placed, moved, mode))
    # The full per-item plan, once a day.
    if plans and _daily_due(store, "plan_sent_at"):
        store.set_meta("plan_sent_at", dt.datetime.now().isoformat(timespec="seconds"))
        bot.reply(plan_report(bot_mon))

    if args.ping:
        bot.reply("Running in GitHub Actions.\n\n" + status_report(bot_mon))

    if args.selftest:
        _selftest(bot, bot_mon)

    left = args.listen_seconds - (time.monotonic() - started)
    handled += bot.listen(max(0.0, left))
    print(f"telegram: {handled} update(s) handled")

    # Keep the cached state small: stale HTTP bodies are most of it.
    with store.tx() as c:
        c.execute("DELETE FROM http_cache WHERE fetched_at < ?",
                  (time.time() - 86400,))
    store.conn.execute("VACUUM")


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
    sub.add_parser("monitor", help="run the monitor loop forever "
                   "(plus Telegram commands, if enabled)") \
        .set_defaults(fn=cmd_monitor)
    sub.add_parser("bot", help="answer Telegram commands only") \
        .set_defaults(fn=cmd_bot)

    c = sub.add_parser("ask", help="run a bot command locally, "
                       "e.g. ask price nitro | ask sellable | ask help")
    c.add_argument("words", nargs="*")
    c.set_defaults(fn=cmd_ask)

    c = sub.add_parser("ci", help="one scheduled GitHub Actions run "
                       "(config from environment secrets)")
    c.add_argument("--listen-seconds", type=float, default=480,
                   help="answer Telegram for this long before exiting")
    c.add_argument("--force-sweep", action="store_true")
    c.add_argument("--force-import", action="store_true")
    c.add_argument("--force-history", action="store_true")
    c.add_argument("--ping", action="store_true",
                   help="send a status message to Telegram")
    c.add_argument("--selftest", action="store_true",
                   help="run every bot command and send the output to Telegram")
    c.set_defaults(fn=cmd_ci)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
