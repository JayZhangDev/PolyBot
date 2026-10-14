"""Fetch Polymarket markets from the Gamma API and persist snapshots to SQLite."""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

import requests

from polybot.storage import counts, save_snapshot

GAMMA_BASE = "https://gamma-api.polymarket.com"
PAGE_SIZE = 100
TIMEOUT = 30

log = logging.getLogger(__name__)


def fetch_active_markets(limit: int = 500) -> list[dict]:
    """Return up to `limit` open markets, highest volume first.

    Sorting by volume (rather than soonest end date) means the snapshot set is
    made of markets the strategies can actually trade; every signal requires
    $200k+ volume and at least several days to resolution.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets: list[dict] = []
    offset = 0
    while len(markets) < limit:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={
                "active": "true",
                "closed": "false",
                "end_date_min": now,
                "order": "volumeNum",
                "ascending": "false",
                "limit": PAGE_SIZE,
                "offset": offset,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        markets.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return markets[:limit]


def fetch_markets_by_id(market_ids: list[str]) -> list[dict]:
    """Fetch specific markets, including ones that have closed or resolved.

    The active-market listing stops returning a market once it closes, so this
    is how open paper positions learn that their market has resolved.
    Failures are logged and skipped so one bad ID never blocks the cycle.
    """
    markets = []
    for mid in market_ids:
        try:
            resp = requests.get(f"{GAMMA_BASE}/markets/{mid}", timeout=TIMEOUT)
            resp.raise_for_status()
            markets.append(resp.json())
        except requests.RequestException as exc:
            log.warning("Could not refresh market %s: %s", mid, exc)
    return markets


def summarize(markets: list[dict]) -> None:
    print(f"\nGot {len(markets)} active markets\n")
    for m in markets[:10]:
        q = m.get("question", "?")
        try:
            vol = float(m.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0
        print(f"  [${vol:>12,.0f} vol]  {q[:80]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot active Polymarket markets.")
    parser.add_argument("--limit", type=int, default=500, help="Max markets to fetch")
    parser.add_argument("--quiet", action="store_true", help="Skip printout")
    args = parser.parse_args()

    print(f"→ Fetching up to {args.limit} active markets from Polymarket Gamma API...")
    markets = fetch_active_markets(limit=args.limit)
    if not args.quiet:
        summarize(markets)

    saved = save_snapshot(markets)
    stats = counts()
    print(f"→ Saved {saved} snapshots this run.")
    print(f"→ DB totals: {stats['markets']} unique markets, {stats['snapshots']} total snapshots.\n")


if __name__ == "__main__":
    main()
