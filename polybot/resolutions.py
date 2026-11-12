"""Learn how markets ended.

Calibration needs an outcome for every market PolyBot has snapshotted, not just
the ones it traded. Markets leave the active listing when they close, so each
cycle this module re-fetches a batch of markets that have ended (or quietly
dropped out of the listing) and records YES, NO or VOID once they resolve.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from polybot.categories import category_of
from polybot.db import connect
from polybot.fetch import fetch_markets_by_id
from polybot.storage import init_db, save_snapshot
from polybot.timeutil import parse_ts

CHECK_LIMIT = 50  # markets re-fetched per cycle, to stay polite to the API
RECHECK_HOURS = 24  # how long to wait before re-checking an unresolved market
DROPPED_AFTER_HOURS = 3  # not seen in the active listing for this long → check it
RESOLVED_HI = 0.99
RESOLVED_LO = 0.01


def outcome_of(market: dict) -> str | None:
    """'YES', 'NO' or 'VOID' for a closed market; None if it hasn't resolved.

    A closed market whose prices are not pinned to 0/1 (e.g. a 50/50 split)
    is VOID and excluded from calibration.
    """
    if not market.get("closed"):
        return None
    try:
        raw = market.get("outcomePrices")
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes = float(prices[0])
    except (TypeError, ValueError, IndexError, json.JSONDecodeError):
        return "VOID"
    if yes >= RESOLVED_HI:
        return "YES"
    if yes <= RESOLVED_LO:
        return "NO"
    return "VOID"


def resolved_at_of(market: dict) -> str | None:
    """When the market closed (closedTime), falling back to its scheduled end."""
    dt = parse_ts(market.get("closedTime")) or parse_ts(market.get("endDate"))
    return dt.isoformat() if dt else None


def record_resolutions(markets: list[dict], checked_ids: list[str] | None = None) -> int:
    """Store outcomes for any resolved markets; mark every checked ID as checked.

    Returns the number of newly resolved markets.
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    by_id = {str(m.get("id")): m for m in markets if m.get("id") is not None}
    ids = set(checked_ids or []) | set(by_id)
    newly = 0
    with connect() as c:
        for mid in ids:
            m = by_id.get(mid)
            outcome = outcome_of(m) if m else None
            resolved_at = resolved_at_of(m) if outcome else None
            prev = c.execute("SELECT outcome FROM resolutions WHERE market_id = ?", (mid,)).fetchone()
            if outcome and not (prev and prev["outcome"]):
                newly += 1
            c.execute(
                """
                INSERT INTO resolutions (market_id, outcome, resolved_at, last_checked)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    outcome = COALESCE(excluded.outcome, resolutions.outcome),
                    resolved_at = COALESCE(excluded.resolved_at, resolutions.resolved_at),
                    last_checked = excluded.last_checked
            """,
                (mid, outcome, resolved_at, now),
            )
    return newly


def markets_to_check(limit: int = CHECK_LIMIT) -> list[str]:
    """Unresolved markets that have ended or dropped out of the active listing."""
    init_db()
    now = datetime.now(timezone.utc)
    now_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    dropped = (now - timedelta(hours=DROPPED_AFTER_HOURS)).isoformat()
    recheck = (now - timedelta(hours=RECHECK_HOURS)).isoformat()
    with connect() as c:
        rows = c.execute(
            """
            SELECT m.id FROM markets m
            LEFT JOIN resolutions r ON r.market_id = m.id
            WHERE r.outcome IS NULL
              AND (r.last_checked IS NULL OR r.last_checked < ?)
              AND (m.end_date < ? OR m.last_seen < ?)
            ORDER BY m.end_date
            LIMIT ?
        """,
            (recheck, now_z, dropped, limit),
        ).fetchall()
    return [r["id"] for r in rows]


def sweep(limit: int = CHECK_LIMIT) -> dict:
    """One resolution pass: fetch a batch of candidates, snapshot them, record outcomes."""
    ids = markets_to_check(limit)
    if not ids:
        return {"checked": 0, "resolved": 0}
    markets = fetch_markets_by_id(ids)
    save_snapshot(markets)
    return {"checked": len(ids), "resolved": record_resolutions(markets, checked_ids=ids)}


def counts() -> dict:
    init_db()
    with connect() as c:
        rows = c.execute("""
            SELECT COALESCE(outcome, 'PENDING') AS o, COUNT(*) AS n
            FROM resolutions GROUP BY o
        """).fetchall()
    return {r["o"]: r["n"] for r in rows}


def backfill(batch: int = 200) -> dict:
    """Check every candidate now instead of 50 per hour. Safe to re-run."""
    total = {"checked": 0, "resolved": 0}
    while True:
        r = sweep(limit=batch)
        if r["checked"] == 0:
            return total
        total["checked"] += r["checked"]
        total["resolved"] += r["resolved"]
        print(f"  checked {total['checked']}, resolved {total['resolved']}...")


def backfill_categories() -> dict:
    """Give a category to every recorded market that has none, by looking it up with its tags.

    Markets in the live listing get their category as they are snapshotted. This
    covers the rest: markets that closed before categories were recorded.
    Returns how many were missing and how many were filled in.
    """
    init_db()
    with connect() as c:
        missing = [r["id"] for r in c.execute("SELECT id FROM markets WHERE category IS NULL ORDER BY id")]
    found = {str(m.get("id")): category_of(m) for m in fetch_markets_by_id(missing)} if missing else {}
    filled = [(category, market_id) for market_id, category in found.items() if category]
    with connect() as c:
        c.executemany("UPDATE markets SET category = ? WHERE id = ? AND category IS NULL", filled)
    return {"missing": len(missing), "filled": len(filled)}


def main() -> None:
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="Record outcomes of ended Polymarket markets.")
    parser.add_argument(
        "--backfill", action="store_true", help="Check every ended market now (first run on an existing database)"
    )
    parser.add_argument(
        "--categories", action="store_true", help="Look up a category for every market that has none, then stop"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.categories:
        result = backfill_categories()
        print(f"{result['missing']} markets had no category; filled in {result['filled']}.")
        return

    result = backfill() if args.backfill else sweep()
    print(f"Checked {result['checked']} markets, {result['resolved']} newly resolved.")
    print(f"Totals: {counts()}")


if __name__ == "__main__":
    main()
