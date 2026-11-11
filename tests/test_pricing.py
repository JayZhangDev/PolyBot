import pytest

from polybot.pricing import entry_price


def test_yes_pays_the_ask():
    assert entry_price("YES", best_bid=0.49, best_ask=0.52) == 0.52


def test_no_pays_one_minus_the_bid():
    assert entry_price("NO", best_bid=0.48, best_ask=0.52) == pytest.approx(0.52)


def test_no_quotes_means_no_price_instead_of_the_midpoint():
    assert entry_price("YES", None, None) is None
    assert entry_price("NO", None, None) is None


def test_missing_bid_blocks_no_but_not_yes():
    # The API omits bestBid when nobody is bidding. YES can still be bought at the ask,
    # but NO (which is filled against the YES bid) cannot be bought at all.
    assert entry_price("YES", best_bid=None, best_ask=0.001) == 0.001
    assert entry_price("NO", best_bid=None, best_ask=0.001) is None


def test_ignores_degenerate_book_values():
    assert entry_price("YES", best_bid=0.0, best_ask=1.0) is None
    assert entry_price("NO", best_bid=0.0, best_ask=1.0) is None


def test_rejects_unknown_side():
    with pytest.raises(ValueError):
        entry_price("MAYBE", 0.5, 0.5)
