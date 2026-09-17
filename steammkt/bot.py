"""
Telegram command bot: ask the monitor about your items from your phone.

    /price nitro   ->  current ask, what you'd net, break-even, what to list at
    /login         ->  a QR to approve in the Steam app (unlocks price history)

Read-only as far as your items go: it answers with prices and plans and
never lists, moves or sells anything. It answers ONLY the configured
chat_id -- a Telegram bot is public, and anyone who finds its username can
message it.

Uses long polling (getUpdates), so it needs no public URL or webhook: it
runs as a scheduled GitHub Actions job, one listen window at a time.
"""
from __future__ import annotations

import json
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from . import steamauth
from .monitor import Monitor
from .reports import (alerts_report, events_report, fees_report,
                      holdings_report, orders_report, plan_report,
                      portfolio_report, price_report, sellable_report,
                      status_report)

TELEGRAM_LIMIT = 4096
# /price is a live question: re-fetch a quote older than this rather than
# serve the monitor's half-hour cache.
BOT_QUOTE_CACHE_S = 300
# How long a /login QR is waited on before asking for a fresh one. A CI run
# stretches its listen window to cover it.
LOGIN_WINDOW_S = 240

# Registered with Telegram at startup, so they show up in the "/" menu.
COMMANDS = [
    ("price", "live price, break-even and what to list at. /price nitro"),
    ("plan", "the full sell plan: list price and break-even odds per item"),
    ("sellable", "items that clear break-even right now"),
    ("portfolio", "cost, current value and P/L of everything held"),
    ("holdings", "every item: current ask vs break-even"),
    ("orders", "sell orders: planned, awaiting confirmation, listed, sold"),
    ("alerts", "recent alerts. /alerts 20"),
    ("fees", "what you receive for a price. /fees 90"),
    ("events", "upcoming and recent market events"),
    ("status", "is the monitor running, when it last swept"),
    ("login", "log the bot into Steam by QR (unlocks price history)"),
    ("logout", "forget the Steam login"),
    ("help", "list commands"),
]


def help_text() -> str:
    L = ["Commands:"]
    L += [f"/{c} - {d}" for c, d in COMMANDS]
    L += ["", "Or just send an item name (e.g. nitro) to get its price.",
          "The Menu button beside the message box lists these too.",
          "Read-only: nothing here lists or sells anything."]
    return "\n".join(L)


def _telegram_only(mon: Monitor, arg: str) -> str:
    return "Send this to the bot in Telegram -- the Steam login needs your phone."


HANDLERS: dict[str, Callable[[Monitor, str], str]] = {
    "price": price_report,
    "plan": lambda mon, arg: plan_report(mon),
    "sellable": lambda mon, arg: sellable_report(mon),
    "portfolio": lambda mon, arg: portfolio_report(mon),
    "holdings": lambda mon, arg: holdings_report(mon),
    "orders": lambda mon, arg: orders_report(mon),
    "alerts": alerts_report,
    "fees": lambda mon, arg: fees_report(mon.cfg, arg),
    "events": lambda mon, arg: events_report(mon.calendar),
    "status": lambda mon, arg: status_report(mon),
    "login": _telegram_only,
    "logout": _telegram_only,
    "help": lambda mon, arg: help_text(),
    "start": lambda mon, arg: help_text(),   # Telegram sends this on first contact
}


