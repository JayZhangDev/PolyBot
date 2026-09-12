"""SQLite persistence for market snapshots."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("polybot.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as c:
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


def _parse_yes_no_prices(market: dict) -> tuple[float | None, float | None]:
    """Extract YES/NO prices from the market's outcomePrices field."""
    outcomes_raw = market.get("outcomePrices")
    if not outcomes_raw:
        return None, None
    try:
        # Polymarket returns this as a JSON string of ["0.35", "0.65"]
        if isinstance(outcomes_raw, str):
            prices = json.loads(outcomes_raw)
        else:
            prices = outcomes_raw
        if len(prices) >= 2:
            return float(prices[0]), float(prices[1])
        if len(prices) == 1:
            return float(prices[0]), None
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None, None


def save_snapshot(markets: list[dict]) -> int:
    """Save markets metadata + a fresh snapshot row per market. Returns snapshot count."""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    with _connect() as c:
        for m in markets:
            mid = str(m.get("id"))
            if not mid or mid == "None":
                continue
            question = m.get("question", "")
            slug = m.get("slug")
            category = m.get("category")
            end_date = m.get("endDate")
            raw = json.dumps(m)

            # Upsert market metadata
            c.execute("""
                INSERT INTO markets (id, question, slug, category, end_date, first_seen, last_seen, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    question=excluded.question,
                    slug=excluded.slug,
                    category=excluded.category,
                    end_date=excluded.end_date,
                    last_seen=excluded.last_seen,
                    raw_json=excluded.raw_json
            """, (mid, question, slug, category, end_date, now, now, raw))

            # Snapshot the current state
            try:
                vol = float(m.get("volume") or 0)
            except (TypeError, ValueError):
                vol = None
            try:
                liq = float(m.get("liquidity") or 0)
            except (TypeError, ValueError):
                liq = None
            yes_p, no_p = _parse_yes_no_prices(m)

            c.execute("""
                INSERT INTO snapshots (market_id, captured_at, volume, liquidity, yes_price, no_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (mid, now, vol, liq, yes_p, no_p))
            saved += 1
    return saved


def counts() -> dict:
    init_db()
    with _connect() as c:
        m = c.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    return {"markets": m, "snapshots": s}
