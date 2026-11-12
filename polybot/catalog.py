"""The market catalog: one row of metadata for every open market that passes the floors.

Two tables hold markets, and they answer different questions:

* ``markets`` (older) is every market PolyBot has recorded *prices* for.
* ``market_catalog`` (this file) is every market open on Polymarket right now
  that passes the liquidity and volume floors, with the facts needed to choose
  and stream it: question, category, end date, outcomes, token IDs, volume and
  liquidity. It is refreshed every 15 minutes.

``select_stream_markets`` applies the rest of ``config.yaml`` (categories and
the maximum count) to pick the markets worth streaming live.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from polybot.config import ConfigError, Settings, load_settings
from polybot.db import connect

# A market counts as "open now" if a refresh saw it within this window. Three
# refresh cycles, so one failed or partial refresh does not empty the selection.
FRESH_MINUTES = 45


@dataclass(frozen=True)
class MarketMeta:
    """The facts about one market that PolyBot keeps in the catalog."""

    market_id: str
    condition_id: str | None  # Polymarket's on-chain ID for the market; the websocket uses it
    question: str
    slug: str | None
    category: str  # one of polybot.config.CATEGORIES
    tags: tuple[str, ...]  # Polymarket's tag slugs, most specific last
    end_date: str | None
    outcomes: tuple[str, ...]  # usually ("Yes", "No")
    token_ids: tuple[str, ...]  # one tradable token per outcome, in the same order
    volume: float  # dollars traded since the market opened
    volume_24h: float
    liquidity: float  # dollars of resting orders
    accepting_orders: bool
    fee_rate: float | None = None  # the market's own taker fee rate, when the API gave one (not stored)


def init_catalog_table() -> None:
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS market_catalog (
            market_id TEXT PRIMARY KEY,
            condition_id TEXT,
            question TEXT NOT NULL,
            slug TEXT,
            category TEXT NOT NULL,
            tags TEXT NOT NULL,              -- JSON list of tag slugs
            end_date TEXT,
            outcomes TEXT NOT NULL,          -- JSON list, e.g. ["Yes", "No"]
            token_ids TEXT NOT NULL,         -- JSON list, same order as outcomes
            volume REAL NOT NULL,
            volume_24h REAL NOT NULL,
            liquidity REAL NOT NULL,
            accepting_orders INTEGER NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_catalog_last_seen ON market_catalog(last_seen);
        """)


def _row_values(m: MarketMeta, stamp: str) -> tuple:
    return (
        m.market_id,
        m.condition_id,
        m.question,
        m.slug,
        m.category,
        json.dumps(list(m.tags)),
        m.end_date,
        json.dumps(list(m.outcomes)),
        json.dumps(list(m.token_ids)),
        m.volume,
        m.volume_24h,
        m.liquidity,
        int(m.accepting_orders),
        stamp,
        stamp,
    )


def save_catalog(metas: list[MarketMeta], now: datetime | None = None) -> dict:
    """Insert new markets and refresh the ones already known. Returns counts.

    Everything goes in one transaction, so a reader never sees a half-updated
    catalog. ``first_seen`` is kept from the first time a market appeared.
    """
    init_catalog_table()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with connect() as c:
        before = c.execute("SELECT COUNT(*) FROM market_catalog").fetchone()[0]
        c.executemany(
            """
            INSERT INTO market_catalog (market_id, condition_id, question, slug, category, tags, end_date,
                outcomes, token_ids, volume, volume_24h, liquidity, accepting_orders, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                condition_id = excluded.condition_id, question = excluded.question, slug = excluded.slug,
                category = excluded.category, tags = excluded.tags, end_date = excluded.end_date,
                outcomes = excluded.outcomes, token_ids = excluded.token_ids, volume = excluded.volume,
                volume_24h = excluded.volume_24h, liquidity = excluded.liquidity,
                accepting_orders = excluded.accepting_orders, last_seen = excluded.last_seen
        """,
            [_row_values(m, stamp) for m in metas],
        )
        after = c.execute("SELECT COUNT(*) FROM market_catalog").fetchone()[0]
    return {"listed": len(metas), "new": after - before, "total": after}


