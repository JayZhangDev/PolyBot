"""Fetch active Polymarket markets and persist snapshots to SQLite."""
from __future__ import annotations

import argparse
import requests

from polybot.storage import counts, save_snapshot

GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_active_markets(limit: int = 50) -> list[dict]:
    all_markets = []
    offset = 0
    page_size = 100
    while len(all_markets) < limit:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
                "order": "endDate",
                "ascending": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        all_markets.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_markets[:limit]


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50, help="Max markets to fetch")
    parser.add_argument("--quiet", action="store_true", help="Skip printout")
    args = parser.parse_args()

    print(f"→ Fetching up to {args.limit} active markets from Polymarket Gamma API...")
    markets = fetch_active_markets(limit=args.limit)
    if not args.quiet:
        summarize(markets)

    print(f"\n→ Saving snapshots to SQLite...")
    saved = save_snapshot(markets)
    stats = counts()
    print(f"→ Saved {saved} snapshots this run.")
    print(f"→ DB totals: {stats['markets']} unique markets, {stats['snapshots']} total snapshots.\n")


if __name__ == "__main__":
    main()
