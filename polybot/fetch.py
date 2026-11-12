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
BY_ID_TRIES = 3  # lookups by ID get fewer retries; they are tried again next cycle
BY_ID_BATCH = 50  # market IDs per lookup request
MAX_BY_ID_FAILURES = 3  # this many batches failing in a row means the API is down: stop asking

log = logging.getLogger(__name__)


def _listing_params(now: str, cursor: str | None) -> dict:
    """Query for one page of open markets, highest volume first."""
    params: dict[str, object] = {
        "closed": "false",
        "include_tag": "true",  # tags are how a market gets its category
        "end_date_min": now,
        "order": "volumeNum",
        "ascending": "false",
        "limit": PAGE_SIZE,
    }
    if cursor:
        params["after_cursor"] = cursor
    return params


def fetch_active_markets(limit: int = 500) -> list[dict]:
    """Return up to ``limit`` open markets, highest volume first.

    Sorting by volume (rather than soonest end date) means the snapshot set is
    made of markets the strategies can actually trade; every signal requires
    $200k+ volume and at least several days to resolution.

    Paging uses the keyset endpoint: every response carries a ``next_cursor``
    that marks where the following page starts. Volumes keep changing while we
    page, so a market can show up twice; duplicates are dropped by ID.

    Each page is retried with backoff (see ``polybot.http``). If a later page
    still fails, the pages already loaded are returned: most of the data is
    better than none. Only a failure on the very first page raises.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets: list[dict] = []
    seen: set[str] = set()
    cursor: str | None = None
    while len(markets) < limit:
        try:
            body = http.get_json(f"{GAMMA_BASE}/markets/keyset", _listing_params(now, cursor))
        except FetchError as exc:
            if not markets:
                raise
            log.warning("Keeping the %d markets already loaded; the next page failed: %s", len(markets), exc)
            break
        page = body.get("markets") if isinstance(body, dict) else None
        if not page:
            break
        for market in page:
            market_id = str(market.get("id"))
            if market_id not in seen:
                seen.add(market_id)
                markets.append(market)
        cursor = body.get("next_cursor")
        if not cursor:
            break
        time.sleep(PAGE_PAUSE)
    return markets[:limit]


def fetch_markets_by_id(market_ids: list[str]) -> list[dict]:
    """Fetch specific markets, including ones that have closed or resolved.

    The active-market listing stops returning a market once it closes, so this
    is how open paper positions learn that their market has resolved.

    IDs are sent in batches of 50 per request (``closed=any`` returns open and
    closed markets alike), so checking 200 markets is 4 requests, not 200. An ID
    the API does not know is simply left out of the answer. A batch that fails
    is logged and skipped, and the run stops early if the API is clearly down.
    """
    markets: list[dict] = []
    failures_in_a_row = 0
    for start in range(0, len(market_ids), BY_ID_BATCH):
        batch = market_ids[start : start + BY_ID_BATCH]
        params = [("id", market_id) for market_id in batch]
        params += [("closed", "any"), ("include_tag", "true"), ("limit", PAGE_SIZE)]
        try:
            body = http.get_json(f"{GAMMA_BASE}/markets/keyset", params, tries=BY_ID_TRIES)
        except FetchError as exc:
            log.warning("Could not refresh %d markets starting with %s: %s", len(batch), batch[0], exc)
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_BY_ID_FAILURES:
                log.warning("%d batches failed in a row; leaving the rest until next cycle", failures_in_a_row)
                break
            continue
        failures_in_a_row = 0
        page = body.get("markets") if isinstance(body, dict) else None
        markets.extend(m for m in page or [] if isinstance(m, dict))
        if start + BY_ID_BATCH < len(market_ids):
            time.sleep(PAGE_PAUSE)
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
