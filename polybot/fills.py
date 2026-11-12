"""Simulated fills: what a paper order would really have cost against the recorded order book.

Three things make a paper result honest, and all three are here:

* **You trade at the ask or the bid, never the midpoint.** A buyer pays what
  sellers are asking; a seller gets what buyers are bidding.
* **Depth is limited.** Each price level has only so many shares. A bigger order
  eats through the best level and continues at worse prices, and it can never
  fill more than the book held.
* **Fees.** Polymarket charges the taker ``shares x rate x price x (1 - price)``,
  which is largest near 50% and shrinks towards 0% and 100%.

A market has two tokens, YES and NO, and their books mirror each other: someone
bidding 0.30 for YES is, in effect, offering NO at 0.70. Only the YES book is
recorded, so the NO book is derived from it here.

No network, no files: plain arithmetic that the tests can check to the cent.
"""

from __future__ import annotations

from dataclasses import dataclass

Level = tuple[float, float]  # (price, shares available at that price)
FEE_DECIMALS = 5  # Polymarket rounds fees to five decimal places


@dataclass(frozen=True)
class Book:
    """The recorded top of a market's YES order book: best levels first on each side."""

    bids: tuple[Level, ...]  # buyers of YES, highest price first
    asks: tuple[Level, ...]  # sellers of YES, lowest price first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        """Halfway between the best bid and ask, or None if one side is empty."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def buy_levels(self, side: str) -> tuple[Level, ...]:
        """Levels a buyer of ``side`` lifts, cheapest first. Buying NO takes the YES bids, mirrored."""
        if side == "YES":
            return self.asks
        return tuple((round(1 - price, 6), size) for price, size in self.bids)

    def sell_levels(self, side: str) -> tuple[Level, ...]:
        """Levels a seller of ``side`` hits, best price first. Selling NO takes the YES asks, mirrored."""
        if side == "YES":
            return self.bids
        return tuple((round(1 - price, 6), size) for price, size in self.asks)


@dataclass(frozen=True)
class Fill:
    """The result of a simulated order. ``shares`` is 0 when nothing could be filled."""

    shares: float
    avg_price: float  # average price per share, before fees
    fees: float
    dollars: float  # buy: total paid including fees. Sell: total received after fees.
    limited_by: str  # what stopped the order growing: "budget", "depth", "price" or "shares"


NO_FILL = Fill(0.0, 0.0, 0.0, 0.0, "depth")


def fee_per_share(price: float, rate: float) -> float:
    """The taker fee on one share at ``price``: ``rate x price x (1 - price)``."""
    return rate * price * (1 - price)


def unit_cost(price: float, rate: float) -> float:
    """What one share really costs a buyer: the price plus the fee."""
    return price + fee_per_share(price, rate)


def unit_proceeds(price: float, rate: float) -> float:
    """What one share really brings a seller: the price minus the fee."""
    return price - fee_per_share(price, rate)


def simulate_buy(levels: tuple[Level, ...], budget: float, rate: float, *, max_unit_cost: float = 1.0) -> Fill:
    """Spend up to ``budget`` dollars walking up the levels, cheapest first.

    A level is used only while a share there costs no more than ``max_unit_cost``
    all-in (price plus fee): that is how a strategy says "I only want this while
    it is still a good deal". Stops at the budget, at that price, or when the
    recorded book runs out.
    """
    shares = spent_on_shares = 0.0
    remaining = budget
    limited_by = "depth"
    for price, size in levels:
        cost = unit_cost(price, rate)
        if cost > max_unit_cost:
            limited_by = "price"
            break
        take = min(size, remaining / cost)
        shares += take
        spent_on_shares += take * price
        remaining -= take * cost
        if take < size:  # the budget ran out part-way through this level
            limited_by = "budget"
            break
    if shares <= 0:
        return Fill(0.0, 0.0, 0.0, 0.0, limited_by)
    fees = round(sum_fees(levels, shares, rate), FEE_DECIMALS)
    return Fill(shares, spent_on_shares / shares, fees, spent_on_shares + fees, limited_by)


def simulate_sell(levels: tuple[Level, ...], shares_to_sell: float, rate: float) -> Fill:
    """Sell up to ``shares_to_sell`` walking down the levels, best price first.

    If the book holds fewer shares than that, only what it holds is sold and
    ``limited_by`` says "depth": the rest stays in the position.
    """
    shares = received = 0.0
    for price, size in levels:
        take = min(size, shares_to_sell - shares)
        shares += take
        received += take * price
        if shares >= shares_to_sell:
            break
    if shares <= 0:
        return NO_FILL
    fees = round(sum_fees(levels, shares, rate), FEE_DECIMALS)
    return Fill(shares, received / shares, fees, received - fees, "shares" if shares >= shares_to_sell else "depth")


def sum_fees(levels: tuple[Level, ...], shares: float, rate: float) -> float:
    """Total fee for taking ``shares`` from the front of ``levels`` (each level is charged at its own price)."""
    total, left = 0.0, shares
    for price, size in levels:
        take = min(size, left)
        total += take * fee_per_share(price, rate)
        left -= take
        if left <= 1e-12:
            break
    return total
