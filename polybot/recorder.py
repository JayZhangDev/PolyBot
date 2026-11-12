"""Turn websocket messages into the rows worth keeping. No network and no files in here.

The market feed sends four kinds of message for the tokens we subscribe to:

* ``book``             a full snapshot of one token's order book
* ``price_change``     one or more price levels whose size changed
* ``last_trade_price`` a trade happened
* ``tick_size_change`` the smallest price step changed (ignored)

Two choices keep the data small without losing information:

1. **Only the first token's book is kept.** A market has two tokens (YES and
   NO) and their books are exact mirrors: a bid for YES at 0.30 *is* an ask for
   NO at 0.70. Recording both would store everything twice.
2. **The book is written at most once per flush, and only if its top changed.**
   The feed sends around a hundred level changes a second, many of them deep in
   the book. We apply every one to our copy, then every couple of seconds write
   one row per market whose best five levels are different from last time.

Every trade is written, for either token.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from polybot.book import BookTop, OrderBook

DEPTH = 5  # price levels recorded on each side

BOOK_COLUMNS = (
    "ts",
    "market_id",
    "best_bid",
    "best_ask",
    "spread",
    "bid_depth",
    "ask_depth",
    *(f"bid_{kind}_{n}" for n in range(1, DEPTH + 1) for kind in ("px", "sz")),
    *(f"ask_{kind}_{n}" for n in range(1, DEPTH + 1) for kind in ("px", "sz")),
)
TRADE_COLUMNS = ("ts", "market_id", "outcome", "price", "size", "side", "fee_rate_bps", "tx_hash")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamMarket:
    """One market to stream: its ID and the two tokens that trade in it."""

    market_id: str
    question: str
    category: str
    yes_token: str  # the first outcome's token; its book is the one we record
    no_token: str


def parse_frame(raw: str | bytes) -> list[dict]:
    """Decode one websocket frame into a list of event dicts.

    The server sends either one JSON object or a JSON list of them (the first
    reply to a subscription is a list of every book). ``PONG`` and anything that
    is not JSON give an empty list.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw or raw in ("PONG", "PING"):
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    events = data if isinstance(data, list) else [data]
    return [event for event in events if isinstance(event, dict)]


