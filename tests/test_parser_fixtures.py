"""The parsers, run against real Gamma API responses saved in tests/fixtures.

The fixtures were captured on 2026-11-11. They hold public market data only
(on-chain address fields were removed). If Polymarket renames a field, saving a
fresh sample here is the quickest way to see what PolyBot would misread.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from conftest import load_fixture

from polybot.agent import to_market_view
from polybot.db import connect
from polybot.pricing import entry_price
from polybot.resolutions import outcome_of, resolved_at_of
from polybot.storage import _event_info, _parse_yes_no_prices, save_snapshot

NOW = datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)
PAGE = load_fixture("gamma_markets_page.json")
WITH_BOOK = [m for m in PAGE if "bestBid" in m and "bestAsk" in m]
NO_BIDS = [m for m in PAGE if "bestBid" not in m]


def test_fixture_has_both_kinds_of_market():
    assert len(WITH_BOOK) >= 3 and len(NO_BIDS) >= 1


@pytest.mark.parametrize("market", PAGE, ids=lambda m: m["id"])
def test_prices_parse_from_the_json_encoded_string(market):
    assert isinstance(market["outcomePrices"], str)  # the API sends a string holding JSON, not a list
    yes, no = _parse_yes_no_prices(market)
    assert 0.0 <= yes <= 1.0 and yes + no == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("market", PAGE, ids=lambda m: m["id"])
def test_every_listed_market_becomes_a_market_view(market):
    view = to_market_view(market, NOW)
    assert view is not None and view.market_id == market["id"]
    assert view.question == market["question"] and view.days_left > 0
    assert view.liquidity >= 0 and view.volume > 0


@pytest.mark.parametrize("market", WITH_BOOK, ids=lambda m: m["id"])
def test_book_fields_give_executable_prices(market):
    view = to_market_view(market, NOW)
    assert 0 < view.best_bid < view.best_ask < 1
    assert entry_price("YES", view.best_bid, view.best_ask) == view.best_ask
    assert entry_price("NO", view.best_bid, view.best_ask) == pytest.approx(1 - view.best_bid)


@pytest.mark.parametrize("market", NO_BIDS, ids=lambda m: m["id"])
def test_market_with_no_bids_cannot_be_sold_into(market):
    view = to_market_view(market, NOW)
    assert view.best_bid is None  # bestBid is simply absent from the response
    assert entry_price("NO", view.best_bid, view.best_ask) is None


def test_event_id_comes_from_the_nested_events_list():
    event_id, _neg_risk = _event_info(PAGE[0])
    assert event_id == str(PAGE[0]["events"][0]["id"])


def test_token_ids_and_outcomes_are_json_strings_of_two_items():
    for market in PAGE:
        assert len(json.loads(market["clobTokenIds"])) == 2
        assert len(json.loads(market["outcomes"])) == 2


def test_snapshot_stores_real_markets_and_leaves_missing_bid_empty():
    assert save_snapshot(PAGE) == len(PAGE)
    with connect() as c:
        rows = {r["market_id"]: r for r in c.execute("SELECT * FROM snapshots")}
    assert len(rows) == len(PAGE)
    assert all(rows[m["id"]]["best_bid"] is None for m in NO_BIDS)
    assert all(rows[m["id"]]["best_bid"] == m["bestBid"] for m in WITH_BOOK)
    assert all(rows[m["id"]]["volume"] == pytest.approx(float(m["volume"])) for m in PAGE)


def test_open_markets_have_no_outcome_yet():
    assert all(outcome_of(m) is None for m in PAGE)


def test_resolved_yes_market():
    market = load_fixture("gamma_market_resolved_yes.json")
    assert outcome_of(market) == "YES"
    assert resolved_at_of(market).startswith("2026-11-11T09:32:08")  # closedTime uses a space and "+00"


def test_resolved_no_market():
    market = load_fixture("gamma_market_resolved_no.json")
    assert outcome_of(market) == "NO"
    assert resolved_at_of(market).startswith("2026-11-11T17:15:22")
