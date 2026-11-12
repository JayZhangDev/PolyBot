"""The order book: snapshots, level changes, and the best few levels."""

from __future__ import annotations

import pytest

from polybot.book import BookTop, OrderBook


def levels(*pairs: tuple[str, str]) -> list[dict]:
    return [{"price": price, "size": size} for price, size in pairs]


def test_snapshot_gives_best_levels_best_first():
    book = OrderBook()
    book.replace(levels(("0.40", "100"), ("0.42", "50"), ("0.41", "75")), levels(("0.45", "20"), ("0.44", "10")))
    top = book.top()
    assert top.bids == ((0.42, 50.0), (0.41, 75.0), (0.40, 100.0))  # highest bid first
    assert top.asks == ((0.44, 10.0), (0.45, 20.0))  # lowest ask first
    assert (top.best_bid, top.best_ask, top.spread) == (0.42, 0.44, 0.02)
    assert (top.bid_depth, top.ask_depth) == (225.0, 30.0)


def test_only_the_requested_depth_is_returned():
    book = OrderBook()
    book.replace(levels(*[(f"0.{n:02d}", "10") for n in range(1, 21)]), [])
    assert [price for price, _size in book.top(depth=5).bids] == [0.20, 0.19, 0.18, 0.17, 0.16]


def test_change_adds_updates_and_removes_a_level():
    book = OrderBook()
    book.replace(levels(("0.42", "50")), levels(("0.44", "10")))
    book.update("BUY", 0.43, 30.0)  # a new, better bid
    book.update("SELL", 0.44, 25.0)  # the ask's size changed
    assert book.top().bids[0] == (0.43, 30.0) and book.top().asks[0] == (0.44, 25.0)
    book.update("BUY", 0.43, 0.0)  # size 0 means the level is gone
    assert book.top().best_bid == 0.42
    book.update("BUY", 0.99, 0.0)  # removing a level we never had is harmless
    assert len(book.bids) == 1


def test_new_snapshot_replaces_everything():
    book = OrderBook()
    book.replace(levels(("0.42", "50")), levels(("0.44", "10")))
    book.replace(levels(("0.10", "5")), [])
    assert book.top() == BookTop(bids=((0.10, 5.0),), asks=())


def test_empty_levels_in_a_snapshot_are_dropped():
    book = OrderBook()
    book.replace(levels(("0.42", "0"), ("0.41", "5")), [])
    assert book.top().bids == ((0.41, 5.0),)


def test_empty_side_has_no_best_price_and_no_spread():
    book = OrderBook()
    book.replace([], levels(("0.001", "5000")))
    top = book.top()
    assert top.best_bid is None and top.best_ask == 0.001 and top.spread is None and top.bid_depth == 0


def test_unknown_side_is_rejected():
    with pytest.raises(ValueError):
        OrderBook().update("HOLD", 0.5, 1.0)
