# Forecast models

Each `.py` file in this folder is one model. The calibration tracker loads them all, asks each
for a probability on every resolved market, and scores it against the market's own price
(`python -m polybot calibrate`, report in `reports/calibration.md`).

## Writing one

Copy `_template.py` to a new name (files starting with `_` are ignored) and fill it in:

```python
NAME = "my_model"
DESCRIPTION = "One sentence on the idea being tested."


def predict(market: dict) -> float | None:
    """Return the probability of YES, between 0 and 1. Return None to pass on a market."""
    return market["price"]
```

## What a model is given

`market` holds only what was known at that moment. It never includes the outcome.

| Key | Meaning |
|---|---|
| `market_id`, `question`, `category` | which market this is |
| `price` | the market's YES price, between 0 and 1 |
| `best_bid`, `best_ask` | the best prices to sell and buy YES at (either can be `None`) |
| `volume`, `liquidity` | dollars traded so far, and dollars of resting orders |
| `days_to_end` | scheduled days until the market closes (can be `None`) |
| `change_24h` | the price minus the price a day earlier (`None` if there is no earlier price) |

## Rules that keep a model honest

- **Use only what is in `market`.** The tracker replays history, so a model that looks anything up
  from outside (news, another site, today's price) would be peeking at the future. Forecasts that
  need outside information have to be recorded at the time they are made instead.
- **Returning the price itself scores exactly the same as the market.** A model only shows skill
  where it disagrees with the price and turns out closer.
- **A handful of markets proves nothing.** The report gives the gain per market with its standard
  error and says "no clear difference" unless the gain is more than about twice that error.
