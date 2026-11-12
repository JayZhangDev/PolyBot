"""Momentum: does a price that moved yesterday keep moving the same way?

If markets react too slowly to news, a price that rose 10 points in a day has
further to go. This model adds half of the last day's move to the price. If
markets instead overreact, this model will score worse than the price, which
is just as useful to know.
"""

NAME = "momentum_24h"
DESCRIPTION = "The price plus half of its move over the previous 24 hours."
CONTINUATION = 0.5


def predict(market: dict) -> float | None:
    change = market["change_24h"]
    if change is None:
        return None  # no price from a day earlier, so nothing to go on
    return min(0.99, max(0.01, market["price"] + CONTINUATION * change))
