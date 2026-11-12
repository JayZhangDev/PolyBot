"""Discover the open markets worth knowing about and refresh the catalog.

Polymarket lists tens of thousands of open markets, and most have almost no
money in them. Downloading all of them every 15 minutes would move hundreds of
megabytes for nothing, so the liquidity and volume floors from ``config.yaml``
are sent to the API and only markets that pass come back. Set both floors to 0
to list every open market.

Paging uses the "keyset" endpoint: each response carries a cursor marking
where the next page starts. The older offset style ("skip the first N") is
being retired by Polymarket and already refuses large offsets.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from polybot import http, rawstore
from polybot.catalog import MarketMeta, save_catalog, select_stream_markets
from polybot.categories import CATEGORY_TAGS, category_from_tags, tag_slugs
from polybot.config import Settings, load_settings
from polybot.fetch import GAMMA_BASE
from polybot.http import FetchError
from polybot.jobs import run_job

KEYSET_URL = f"{GAMMA_BASE}/markets/keyset"
PAGE_SIZE = 100  # the most the endpoint allows
PAGE_PAUSE = 0.2  # seconds between pages, to stay polite
MAX_PAGES = 1500  # safety stop (150,000 markets) in case a cursor ever loops
LOCK_WAIT_SECONDS = 600

log = logging.getLogger("polybot.discovery")
__all__ = ["CATEGORY_TAGS", "category_from_tags", "fetch_open_markets", "main", "parse_market"]


def _json_list(value: object) -> tuple[str, ...]:
    """The API sends lists such as outcomes as a string holding JSON; decode it safely."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def _number(value: object) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def parse_market(market: dict) -> MarketMeta | None:
    """Turn one Gamma API market into a :class:`MarketMeta`; None if it has no ID or question."""
    market_id, question = market.get("id"), market.get("question")
    if market_id is None or not question:
        return None
    tags = tag_slugs(market)
    return MarketMeta(
        market_id=str(market_id),
        condition_id=market.get("conditionId") or None,
        question=str(question),
        slug=market.get("slug") or None,
        category=category_from_tags(tags),
        tags=tags,
        end_date=market.get("endDate") or None,
        outcomes=_json_list(market.get("outcomes")),
        token_ids=_json_list(market.get("clobTokenIds")),
        volume=_number(market.get("volumeNum") or market.get("volume")),
        volume_24h=_number(market.get("volume24hr")),
        liquidity=_number(market.get("liquidityNum") or market.get("liquidity")),
        accepting_orders=bool(market.get("acceptingOrders")),
    )


def _page_params(settings: Settings, cursor: str | None) -> dict:
    """Query for one page of open markets, with the floors applied by the server."""
    params: dict[str, object] = {"limit": PAGE_SIZE, "closed": "false", "include_tag": "true"}
    if settings.min_liquidity > 0:
        params["liquidity_num_min"] = settings.min_liquidity
    if settings.min_volume > 0:
        params["volume_num_min"] = settings.min_volume
    if cursor:
        params["after_cursor"] = cursor
    return params


def fetch_open_markets(settings: Settings) -> list[MarketMeta]:
    """Page through every open market that passes the floors.

    Each page is parsed and the raw response dropped straight away, so memory
    stays small however long the list is. If a later page fails after its
    retries, the markets already listed are kept; markets that were not reached
    simply keep their previous ``last_seen`` and are picked up next time.
    """
    metas: list[MarketMeta] = []
    cursor: str | None = None
    for page_number in range(1, MAX_PAGES + 1):
        try:
            body = http.get_json(KEYSET_URL, _page_params(settings, cursor))
        except FetchError as exc:
            if not metas:
                raise
            log.warning("Keeping the %d markets already listed; page %d failed: %s", len(metas), page_number, exc)
            break
        if not isinstance(body, dict):
            break
        metas.extend(meta for meta in map(parse_market, body.get("markets") or []) if meta is not None)
        cursor = body.get("next_cursor")
        if not cursor:
            break
        time.sleep(PAGE_PAUSE)
    return metas


def write_selection(selected: list[MarketMeta]) -> None:
    """Publish the chosen markets in a small file for the streamer.

    The streamer reads this file instead of the database, so it never has to
    wait for (or collide with) a job that is writing there.
    """
    markets = [
        {
            "market_id": m.market_id,
            "question": m.question,
            "category": m.category,
            "yes_token": m.token_ids[0],
            "no_token": m.token_ids[1],
        }
        for m in selected
    ]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rawstore.write_json_atomic(rawstore.selection_path(), {"updated_at": stamp, "markets": markets})


def _download() -> tuple[Settings, list[MarketMeta]]:
    """The slow part, done before the database lock is taken."""
    settings = load_settings()
    return settings, fetch_open_markets(settings)


def _store(downloaded: tuple[Settings, list[MarketMeta]]) -> str:
    """The quick part, done while holding the lock. Returns the run's one-line summary."""
    settings, metas = downloaded
    counts = save_catalog(metas)
    selected = select_stream_markets(settings)
    write_selection(selected)
    return (
        f"listed {counts['listed']} open markets ({counts['new']} new), "
        f"{len(selected)} selected for streaming (max {settings.max_stream_markets})"
    )


def main() -> int:
    """Entry point for the 15-minute scheduled task. Returns the process exit code."""
    return run_job("discover", _store, prepare=_download, wait_seconds=LOCK_WAIT_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
