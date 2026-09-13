"""
Telegram command bot: ask the monitor about your items from your phone.

    /price nitro   ->  current ask, what you'd net, break-even, what to list at

Read-only. It answers with prices and plans; it never lists, moves or sells
anything. It answers ONLY the configured chat_id -- a Telegram bot is
public, and anyone who finds its username can message it.

Uses long polling (getUpdates), so it needs no public URL or webhook and
runs fine on a home PC behind a router.
"""
from __future__ import annotations

import json
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from .monitor import Monitor
from .reports import (alerts_report, events_report, fees_report,
                      holdings_report, portfolio_report, price_report,
                      sellable_report, status_report)

TELEGRAM_LIMIT = 4096
# /price is a live question: re-fetch a quote older than this rather than
# serve the monitor's half-hour cache.
BOT_QUOTE_CACHE_S = 300

# Registered with Telegram at startup, so they show up in the "/" menu.
COMMANDS = [
    ("price", "live price, break-even and what to list at. /price nitro"),
    ("sellable", "items that clear break-even right now"),
    ("portfolio", "cost, current value and P/L of everything held"),
    ("holdings", "every item: current ask vs break-even"),
    ("alerts", "recent alerts. /alerts 20"),
    ("fees", "what you receive for a price. /fees 90"),
    ("events", "upcoming and recent market events"),
    ("status", "is the monitor running, when it last swept"),
    ("help", "list commands"),
]


def help_text() -> str:
    L = ["Commands:"]
    L += [f"/{c} - {d}" for c, d in COMMANDS]
    L += ["", "Or just send an item name (e.g. nitro) to get its price.",
          "Read-only: nothing here lists or sells anything."]
    return "\n".join(L)


HANDLERS: dict[str, Callable[[Monitor, str], str]] = {
    "price": price_report,
    "sellable": lambda mon, arg: sellable_report(mon),
    "portfolio": lambda mon, arg: portfolio_report(mon),
    "holdings": lambda mon, arg: holdings_report(mon),
    "alerts": alerts_report,
    "fees": lambda mon, arg: fees_report(mon.cfg, arg),
    "events": lambda mon, arg: events_report(mon.calendar),
    "status": lambda mon, arg: status_report(mon),
    "help": lambda mon, arg: help_text(),
    "start": lambda mon, arg: help_text(),   # Telegram sends this on first contact
}


def answer(mon: Monitor, text: str) -> str:
    """One message in, one reply out. A bare item name is a price query."""
    text = text.strip()
    if not text:
        return help_text()
    if text.startswith("/"):
        head, _, arg = text.partition(" ")
        cmd = head[1:].split("@", 1)[0].lower()    # "/price@MyBot" in groups
    else:
        cmd, arg = "price", text
    fn = HANDLERS.get(cmd)
    if fn is None:
        return f"Unknown command /{cmd}\n\n{help_text()}"
    try:
        return fn(mon, arg.strip())
    except Exception as e:
        # Includes a no-loss violation raised by validate(): the user gets
        # the error, never the price that tripped it.
        traceback.print_exc()
        return f"/{cmd} failed: {type(e).__name__}: {e}"


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries into chunks Telegram will accept."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:              # one enormous line: hard-cut it
            if cur:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        add = len(line) + (1 if cur else 0)
        if cur and size + add > limit:
            chunks.append("\n".join(cur))
            cur, size, add = [], 0, len(line)
        cur.append(line)
        size += add
    if cur or not chunks:
        chunks.append("\n".join(cur))
    return chunks


class TelegramBot:
    def __init__(self, token: str, chat_id, mon: Monitor,
                 api: Optional[Callable[..., dict]] = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.mon = mon
        self.api = api or self._http
        self.offset = 0

    def _http(self, method: str, **params) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        wait = int(params.get("timeout", 0)) + 20
        with urllib.request.urlopen(url, data=data, timeout=wait) as r:
            return json.loads(r.read().decode())

    def process(self, update: dict) -> None:
        self.offset = max(self.offset, update["update_id"] + 1)
        msg = update.get("message") or {}
        text = msg.get("text")
        if not text:
            return
        chat = str(msg.get("chat", {}).get("id"))
        if chat != self.chat_id:
            print(f"  [bot] ignored a message from chat {chat} "
                  f"(not alerts.telegram.chat_id)")
            return
        self.reply(answer(self.mon, text))

    def reply(self, text: str) -> None:
        for part in split_message(text):
            self.api("sendMessage", chat_id=self.chat_id, text=part or "(empty)",
                     disable_web_page_preview="true")

    def register_commands(self) -> None:
        """Show the commands in Telegram's "/" menu."""
        try:
            self.api("setMyCommands", commands=json.dumps(
                [{"command": c, "description": d} for c, d in COMMANDS]))
        except Exception as e:
            print(f"  [bot] could not register the command menu: {e}")

    def poll_once(self, timeout: int = 0) -> int:
        """Answer whatever is waiting, long-polling up to `timeout` seconds.
        Returns how many updates arrived."""
        res = self.api("getUpdates", offset=self.offset, timeout=timeout,
                       allowed_updates=json.dumps(["message"]))
        updates = res.get("result", [])
        for u in updates:
            self.process(u)
        return len(updates)

    def _poll(self, timeout: int) -> int:
        """poll_once, with errors logged and backed off rather than raised."""
        try:
            return self.poll_once(timeout)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                print("  [bot] 409: another copy of this bot is polling "
                      "(a local `bot`/`monitor`, or an overlapping CI run).")
            else:
                print(f"  [bot] telegram HTTP {e.code}")
            time.sleep(30 if e.code == 409 else 5)
        except Exception as e:
            print(f"  [bot] {type(e).__name__}: {e}")
            time.sleep(5)
        return 0

    def confirm(self) -> None:
        """Tell Telegram everything below self.offset is handled, so the next
        process to poll -- the next CI run -- doesn't answer it again."""
        if self.offset:
            try:
                self.api("getUpdates", offset=self.offset, timeout=0)
            except Exception as e:
                print(f"  [bot] could not confirm updates: {e}")

    def listen(self, seconds: float) -> int:
        """Answer messages for `seconds` (one CI run's window; 0 = just what
        is already waiting), then confirm them. Returns updates handled."""
        deadline = time.monotonic() + seconds
        handled = 0
        while True:
            left = deadline - time.monotonic()
            handled += self._poll(int(min(50, max(left, 0))))
            if deadline - time.monotonic() < 1:
                break
        self.confirm()
        return handled

    def poll_forever(self) -> None:
        self.register_commands()
        print("telegram bot listening -- send /help to it.")
        while True:
            self._poll(50)
