"""Executable entry prices.

Polymarket's `outcomePrices` is a midpoint, and nobody can actually trade at the
midpoint. A buyer of YES pays the best ask; a buyer of NO is effectively selling
YES, so pays 1 - best bid. Filling paper trades at those prices keeps paper P&L
honest about the spread. When the book fields are missing, fall back to the
midpoint so older snapshots still work.
"""
from __future__ import annotations


def _valid(p: float | None) -> bool:
    return p is not None and 0.0 < p < 1.0


def entry_price(side: str, yes_mid: float | None, no_mid: float | None,
                best_bid: float | None, best_ask: float | None) -> float | None:
    """Price paid per share to buy `side` ('YES' or 'NO')."""
    if side == "YES":
        if _valid(best_ask):
            return best_ask
        return yes_mid if _valid(yes_mid) else None
    if side == "NO":
        if _valid(best_bid):
            return round(1.0 - best_bid, 6)
        return no_mid if _valid(no_mid) else None
    raise ValueError(f"side must be 'YES' or 'NO', got {side!r}")