def iso_ms(timestamp_ms: int) -> str:
    """Exchange time (milliseconds since 1970) as ``2026-11-12T02:38:43.376Z``."""
    moment = datetime.fromtimestamp(timestamp_ms // 1000, tz=timezone.utc)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{timestamp_ms % 1000:03d}Z"


def book_row(timestamp_ms: int, market_id: str, top: BookTop, depth: int = DEPTH) -> tuple:
    """One ``book_top`` row. Levels that do not exist are left blank."""
    row: list[object] = [
        iso_ms(timestamp_ms),
        market_id,
        _blank(top.best_bid),
        _blank(top.best_ask),
        _blank(top.spread),
        top.bid_depth,
        top.ask_depth,
    ]
    for levels in (top.bids, top.asks):
        for n in range(depth):
            row.extend(levels[n] if n < len(levels) else ("", ""))
    return tuple(row)


def _blank(value: float | None) -> object:
    return "" if value is None else value


class Recorder:
    """Keeps a live order book per market and collects rows between flushes."""

    def __init__(self, depth: int = DEPTH) -> None:
        self.depth = depth
        self.markets: dict[str, StreamMarket] = {}
        self._token: dict[str, tuple[str, int]] = {}  # token ID -> (market ID, 0 for YES or 1 for NO)
        self._books: dict[str, OrderBook] = {}  # market ID -> the YES token's book
        self._changed: dict[str, int] = {}  # market ID -> time (ms) of its latest change
        self._last_written: dict[str, BookTop] = {}
        self._trades: list[tuple] = []
        self.counts: Counter[str] = Counter()
        # A snapshot is stamped with the time the book last changed, which for a quiet market can be
        # hours before we connected. Rows are filed by day, so snapshot times are never allowed to be
        # earlier than this: the moment we subscribed and first saw the book in that state.
        self.snapshot_floor_ms = 0

    # ----------------------------- selection -----------------------------
    @property
    def tokens(self) -> list[str]:
        """Every token to subscribe to: both tokens of every selected market."""
        return list(self._token)

    def set_selection(self, markets: list[StreamMarket]) -> tuple[list[str], list[str]]:
        """Replace the set of markets. Returns ``(tokens to subscribe, tokens to unsubscribe)``."""
        old_tokens = set(self._token)
        self.markets = {m.market_id: m for m in markets}
        self._token = {}
        for m in markets:
            self._token[m.yes_token] = (m.market_id, 0)
            self._token[m.no_token] = (m.market_id, 1)
        for gone in set(self._books) - set(self.markets):
            del self._books[gone]
            self._changed.pop(gone, None)
            self._last_written.pop(gone, None)
        new_tokens = set(self._token)
        return sorted(new_tokens - old_tokens), sorted(old_tokens - new_tokens)

    def reset_books(self) -> None:
        """Forget every book. Called after a reconnect: we may have missed changes, and fresh snapshots follow."""
        self._books.clear()
        self._changed.clear()

    # ----------------------------- messages -----------------------------
    def handle(self, event: dict) -> None:
        """Apply one event. A malformed event is counted and skipped; it never stops the stream."""
        kind = event.get("event_type")
        self.counts[str(kind)] += 1
        try:
            if kind == "book":
                self._on_book(event)
            elif kind == "price_change":
                self._on_price_change(event)
            elif kind == "last_trade_price":
                self._on_trade(event)
        except (KeyError, TypeError, ValueError) as exc:
            self.counts["malformed"] += 1
            if self.counts["malformed"] <= 5:
                log.warning("Skipped a malformed %s message: %s: %s", kind, type(exc).__name__, exc)

    def _yes_market(self, token_id: object) -> str | None:
        """The market ID if ``token_id`` is a selected market's first (YES) token, else None."""
        info = self._token.get(str(token_id))
        return info[0] if info and info[1] == 0 else None

    def _on_book(self, event: dict) -> None:
        market_id = self._yes_market(event["asset_id"])
        if market_id is None:
            return
        self._books.setdefault(market_id, OrderBook()).replace(event.get("bids") or [], event.get("asks") or [])
        self._changed[market_id] = max(int(event["timestamp"]), self.snapshot_floor_ms)

    def _on_price_change(self, event: dict) -> None:
        timestamp = int(event["timestamp"])
        for change in event.get("price_changes") or []:
            market_id = self._yes_market(change["asset_id"])
            if market_id is None:
                continue
            book = self._books.get(market_id)
            if book is None:  # a change with no snapshot to apply it to; the snapshot is on its way
                self.counts["change_before_snapshot"] += 1
                continue
            book.update(change["side"], float(change["price"]), float(change["size"]))
            self._changed[market_id] = timestamp

    def _on_trade(self, event: dict) -> None:
        info = self._token.get(str(event["asset_id"]))
        if info is None:
            return
        market_id, outcome = info
        self._trades.append(
            (
                iso_ms(int(event["timestamp"])),
                market_id,
                outcome,
                float(event["price"]),
                float(event["size"]),
                event.get("side", ""),
                event.get("fee_rate_bps", ""),
                event.get("transaction_hash", ""),
            )
        )

    # ----------------------------- output -----------------------------
    def drain(self) -> tuple[list[tuple], list[tuple]]:
        """Hand over the rows collected since the last call: ``(book_top rows, trade rows)``.

        A market gets a book row only if its best levels differ from the last
        row written for it, so a change deep in the book adds nothing.
        """
        book_rows: list[tuple] = []
        for market_id, timestamp in self._changed.items():
            top = self._books[market_id].top(self.depth)
            if top != self._last_written.get(market_id):
                book_rows.append(book_row(timestamp, market_id, top, self.depth))
                self._last_written[market_id] = top
        self._changed.clear()
        trades, self._trades = self._trades, []
        return book_rows, trades
