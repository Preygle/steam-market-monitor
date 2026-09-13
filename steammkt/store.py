"""SQLite persistence. One file, no server, survives reboots."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
    asset_id        TEXT PRIMARY KEY,
    market_hash_name TEXT NOT NULL,
    class_id        TEXT,
    instance_id     TEXT,
    item_type       TEXT,      -- sticker | skin | charm | case | other
    rarity          TEXT,
    exterior        TEXT,
    tradable        INTEGER,
    marketable      INTEGER,
    tradable_after  TEXT,
    source          TEXT,      -- which armory collection it came from
    star_cost       REAL,      -- Armory credits attributed to this item
    cost_basis_paise INTEGER,  -- our INR cost, minor units
    acquired_at     TEXT,
    raw             TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    market_hash_name TEXT NOT NULL,
    ts               TEXT NOT NULL,   -- ISO date of the observation
    median_paise     INTEGER,
    volume           INTEGER,
    source           TEXT,            -- pricehistory | priceoverview
    PRIMARY KEY (market_hash_name, ts, source)
);

CREATE TABLE IF NOT EXISTS quotes (
    market_hash_name TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    lowest_paise     INTEGER,
    median_paise     INTEGER,
    volume           INTEGER,
    PRIMARY KEY (market_hash_name, fetched_at)
);

CREATE TABLE IF NOT EXISTS orderbook (
    market_hash_name TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    best_buy_paise   INTEGER,
    buy_depth        INTEGER,
    sell_depth       INTEGER,
    raw              TEXT,
    PRIMARY KEY (market_hash_name, fetched_at)
);

CREATE TABLE IF NOT EXISTS plan (
    market_hash_name TEXT PRIMARY KEY,
    qty              INTEGER,
    cost_basis_paise INTEGER,
    breakeven_list_paise INTEGER,
    target_list_paise    INTEGER,
    floor_list_paise     INTEGER,
    confidence       REAL,
    rationale        TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    kind        TEXT,
    market_hash_name TEXT,
    message     TEXT,
    payload     TEXT,
    acted       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    market_hash_name TEXT,
    asset_id    TEXT,
    list_paise  INTEGER,
    net_paise   INTEGER,
    cost_basis_paise INTEGER,
    status      TEXT      -- listed | sold | cancelled
);

CREATE TABLE IF NOT EXISTS http_cache (
    url         TEXT PRIMARY KEY,
    fetched_at  REAL,
    body        TEXT
);

CREATE INDEX IF NOT EXISTS idx_hist_name ON price_history(market_hash_name);
CREATE INDEX IF NOT EXISTS idx_quotes_name ON quotes(market_hash_name);
"""


class Store:
    def __init__(self, path: str | Path = "data/market.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(args)))

    def one(self, sql: str, args: Iterable = ()) -> Optional[sqlite3.Row]:
        r = self.conn.execute(sql, tuple(args)).fetchone()
        return r

    # ---- http cache -------------------------------------------------
    def cache_get(self, url: str, max_age: float) -> Optional[str]:
        row = self.one("SELECT fetched_at, body FROM http_cache WHERE url=?", (url,))
        if row and (time.time() - row["fetched_at"]) < max_age:
            return row["body"]
        return None

    def cache_put(self, url: str, body: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO http_cache(url, fetched_at, body) VALUES (?,?,?)",
                (url, time.time(), body),
            )

    def close(self):
        self.conn.close()
