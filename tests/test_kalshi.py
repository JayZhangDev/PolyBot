"""The Kalshi client, against a fake API (no network)."""

from __future__ import annotations

from polybot import kalshi
from polybot.http import FetchError
from polybot.kalshi import market_from_api


def api_market(ticker: str = "KXTEST-26-A", **changes) -> dict:
    """A market as Kalshi's API returns it, with only the fields PolyBot reads."""
    m = {
        "ticker": ticker,
        "event_ticker": "KXTEST-26",
        "title": "Will Alpha win the 2026 Test Cup?",
        "yes_sub_title": "Alpha",
        "status": "active",
        "yes_bid_dollars": "0.4100",
        "yes_ask_dollars": "0.4300",
        "last_price_dollars": "0.4200",
        "volume_24h_fp": "1234.00",
        "close_time": "2026-12-31T23:59:00Z",
    }
    m.update(changes)
    return m


def test_prices_are_read_from_dollar_strings():
    m = market_from_api(api_market())
    assert (m.yes_bid, m.yes_ask, m.last_price, m.volume_24h) == (0.41, 0.43, 0.42, 1234.0)
    assert m.status == "active" and m.close_time == "2026-12-31T23:59:00Z"


def test_an_empty_side_of_the_book_has_no_price():
    m = market_from_api(api_market(yes_bid_dollars="0.0000", yes_ask_dollars="1.0000", last_price_dollars=None))
    assert (m.yes_bid, m.yes_ask, m.last_price) == (None, None, None)


def test_the_question_falls_back_to_the_event_title_and_adds_the_answer():
    event = {"event_ticker": "KXIPO-26", "title": "Who will IPO before 2027?"}
    m = market_from_api(api_market(title=None, yes_sub_title="Anthropic", event_ticker=None), event)
    assert m.title == "Who will IPO before 2027?" and m.event_ticker == "KXIPO-26"
    assert m.text == "Who will IPO before 2027? Anthropic"
    assert market_from_api(api_market()).text == "Will Alpha win the 2026 Test Cup?"  # the answer is already there


def test_listing_follows_the_cursor_skips_closed_markets_and_pauses_between_pages():
    pages = {
        "": {
            "events": [
                {
                    "event_ticker": "E1",
                    "title": "One",
                    "markets": [api_market("M1"), api_market("M2", status="finalized")],
                }
            ],
            "cursor": "page2",
        },
        "page2": {"events": [{"event_ticker": "E2", "title": "Two", "markets": [api_market("M3")]}], "cursor": ""},
    }
    calls, pauses = [], []

    def get(url, params):
        calls.append(dict(params))
        return pages[params.get("cursor", "")]

    markets = list(kalshi.iter_open_markets(get=get, sleep=pauses.append))
    assert [m.ticker for m in markets] == ["M1", "M3"]
    assert calls[0]["status"] == "open" and calls[0]["with_nested_markets"] == "true" and "cursor" not in calls[0]
    assert calls[1]["cursor"] == "page2"
    assert pauses == [kalshi.PAGE_PAUSE_SECONDS]  # one polite pause, between the two pages


def test_prices_come_in_batches_and_a_failed_batch_is_skipped():
    tickers = [f"T{i}" for i in range(120)]
    pauses = []

    def get(url, params):
        batch = params["tickers"].split(",")
        if batch[0] == "T50":
            raise FetchError("Kalshi is down for a moment")
        return {"markets": [api_market(t) for t in batch]}

    found = kalshi.fetch_markets(tickers, get=get, sleep=pauses.append)
    assert len(found) == 70 and "T49" in found and "T50" not in found and "T119" in found
    assert len(pauses) == 2  # between the three batches of up to 50