def _from_row(r: sqlite3.Row) -> MarketMeta:
    return MarketMeta(
        market_id=r["market_id"],
        condition_id=r["condition_id"],
        question=r["question"],
        slug=r["slug"],
        category=r["category"],
        tags=tuple(json.loads(r["tags"])),
        end_date=r["end_date"],
        outcomes=tuple(json.loads(r["outcomes"])),
        token_ids=tuple(json.loads(r["token_ids"])),
        volume=r["volume"],
        volume_24h=r["volume_24h"],
        liquidity=r["liquidity"],
        accepting_orders=bool(r["accepting_orders"]),
    )


def _fresh_cutoff(now: datetime | None) -> str:
    return ((now or datetime.now(timezone.utc)) - timedelta(minutes=FRESH_MINUTES)).isoformat()


def open_markets(now: datetime | None = None) -> list[MarketMeta]:
    """Every catalog market a recent refresh saw, highest volume first."""
    init_catalog_table()
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM market_catalog WHERE last_seen >= ? ORDER BY volume DESC", (_fresh_cutoff(now),)
        ).fetchall()
    return [_from_row(r) for r in rows]


def passes_filters(m: MarketMeta, settings: Settings) -> bool:
    """True when a market may be streamed under the current settings.

    Besides the thresholds in config.yaml, a market needs exactly two tokens
    (a YES/NO style market) and must be accepting orders, or there is no live
    order book to record.
    """
    if m.liquidity < settings.min_liquidity or m.volume < settings.min_volume:
        return False
    if settings.include_categories and m.category not in settings.include_categories:
        return False
    if m.category in settings.skip_categories:
        return False
    return m.accepting_orders and len(m.token_ids) == 2


def select_stream_markets(settings: Settings, now: datetime | None = None) -> list[MarketMeta]:
    """The markets to stream live: those passing the filters, top ``max_stream_markets`` by ``rank_by``."""
    eligible = [m for m in open_markets(now) if passes_filters(m, settings)]
    eligible.sort(key=lambda m: getattr(m, settings.rank_by), reverse=True)
    return eligible[: settings.max_stream_markets]


def last_refresh() -> datetime | None:
    """When the catalog was last refreshed, or None if it never was."""
    init_catalog_table()
    with connect() as c:
        stamp = c.execute("SELECT MAX(last_seen) FROM market_catalog").fetchone()[0]
    return datetime.fromisoformat(stamp) if stamp else None


def category_counts(markets: list[MarketMeta]) -> list[tuple[str, int]]:
    """``[(category, how many)]`` with the biggest group first."""
    return Counter(m.category for m in markets).most_common()


def describe_selection(settings: Settings, now: datetime | None = None, top: int = 10) -> str:
    """The text ``polybot markets`` prints: catalog size, the selection, and its biggest markets."""
    now = now or datetime.now(timezone.utc)
    listed = open_markets(now)
    selected = select_stream_markets(settings, now)
    refreshed = last_refresh()
    age = "never refreshed" if refreshed is None else f"refreshed {(now - refreshed).total_seconds() / 60:.0f} min ago"
    lines = [
        f"Catalog  : {len(listed):,} open markets with liquidity >= ${settings.min_liquidity:,.0f} "
        f"and volume >= ${settings.min_volume:,.0f} ({age})",
        f"Selected : {len(selected):,} to stream (max {settings.max_stream_markets}, top by {settings.rank_by})",
    ]
    if settings.include_categories:
        lines.append(f"Only     : {', '.join(settings.include_categories)}")
    if settings.skip_categories:
        lines.append(f"Skipping : {', '.join(settings.skip_categories)}")
    lines += ["", "Category      catalog  selected"]
    chosen = dict(category_counts(selected))
    for category, count in category_counts(listed):
        lines.append(f"  {category:<12}{count:>7}{chosen.get(category, 0):>10}")
    lines += ["", f"Top {min(top, len(selected))} selected:"]
    for m in selected[:top]:
        lines.append(f"  ${m.volume:>13,.0f} vol  ${m.liquidity:>10,.0f} liq  {m.category:<11} {m.question[:70]}")
    return "\n".join(lines)


def main() -> int:
    """``polybot markets``: show what the current config.yaml selects. Reads only."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config.yaml problem: {exc}")
        return 2
    print(describe_selection(settings))
    return 0
