"""Signal generation for PolyBot.

v0.1 signal: contrarian bet against heavy favorites.
  - YES price >= 0.80
  - Volume >= $1M
  - Market ends within 30 days
  - Not already in an open position

This signal probably loses money over time. The point is to have a real,
executable signal so the paper-trade infrastructure has something to run against.
Better signals come later.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path("polybot.db")

# Signal parameters — v0.1 longshot favorite bias
MAX_YES_PRICE = 0.15
MIN_VOLUME = 500_000
MIN_DAYS_TO_RESOLUTION = 7
MAX_DAYS_TO_RESOLUTION = 365


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def find_signals(exclude_market_ids: set[str] | None = None) -> list[dict]:
    """Return list of markets currently matching the signal.

    Each returned dict has: market_id, question, yes_price, no_price,
    volume, end_date, side ('NO'), reason.
    """
    exclude_market_ids = exclude_market_ids or set()
    now = datetime.now(timezone.utc)
    min_end = now + timedelta(days=MIN_DAYS_TO_RESOLUTION)
    max_end = now + timedelta(days=MAX_DAYS_TO_RESOLUTION)

    signals = []
    with _connect() as c:
        # Get the LATEST snapshot for each market
        rows = c.execute("""
            SELECT m.id, m.question, m.end_date,
                   s.yes_price, s.no_price, s.volume, s.captured_at
            FROM markets m
            JOIN snapshots s ON s.market_id = m.id
            WHERE s.id = (
                SELECT MAX(id) FROM snapshots WHERE market_id = m.id
            )
        """).fetchall()

    for r in rows:
        mid = r["id"]
        if mid in exclude_market_ids:
            continue
        yes = r["yes_price"]
        vol = r["volume"]
        end = r["end_date"]
        if yes is None or vol is None or end is None:
            continue
        if yes > MAX_YES_PRICE:
            continue
        if yes <= 0.001:
            continue  # too extreme, likely dead market
        if vol < MIN_VOLUME:
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if end_dt < min_end or end_dt > max_end:
            continue
        signals.append({
            "market_id": mid,
            "question": r["question"],
            "yes_price": yes,
            "no_price": r["no_price"],
            "volume": vol,
            "end_date": end,
            "side": "YES",
            "reason": f"Longshot: YES={yes:.3f} <= {MAX_YES_PRICE}, vol=${vol:,.0f}",
        })
    return signals


if __name__ == "__main__":
    sigs = find_signals()
    print(f"\nFound {len(sigs)} signals:\n")
    for s in sigs:
        print(f"  [{s['side']} @ {s['no_price']:.3f}]  vol=${s['volume']:>12,.0f}  ends {s['end_date'][:10]}")
        print(f"    {s['question'][:80]}")
        print(f"    reason: {s['reason']}\n")
