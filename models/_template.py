"""Template for a forecast model. Copy this file to a new name without the leading underscore."""

NAME = "my_model"
DESCRIPTION = "One sentence on the idea being tested."


def predict(market: dict) -> float | None:
    """Return the probability of YES (0 to 1) for one market, or None to pass on it.

    ``market`` holds only what was known at that moment; see README.md in this folder.
    """
    return market["price"]
