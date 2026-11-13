"""Executable entry prices.

Polymarket's `outcomePrices` is a midpoint, and nobody can actually trade at the
midpoint. A buyer of YES pays the best ask; a buyer of NO is effectively selling
YES, so pays 1 - best bid. Filling paper trades at those prices keeps paper P&L
honest about the spread.

There is no fallback to the midpoint. The API leaves `bestBid` out when nobody
is bidding (typical for a market priced near zero), and filling at the midpoint
there would invent a trade that no real order could get. With no quote on the
side we need, there is no executable price and the trade is skipped.
"""

from __future__ import annotations


def _valid(p: float | None) -> bool:
    return p is not None and 0.0 < p < 1.0


def entry_price(side: str, best_bid: float | None, best_ask: float | None) -> float | None:
    """Price paid per share to buy `side` ('YES' or 'NO'), or None when it cannot be bought."""
    if side == "YES":
        return best_ask if _valid(best_ask) else None
    if side == "NO":
        return round(1.0 - best_bid, 6) if _valid(best_bid) else None
    raise ValueError(f"side must be 'YES' or 'NO', got {side!r}")
