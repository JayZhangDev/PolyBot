"""SQLite persistence for market metadata and price snapshots."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from polybot.db import connect

# Columns added after v0.1. init_db() adds them to existing databases in place,
# so an older polybot.db keeps all its history.
_MIGRATIONS = {
    "markets": {"event_id": "TEXT", "neg_risk": "INTEGER"},
    "snapshots": {"best_bid": "REAL", "best_ask": "REAL"},
}


def init_db() -> None:
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            slug TEXT,
            category TEXT,
            end_date TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            volume REAL,
            liquidity REAL,
            yes_price REAL,
            no_price REAL,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(captured_at);
        """)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
        for col, col_type in cols.items():
            if col not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def _float(value) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _parse_yes_no_prices(market: dict) -> tuple[float | None, float | None]:
    """Extract YES/NO prices from the market's outcomePrices field."""
    outcomes_raw = market.get("outcomePrices")
    if not outcomes_raw:
        return None, None
    try:
        # Polymarket returns this as a JSON string such as '["0.35", "0.65"]'
        prices = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        if len(prices) >= 2:
            return float(prices[0]), float(prices[1])
        if len(prices) == 1:
            return float(prices[0]), None
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None, None


def _event_info(market: dict) -> tuple[str | None, int | None]:
    """Return (event_id, neg_risk) for the event this market belongs to.

    A neg-risk event is a set of mutually exclusive markets where exactly one
    resolves YES (e.g. "Who will win the election?"), which is what the
    odds-mismatch check needs to be meaningful.
    """
    events = market.get("events") or []
    if not events or not isinstance(events[0], dict):
        return None, None
    ev = events[0]
    event_id = ev.get("id")
    neg_risk = ev.get("negRisk")
    return (str(event_id) if event_id is not None else None,
            int(bool(neg_risk)) if neg_risk is not None else None)


def save_snapshot(markets: list[dict]) -> int:
    """Upsert market metadata and insert one snapshot row per market."""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    with connect() as c:
        for m in markets:
            mid = str(m.get("id"))
            if not mid or mid == "None":
                continue
            event_id, neg_risk = _event_info(m)

            c.execute("""
                INSERT INTO markets (id, question, slug, category, end_date, first_seen, last_seen,
                                     raw_json, event_id, neg_risk)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    question=excluded.question,
                    slug=excluded.slug,
                    category=excluded.category,
                    end_date=excluded.end_date,
                    last_seen=excluded.last_seen,
                    raw_json=excluded.raw_json,
                    event_id=excluded.event_id,
                    neg_risk=excluded.neg_risk
            """, (mid, m.get("question", ""), m.get("slug"), m.get("category"), m.get("endDate"),
                  now, now, json.dumps(m), event_id, neg_risk))

            yes_p, no_p = _parse_yes_no_prices(m)
            c.execute("""
                INSERT INTO snapshots (market_id, captured_at, volume, liquidity,
                                       yes_price, no_price, best_bid, best_ask)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (mid, now, _float(m.get("volume")), _float(m.get("liquidity")),
                  yes_p, no_p, _float(m.get("bestBid")), _float(m.get("bestAsk"))))
            saved += 1
    return saved


def counts() -> dict:
    init_db()
    with connect() as c:
        m = c.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    return {"markets": m, "snapshots": s}
