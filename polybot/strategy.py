"""Paper strategies: rules that turn a model's probability and a market's prices into an order.

A model says how likely YES is. A strategy decides whether that opinion is
worth acting on once the real cost of trading is counted. The one rule shipped
here, :class:`EdgeThreshold`, is the simplest honest version:

* **Edge** is the model's probability minus what a share really costs (the ask
  price plus the fee). If the model says 60% and a YES share costs 0.55 all-in,
  the edge is 5 points.
* **Buy** the side whose edge is largest, if it beats ``min_edge``.
* **Sell** a position once its edge has shrunk to ``exit_edge`` or less, meaning
  the market will now pay about what the model thinks the share is worth.

To try a different rule, write another class with the same ``decide`` method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from polybot.fills import Book, unit_cost, unit_proceeds


@dataclass(frozen=True)
class Order:
    """What a strategy wants done in one market."""

    action: str  # "BUY" or "SELL"
    side: str  # "YES" or "NO"
    edge: float  # the edge that triggered it, after fees
    max_unit_cost: float  # buys only: the most a share may cost all-in and still leave min_edge
    reason: str  # one line, kept in the ledger so every trade can be explained later


class Strategy(Protocol):
    """Anything with a name and a ``decide`` method can drive the paper account."""

    name: str

    def decide(self, probability: float, book: Book, fee_rate: float, holding: str | None) -> Order | None:
        """Return an order, or None to do nothing. ``holding`` is "YES", "NO" or None."""


@dataclass(frozen=True)
class EdgeThreshold:
    """Buy when the model beats the all-in cost by ``min_edge``; sell when the edge is gone."""

    min_edge: float
    exit_edge: float | None = 0.0  # None means never sell early: hold until the market resolves
    name: str = "edge_threshold"

    def decide(self, probability: float, book: Book, fee_rate: float, holding: str | None) -> Order | None:
        if holding is not None:
            return self._exit(probability, book, fee_rate, holding)
        return self._entry(probability, book, fee_rate)

    def _entry(self, probability: float, book: Book, fee_rate: float) -> Order | None:
        best: Order | None = None
        for side, fair in (("YES", probability), ("NO", 1 - probability)):
            levels = book.buy_levels(side)
            if not levels:
                continue
            price = levels[0][0]
            edge = fair - unit_cost(price, fee_rate)
            if edge > self.min_edge and (best is None or edge > best.edge):
                reason = (
                    f"model says {side} is worth {fair:.3f}; it costs {unit_cost(price, fee_rate):.3f} "
                    f"with the fee, an edge of {edge:+.3f}"
                )
                best = Order("BUY", side, edge, fair - self.min_edge, reason)
        return best

    def _exit(self, probability: float, book: Book, fee_rate: float, holding: str) -> Order | None:
        levels = book.sell_levels(holding)
        if self.exit_edge is None or not levels:
            return None
        fair = probability if holding == "YES" else 1 - probability
        net = unit_proceeds(levels[0][0], fee_rate)
        edge = fair - net  # what holding is still worth to the model, over what selling pays now
        if edge > self.exit_edge:
            return None
        reason = (
            f"model says {holding} is worth {fair:.3f}; selling pays {net:.3f} after the fee, "
            f"so the edge is gone ({edge:+.3f})"
        )
        return Order("SELL", holding, edge, 0.0, reason)
