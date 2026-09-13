"""
Alert channels. The output side of the monitor.

Deliberately NOT an execution layer. Every alert ends with a price to type
and a link to the item -- the final click and the Steam Guard mobile
confirmation stay with the human. Steam requires that confirmation for
every listing anyway; automating it would mean extracting the mobile
authenticator's identity_secret, which is the single most dangerous thing
you can do to a Steam account.
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional


def steam_item_url(market_hash_name: str) -> str:
    return ("https://steamcommunity.com/market/listings/730/"
            + urllib.parse.quote(market_hash_name))


def inventory_url(steamid64: str, assetid: Optional[str] = None) -> str:
    """Deep link that opens the inventory with the item preselected."""
    u = f"https://steamcommunity.com/profiles/{steamid64}/inventory/#730_2"
    if assetid:
        u += f"_{assetid}"
    return u


@dataclass
class Alert:
    kind: str          # spike | target_hit | floor_breach | summary
    title: str
    body: str
    url: str = ""
    price_to_type: str = ""

    def as_text(self) -> str:
        L = [f"[{self.kind.upper()}] {self.title}", self.body]
        if self.price_to_type:
            L.append(f"LIST AT: {self.price_to_type}")
        if self.url:
            L.append(self.url)
        return "\n".join(L)


class Channel:
    def send(self, alert: Alert) -> bool:
        raise NotImplementedError


class ConsoleChannel(Channel):
    def send(self, alert: Alert) -> bool:
        print("\n" + "=" * 62)
        print(alert.as_text())
        print("=" * 62)
        return True


class WindowsToastChannel(Channel):
    """Native Windows toast via PowerShell. No dependencies."""

    def send(self, alert: Alert) -> bool:
        title = alert.title.replace("'", "")
        body = (alert.body[:180] + (
            f"\nLIST AT {alert.price_to_type}" if alert.price_to_type else ""
        )).replace("'", "")
        ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
     [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x[0].AppendChild($t.CreateTextNode('{title}')) > $null
$x[1].AppendChild($t.CreateTextNode('{body}')) > $null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('CS2 Market').Show($n)
"""
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           check=False, capture_output=True, timeout=15)
            return True
        except Exception as e:
            print(f"  [toast] failed: {e}")
            return False


class TelegramChannel(Channel):
    """Push to your phone. Free, reliable, works when you're away."""

    def __init__(self, bot_token: str, chat_id: str):
        self.token, self.chat_id = bot_token, chat_id

    def send(self, alert: Alert) -> bool:
        text = alert.as_text()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=20) as r:
                return r.status == 200
        except Exception as e:
            print(f"  [telegram] failed: {e}")
            return False


class AlertRouter:
    def __init__(self, channels: list[Channel], store=None):
        self.channels = channels
        self.store = store
        self._seen: set[str] = set()

    def send(self, alert: Alert, dedupe_key: Optional[str] = None) -> None:
        if dedupe_key:
            if dedupe_key in self._seen:
                return
            self._seen.add(dedupe_key)
        for ch in self.channels:
            ch.send(alert)
        if self.store:
            import datetime as dt
            with self.store.tx() as c:
                c.execute(
                    "INSERT INTO alerts(ts,kind,market_hash_name,message,payload)"
                    " VALUES (?,?,?,?,?)",
                    (dt.datetime.now().isoformat(timespec="seconds"),
                     alert.kind, alert.title, alert.body,
                     json.dumps({"url": alert.url,
                                 "price": alert.price_to_type})),
                )
