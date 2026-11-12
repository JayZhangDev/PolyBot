"""Favorite-longshot bias: are unlikely outcomes priced a little too high?

In betting markets people tend to overpay for longshots (it is fun to hold a
ticket that could pay 20 to 1), which leaves favorites slightly underpriced.
If that happens on Polymarket, the true chance of a 5% market is below 5% and
the true chance of a 95% market is above 95%.

This model pushes every price a little further from 50%. It works in *log-odds*
(the logarithm of p / (1 - p)), which stretch out the ends of the 0-to-1 scale,
and multiplies them by ``STRENGTH``. At 1.0 the model would equal the price.
"""

import math

NAME = "favorite_longshot"
DESCRIPTION = "Pushes each price further from 50%: longshots a little lower, favorites a little higher."
STRENGTH = 1.1


def predict(market: dict) -> float | None:
    price = market["price"]
    if not 0 < price < 1:
        return None
    log_odds = math.log(price / (1 - price))
    return 1 / (1 + math.exp(-STRENGTH * log_odds))
