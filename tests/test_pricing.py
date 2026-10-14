import pytest

from polybot.pricing import entry_price


def test_yes_pays_the_ask():
    assert entry_price("YES", 0.50, 0.50, best_bid=0.49, best_ask=0.52) == 0.52


def test_no_pays_one_minus_the_bid():
    assert entry_price("NO", 0.50, 0.50, best_bid=0.48, best_ask=0.52) == pytest.approx(0.52)


def test_falls_back_to_mid_without_book():
    assert entry_price("YES", 0.30, 0.70, None, None) == 0.30
    assert entry_price("NO", 0.30, 0.70, None, None) == 0.70


def test_ignores_degenerate_book_values():
    assert entry_price("YES", 0.30, 0.70, best_bid=0.0, best_ask=1.0) == 0.30
    assert entry_price("NO", 0.30, 0.70, best_bid=0.0, best_ask=1.0) == 0.70


def test_no_price_available_returns_none():
    assert entry_price("YES", None, None, None, None) is None


def test_rejects_unknown_side():
    with pytest.raises(ValueError):
        entry_price("MAYBE", 0.5, 0.5, None, None)
