"""Event calendar + spike attribution."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class Event:
    date: dt.date
    name: str
    category: str
    effect: str
    scope: list[str]
    note: str = ""
    confidence: str = "medium"


class EventCalendar:
    def __init__(self, path: str | Path = "config/events.yaml"):
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.events = [
            Event(
                date=e["date"] if isinstance(e["date"], dt.date)
                else dt.date.fromisoformat(str(e["date"])),
                name=e["name"], category=e["category"], effect=e["effect"],
                scope=e.get("scope", []), note=e.get("note", ""),
                confidence=e.get("confidence", "medium"),
            )
            for e in raw["events"]
        ]
        self.events.sort(key=lambda e: e.date)
        self.seasonality = raw.get("seasonality", {})
        self.decay_model = raw.get("decay_model", {})

    def near(self, when: dt.date, window_days: int = 5,
             scope: Optional[str] = None) -> list[Event]:
        """Events within +/- window_days of a date, optionally scope-filtered."""
        out = []
        for e in self.events:
            if abs((e.date - when).days) <= window_days:
                if scope and e.scope and scope not in e.scope and "all" not in e.scope:
                    continue
                out.append(e)
        return out

    def upcoming(self, after: dt.date, scope: Optional[str] = None,
                 limit: int = 3) -> list[Event]:
        """The next events strictly after a date, optionally scope-filtered."""
        out = [e for e in self.events if e.date > after and (
            not scope or not e.scope or scope in e.scope or "all" in e.scope)]
        return out[:limit]

    def attribute(self, when: dt.date, pct_move: float,
                  scope: Optional[str] = None, window_days: int = 5) -> str:
        """Explain a price move. Returns a human-readable attribution."""
        cands = self.near(when, window_days, scope)
        if not cands:
            return "unexplained"
        direction_up = pct_move > 0
        scored = []
        for e in cands:
            # An event explains a move if its expected direction matches.
            expected_up = e.effect in ("supply_down", "demand_up")
            match = (expected_up == direction_up)
            dist = abs((e.date - when).days)
            score = (2 if match else 0) - dist * 0.1
            score += {"high": 0.5, "medium": 0.2, "low": 0.0}.get(e.confidence, 0)
            scored.append((score, e, match))
        scored.sort(key=lambda x: -x[0])
        best_score, best, match = scored[0]
        tag = "consistent with" if match else "coincides with (direction mismatch)"
        return f"{tag} {best.name} ({best.date}, {best.effect})"

    def month_bias(self, month: int) -> float:
        return float(self.seasonality.get("monthly_bias", {}).get(month, 0.0))

    def decay_phase(self, days_since_release: int) -> str:
        for ph in self.decay_model.get("phases", []):
            lo, hi = ph["days"]
            if lo <= days_since_release < hi:
                return ph["label"]
        return "maturity"
