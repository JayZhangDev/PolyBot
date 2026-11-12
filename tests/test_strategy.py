"""The edge-threshold rule: buy only when the model beats the real cost, sell when the edge is gone."""

from __future__ import annotations

import pytest

from polybot.fills import Book
from polybot.strategy import EdgeThreshold

BOOK = Book(bids=((0.40, 100.0),), asks=((0.42, 100.0),))
RULE = EdgeThreshold(min_edge=0.03, exit_edge=0.0)


def test_buys_yes_when_the_model_is_well_above_the_ask():
    order = RULE.decide(0.50, BOOK, 0.0, None)
    assert (order.action, order.side) == ("BUY", "YES") and order.edge == pytest.approx(0.08)
    assert order.max_unit_cost == pytest.approx(0.47)  # pay up to here and still keep 3 points of edge
    assert "worth 0.500" in order.reason and "+0.080" in order.reason


def test_buys_no_when_the_model_is_well_below_the_bid():
    order = RULE.decide(0.30, BOOK, 0.0, None)
    assert (order.action, order.side) == ("BUY", "NO")
    assert order.edge == pytest.approx(0.70 - 0.60)  # NO is worth 0.70 and costs 1 - 0.40


def test_does_nothing_when_the_model_agrees_with_the_market():
    assert RULE.decide(0.41, BOOK, 0.0, None) is None  # inside the spread: neither side is cheap
    assert RULE.decide(0.44, BOOK, 0.0, None) is None  # 2 points over the ask, below the 3-point threshold


def test_fee_can_remove_the_edge():
    book = Book(bids=((0.48, 100.0),), asks=((0.50, 100.0),))
    assert RULE.decide(0.54, book, 0.0, None).side == "YES"  # 4 points with no fee
    assert RULE.decide(0.54, book, 0.07, None) is None  # the fee of 1.75 points leaves only 2.25


def test_one_sided_book_can_only_be_bought_on_the_side_that_exists():
    no_bids = Book(bids=(), asks=((0.05, 100.0),))
    assert RULE.decide(0.01, no_bids, 0.0, None) is None  # NO looks cheap, but nobody is bidding YES
    assert RULE.decide(0.20, no_bids, 0.0, None).side == "YES"


def test_holds_while_the_edge_remains_and_sells_when_it_is_gone():
    assert RULE.decide(0.50, BOOK, 0.0, "YES") is None  # worth 0.50, selling pays 0.40: keep it
    order = RULE.decide(0.40, BOOK, 0.0, "YES")
    assert (order.action, order.side) == ("SELL", "YES") and "edge is gone" in order.reason
    assert RULE.decide(0.70, BOOK, 0.0, "NO").action == "SELL"  # NO worth 0.30, selling NO pays 1 - 0.42 = 0.58


def test_no_exit_rule_means_holding_to_the_end():
    assert EdgeThreshold(0.03, exit_edge=None).decide(0.10, BOOK, 0.0, "YES") is None


def test_cannot_sell_into_an_empty_side():
    assert RULE.decide(0.01, Book(bids=(), asks=((0.05, 10.0),)), 0.0, "YES") is None
