"""Kalshi's public market data: read only, with no account and no key.

Kalshi is a US prediction-market exchange. Many of its questions also trade on
Polymarket, so its price is a second opinion on the same probability. PolyBot
only reads Kalshi's public prices. It has no Kalshi account and no code that
could trade there.

API reference: https://docs.kalshi.com (read 2026-11-13). Two facts shape this file:

* Prices are dollar strings such as ``"0.4300"``. The old whole-cent fields were
  removed in March 2026.
* Kalshi does not publish the request limit for callers without a key, so this
  file waits a second between pages and backs off when told to slow down (429).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from polybot.http import FetchError, get_json

log = logging.getLogger(__name__)

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
EVENTS_PER_PAGE = 200  # the most GET /events returns in one page
TICKERS_PER_REQUEST = 50  # markets per price lookup; the API states no maximum, so stay modest
PAGE_PAUSE_SECONDS = 1.0  # wait between requests
MAX_PAGES = 400  # a safety stop: listing everything took 108 pages in November 2026

Getter = Callable[..., object]


@dataclass(frozen=True)
class KalshiMarket:
    """One Kalshi market (a yes/no question) and its prices, all on a 0 to 1 scale."""

    ticker: str  # Kalshi's ID, e.g. KXFRENCHPRES-27-MLEP
    event_ticker: str
    title: str  # the question, e.g. "Will Marine Le Pen win the 2027 French presidential election?"
    outcome: str  # the YES side in a few words ("Marine Le Pen"), which multi-answer events need
    close_time: str | None  # when trading stops (ISO text, UTC)
    yes_bid: float | None  # best price someone will pay for YES now; None if nobody is bidding
    yes_ask: float | None  # best price YES can be bought at now; None if nobody is selling
    last_price: float | None
    volume_24h: float  # contracts traded in the last day
    status: str = "active"  # "active" while it trades; closed and settled markets say so

    @property
    def text(self) -> str:
        """The question as the matcher reads it: the title, plus the outcome if the title leaves it out."""
        if not self.outcome or self.outcome.lower() in self.title.lower():
            return self.title
        return f"{self.title} {self.outcome}"


def _price(value: object) -> float | None:
    """A dollar string as a number, or None when it is missing or not a number."""
    try:
        return float(str(value))
    except ValueError:
        return None


def market_from_api(m: dict, event: dict | None = None) -> KalshiMarket:
    """Turn one market from the API into a :class:`KalshiMarket`.

    A bid of 0 means nobody is bidding and an ask of 1 means nobody is selling,
    so both become None rather than a price nobody could trade at.
    """
    bid = _price(m.get("yes_bid_dollars"))
    ask = _price(m.get("yes_ask_dollars"))
    title = m.get("title") or (event or {}).get("title") or ""  # the market title is marked deprecated
    return KalshiMarket(
        ticker=str(m["ticker"]),
        event_ticker=str(m.get("event_ticker") or (event or {}).get("event_ticker") or ""),
        title=str(title).strip(),
        outcome=str(m.get("yes_sub_title") or "").strip(),
        close_time=m.get("close_time"),
        yes_bid=bid if bid is not None and bid > 0 else None,
        yes_ask=ask if ask is not None and ask < 1 else None,
        last_price=_price(m.get("last_price_dollars")),
        volume_24h=_price(m.get("volume_24h_fp")) or 0.0,
        status=str(m.get("status") or ""),
    )


def iter_open_markets(get: Getter = get_json, sleep: Callable[[float], None] = time.sleep) -> Iterator[KalshiMarket]:
    """Every open Kalshi market, one page of 200 events at a time.

    ``GET /events`` already leaves out Kalshi's multi-leg "combo" markets. Markets
    are handed over page by page, so a caller can keep only the ones it needs
    instead of holding all 200,000 in memory.
    """
    cursor = ""
    for page_number in range(1, MAX_PAGES + 1):
        params: dict[str, object] = {"status": "open", "with_nested_markets": "true", "limit": EVENTS_PER_PAGE}
        if cursor:
            params["cursor"] = cursor
        page = get(f"{BASE_URL}/events", params)
        for event in page.get("events") or []:  # type: ignore[union-attr]
            for m in event.get("markets") or []:
                if m.get("status") == "active" and m.get("ticker"):
                    yield market_from_api(m, event)
        cursor = page.get("cursor") or ""  # type: ignore[union-attr]
        if not cursor:
            return
        if page_number == MAX_PAGES:
            log.warning("Stopped listing Kalshi markets after %d pages; some were left out.", MAX_PAGES)
            return
        sleep(PAGE_PAUSE_SECONDS)


def fetch_markets(
    tickers: list[str], get: Getter = get_json, sleep: Callable[[float], None] = time.sleep
) -> dict[str, KalshiMarket]:
    """Current prices for the given markets, keyed by ticker. A batch that fails is skipped."""
    found: dict[str, KalshiMarket] = {}
    for start in range(0, len(tickers), TICKERS_PER_REQUEST):
        if start:
            sleep(PAGE_PAUSE_SECONDS)  # a pause before every batch but the first, whether the last one worked or not
        batch = tickers[start : start + TICKERS_PER_REQUEST]
        try:
            page = get(f"{BASE_URL}/markets", {"tickers": ",".join(batch)})
        except FetchError as exc:
            log.warning("Could not fetch %d Kalshi prices starting with %s: %s", len(batch), batch[0], exc)
            continue
        for m in page.get("markets") or []:  # type: ignore[union-attr]
            if m.get("ticker"):
                found[m["ticker"]] = market_from_api(m)
    return found
