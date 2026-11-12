"""An order book for one token, kept up to date from websocket messages.

An order book is the list of orders waiting to trade. **Bids** are offers to
buy ("I will pay up to 0.42") and **asks** are offers to sell ("I want at least
0.44"). The highest bid and the lowest ask are the *best* bid and ask; the gap
between them is the *spread*. *Depth* is how many shares are waiting at each
price, which is what limits how much anyone can really trade.

The feed sends one full snapshot of the book, then small changes ("the size
at 0.42 is now 500"). Applying each change to the snapshot keeps our copy
identical to the exchange's.
"""

from __future__ import annotations

from dataclasses import dataclass

Level = tuple[float, float]  # (price, shares waiting at that price)


@dataclass(frozen=True)
class BookTop:
    """The best few price levels on each side at one moment, best first."""

    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> float | None:
        """Best ask minus best bid, or None when one side is empty."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 6)

    @property
    def bid_depth(self) -> float:
        """Shares waiting across the listed bid levels."""
        return round(sum(size for _price, size in self.bids), 2)

    @property
    def ask_depth(self) -> float:
        return round(sum(size for _price, size in self.asks), 2)


class OrderBook:
    """Every resting price level for one token: price -> shares, per side."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    @staticmethod
    def _levels(raw_levels: list[dict]) -> dict[float, float]:
        """Turn ``[{"price": "0.42", "size": "500"}, ...]`` into ``{0.42: 500.0}``, dropping empty levels."""
        levels = {float(level["price"]): float(level["size"]) for level in raw_levels}
        return {price: size for price, size in levels.items() if size > 0}

    def replace(self, bids: list[dict], asks: list[dict]) -> None:
        """Load a full snapshot (a ``book`` message), discarding whatever we had."""
        self.bids = self._levels(bids)
        self.asks = self._levels(asks)

    def update(self, side: str, price: float, size: float) -> None:
        """Apply one change: ``side`` is BUY (a bid) or SELL (an ask). A size of 0 removes the level."""
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        levels = self.bids if side == "BUY" else self.asks
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)

    def top(self, depth: int = 5) -> BookTop:
        """The best ``depth`` levels per side: highest bids first, lowest asks first."""
        bids = sorted(self.bids.items(), reverse=True)[:depth]
        asks = sorted(self.asks.items())[:depth]
        return BookTop(tuple(bids), tuple(asks))
