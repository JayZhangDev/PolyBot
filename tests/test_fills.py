"""Simulated fills: the ask or the bid, limited depth, and Polymarket's fee, checked to the cent."""

from __future__ import annotations

import pytest

from polybot.fills import Book, fee_per_share, simulate_buy, simulate_sell, unit_cost, unit_proceeds

BOOK = Book(bids=((0.40, 100.0), (0.39, 200.0)), asks=((0.42, 50.0), (0.43, 100.0), (0.45, 1000.0)))


def test_fee_is_rate_times_price_times_one_minus_price():
    assert fee_per_share(0.50, 0.07) == pytest.approx(0.0175)  # Polymarket's own example: $1.75 per 100 shares
    assert fee_per_share(0.10, 0.04) == pytest.approx(0.0036)  # smaller towards the ends
    assert fee_per_share(0.50, 0.0) == 0
    assert unit_cost(0.50, 0.07) == pytest.approx(0.5175) and unit_proceeds(0.50, 0.07) == pytest.approx(0.4825)


def test_book_midpoint_and_best_prices():
    assert (BOOK.best_bid, BOOK.best_ask, BOOK.mid) == (0.40, 0.42, pytest.approx(0.41))
    assert Book((), BOOK.asks).mid is None and Book((), ()).best_ask is None


def test_no_side_mirrors_the_yes_book():
    assert BOOK.buy_levels("YES") == BOOK.asks
    assert BOOK.buy_levels("NO") == ((0.60, 100.0), (0.61, 200.0))  # buying NO takes the YES bids
    assert BOOK.sell_levels("YES") == BOOK.bids
    assert BOOK.sell_levels("NO") == ((0.58, 50.0), (0.57, 100.0), (0.55, 1000.0))


def test_small_buy_fills_at_the_ask_not_the_midpoint():
    fill = simulate_buy(BOOK.buy_levels("YES"), budget=10.0, rate=0.0)
    assert fill.avg_price == 0.42 and fill.shares == pytest.approx(10 / 0.42)
    assert fill.dollars == pytest.approx(10.0) and fill.fees == 0 and fill.limited_by == "budget"


def test_bigger_buy_walks_up_the_levels():
    fill = simulate_buy(BOOK.buy_levels("YES"), budget=50 * 0.42 + 100 * 0.43 + 10 * 0.45, rate=0.0)
    assert fill.shares == pytest.approx(160.0)
    assert fill.avg_price == pytest.approx((21 + 43 + 4.5) / 160) and fill.limited_by == "budget"


def test_buy_cannot_take_more_than_the_book_held():
    thin = ((0.42, 50.0), (0.43, 20.0))
    fill = simulate_buy(thin, budget=1_000.0, rate=0.0)
    assert fill.shares == pytest.approx(70.0) and fill.dollars == pytest.approx(50 * 0.42 + 20 * 0.43)
    assert fill.limited_by == "depth"


def test_buy_stops_at_the_price_limit():
    fill = simulate_buy(BOOK.buy_levels("YES"), budget=1_000.0, rate=0.0, max_unit_cost=0.435)
    assert fill.shares == pytest.approx(150.0) and fill.limited_by == "price"
    assert simulate_buy(BOOK.buy_levels("YES"), 1_000.0, 0.0, max_unit_cost=0.41).shares == 0


def test_fee_is_added_to_a_buy_and_counts_against_the_budget():
    fill = simulate_buy(((0.50, 1_000.0),), budget=51.75, rate=0.07)
    assert fill.shares == pytest.approx(100.0) and fill.fees == pytest.approx(1.75)
    assert fill.dollars == pytest.approx(51.75) and fill.avg_price == 0.50


def test_price_limit_counts_the_fee():
    levels = ((0.50, 100.0),)
    assert simulate_buy(levels, 100.0, rate=0.07, max_unit_cost=0.51).shares == 0  # 0.5175 all-in is too dear
    assert simulate_buy(levels, 100.0, rate=0.07, max_unit_cost=0.52).shares > 0


def test_sell_fills_at_the_bid_and_pays_the_fee():
    fill = simulate_sell(BOOK.sell_levels("YES"), 100.0, rate=0.05)
    assert fill.shares == 100.0 and fill.avg_price == 0.40 and fill.fees == pytest.approx(100 * 0.05 * 0.4 * 0.6)
    assert fill.dollars == pytest.approx(40.0 - 1.2) and fill.limited_by == "shares"


def test_sell_walks_down_and_can_only_sell_what_the_book_will_take():
    fill = simulate_sell(BOOK.sell_levels("YES"), 250.0, rate=0.0)
    assert fill.shares == 250.0 and fill.dollars == pytest.approx(100 * 0.40 + 150 * 0.39)
    partial = simulate_sell(BOOK.sell_levels("YES"), 1_000.0, rate=0.0)
    assert partial.shares == 300.0 and partial.limited_by == "depth"


def test_empty_book_fills_nothing():
    assert simulate_buy((), 100.0, 0.05).shares == 0 and simulate_sell((), 10.0, 0.05).shares == 0