def _command(text: str) -> Optional[str]:
    """'/price@MyBot nitro' -> 'price'; None for plain text."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    return text.split()[0][1:].split("@", 1)[0].lower()


def answer(mon: Monitor, text: str) -> str:
    """One message in, one reply out. A bare item name is a price query."""
    text = text.strip()
    if not text:
        return help_text()
    cmd = _command(text)
    if cmd is None:
        cmd, arg = "price", text
    else:
        arg = text.partition(" ")[2]
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
                 api: Optional[Callable[..., dict]] = None,
                 upload: Optional[Callable[..., dict]] = None,
                 vault: Optional[steamauth.Vault] = None, auth=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.mon = mon
        self.api = api or self._http
        self.upload = upload or self._http_upload
        self.vault = vault                  # None: /login is refused
        self.auth = auth or steamauth
        self.token_ok = True          # cleared on a 404: the token is wrong
        self.login: Optional[steamauth.QrSession] = None
        self.login_deadline = 0.0
        self.offset = 0

    def _http(self, method: str, **params) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        wait = int(params.get("timeout", 0)) + 20
        with urllib.request.urlopen(url, data=data, timeout=wait) as r:
            return json.loads(r.read().decode())

    def _http_upload(self, method: str, fields: dict, file_field: str,
                     filename: str, data: bytes) -> dict:
        import requests
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}",
                              data=fields, timeout=30,
                              files={file_field: (filename, data, "image/png")})
        except requests.RequestException as e:
            # requests puts the URL -- and so the token -- in its messages.
            raise RuntimeError(f"telegram {method} failed: {type(e).__name__}") from None
        return r.json()

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
        cmd = _command(text)
        if cmd == "login":
            self.start_login()
        elif cmd == "logout":
            steamauth.forget_login(self.mon.store)
            self.reply("Forgot the Steam login. To revoke it on Steam's side too, "
                       f"remove '{steamauth.DEVICE_NAME}' at "
                       "https://store.steampowered.com/account/authorizeddevices")
        else:
            self.reply(answer(self.mon, text))

    def reply(self, text: str) -> None:
        for part in split_message(text):
            self.api("sendMessage", chat_id=self.chat_id, text=part or "(empty)",
                     disable_web_page_preview="true")

    # ---- Steam QR login -------------------------------------------------
    def start_login(self) -> None:
        if self.vault is None:
            self.reply("Steam login is off: add a STATE_KEY repository secret (any "
                       "long random string -- it encrypts the login at rest), then "
                       "send /login again.")
            return
        try:
            self.login = self.auth.begin_qr()
        except Exception as e:
            self.reply(f"Steam wouldn't start a login: {e}")
            return
        self.login_deadline = time.monotonic() + LOGIN_WINDOW_S
        self._send_qr()

    def _send_qr(self) -> None:
        caption = (
            "Steam login for this bot. In the Steam app: Steam Guard (shield) -> "
            f"scan this QR, then approve '{steamauth.DEVICE_NAME}'.\n"
            "Steam on this same phone? Open this chat on another screen "
            "(Telegram Desktop/Web) to scan it.\n"
            f"Valid for about {max(LOGIN_WINDOW_S, 0) // 60} minutes.")
        self.upload("sendPhoto", {"chat_id": self.chat_id, "caption": caption},
                    "photo", "steam-login.png", steamauth.qr_png(self.login.challenge_url))

    def _poll_login(self) -> None:
        if self.login is None:
            return
        if time.monotonic() > self.login_deadline:
            self.login = None
            self.reply("The Steam login QR expired. Send /login for a new one.")
            return
        try:
            result = self.auth.poll(self.login)
        except Exception as e:
            self.login = None
            self.reply(f"Steam login failed: {e}\nSend /login to try again.")
            return
        if result is None:
            if self.login.rotated:
                self.login.rotated = False
                self._send_qr()
            return
        self.login = None
        steamauth.save_login(self.mon.store, self.vault, result)
        self.reply(f"Logged in to Steam as {result.account_name or 'your account'}. "
                   "Daily price history starts with the next run. "
                   "/logout forgets the login.")

    # ---- polling ----------------------------------------------------------
    def register_commands(self) -> None:
        """Fill Telegram's command menu -- the Menu button beside the message
        box, and the list that pops up on "/".

        Registered for the default scope and for private chats, and this
        chat's menu button is pinned to "commands", so a client that cached
        an empty menu shows it after a restart."""
        commands = json.dumps([{"command": c, "description": d} for c, d in COMMANDS])
        try:
            for scope in ({"type": "default"}, {"type": "all_private_chats"}):
                self.api("setMyCommands", commands=commands, scope=json.dumps(scope))
            self.api("setChatMenuButton", chat_id=self.chat_id,
                     menu_button=json.dumps({"type": "commands"}))
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
            if e.code == 404:
                # api.telegram.org answers 404 for a token it doesn't know.
                self.token_ok = False
                print("  [bot] 404: Telegram doesn't recognise this bot token "
                      "-- check the TELEGRAM_BOT_TOKEN secret.")
                return 0
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

    def _wait(self, left: float) -> int:
        """Long-poll length: short while a login is pending, so Steam gets
        polled at the interval it asked for."""
        cap = self.login.interval if self.login else 50
        return int(min(cap, max(left, 0)))

    def listen(self, seconds: float) -> int:
        """Answer messages for `seconds` (one CI run's window; 0 = just what
        is already waiting), then confirm them. Stays past the window while a
        /login QR is live. Returns updates handled."""
        deadline = time.monotonic() + seconds
        handled = 0
        while True:
            # Recomputed each pass: once the login resolves, the window
            # snaps back to what the caller asked for.
            end = max(deadline, self.login_deadline) if self.login else deadline
            handled += self._poll(self._wait(end - time.monotonic()))
            self._poll_login()
            if not self.token_ok:
                break
            if not self.login and deadline - time.monotonic() < 1:
                break
        self.confirm()
        return handled

    def poll_forever(self) -> None:
        self.register_commands()
        print("telegram bot listening -- send /help to it.")
        while self.token_ok:
            self._poll(self._wait(50))
            self._poll_login()
