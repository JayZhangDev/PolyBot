"""Fetch Polymarket markets from the Gamma API and persist snapshots to SQLite."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from polybot import http
from polybot.http import FetchError
from polybot.storage import counts, save_snapshot

GAMMA_BASE = "https://gamma-api.polymarket.com"
PAGE_SIZE = 100  # the API returns at most 100 markets per request
PAGE_PAUSE = 0.2  # seconds between pages: far below the rate limit, on purpose
BY_ID_TRIES = 3  # single-market lookups get fewer retries; they are tried again next cycle
MAX_BY_ID_FAILURES = 5  # this many lookups failing in a row means the API is down: stop asking

log = logging.getLogger(__name__)


def _listing_params(now: str, offset: int) -> dict:
    """Query for one page of open markets, highest volume first."""
    return {
        "active": "true",
        "closed": "false",
        "end_date_min": now,
        "order": "volumeNum",
        "ascending": "false",
        "limit": PAGE_SIZE,
        "offset": offset,
    }


def fetch_active_markets(limit: int = 500) -> list[dict]:
    """Return up to ``limit`` open markets, highest volume first.

    Sorting by volume (rather than soonest end date) means the snapshot set is
    made of markets the strategies can actually trade; every signal requires
    $200k+ volume and at least several days to resolution.

    Each page is retried with backoff (see ``polybot.http``). If a later page
    still fails, the pages already loaded are returned: most of the data is
    better than none. Only a failure on the very first page raises.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets: list[dict] = []
    offset = 0
    while len(markets) < limit:
        try:
            page = http.get_json(f"{GAMMA_BASE}/markets", _listing_params(now, offset))
        except FetchError as exc:
            if not markets:
                raise
            log.warning(
                "Keeping the %d markets already loaded; the page at offset %d failed: %s", len(markets), offset, exc
            )
            break
        if not isinstance(page, list) or not page:
            break
        markets.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(PAGE_PAUSE)
    return markets[:limit]


def fetch_markets_by_id(market_ids: list[str]) -> list[dict]:
    """Fetch specific markets, including ones that have closed or resolved.

    The active-market listing stops returning a market once it closes, so this
    is how open paper positions learn that their market has resolved.
    Failures are logged and skipped so one bad ID never blocks the cycle, and
    the batch stops early if the API is clearly down.
    """
    markets: list[dict] = []
    failures_in_a_row = 0
    for position, mid in enumerate(market_ids):
        try:
            market = http.get_json(f"{GAMMA_BASE}/markets/{mid}", tries=BY_ID_TRIES)
        except FetchError as exc:
            log.warning("Could not refresh market %s: %s", mid, exc)
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_BY_ID_FAILURES:
                log.warning(
                    "%d lookups failed in a row; skipping the remaining %d until next cycle",
                    failures_in_a_row,
                    len(market_ids) - position - 1,
                )
                break
            continue
        failures_in_a_row = 0
        if isinstance(market, dict):
            markets.append(market)
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Plain ASCII on purpose: a Windows console that is not UTF-8 crashes on arrows and long dashes.
    print(f"Fetching up to {args.limit} active markets from the Polymarket Gamma API...")
    markets = fetch_active_markets(limit=args.limit)
    if not args.quiet:
        summarize(markets)

    saved = save_snapshot(markets)
    stats = counts()
    print(f"Saved {saved} snapshots this run.")
    print(f"DB totals: {stats['markets']} unique markets, {stats['snapshots']} total snapshots.\n")


if __name__ == "__main__":
    main()
