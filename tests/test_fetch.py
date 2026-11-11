"""Paging through the market listing: full pages, short pages, and pages that fail."""

from __future__ import annotations

import pytest

from polybot import fetch
from polybot.http import FetchError


def page_of(offset: int, size: int = fetch.PAGE_SIZE) -> list[dict]:
    return [{"id": str(offset + i)} for i in range(size)]


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(fetch, "PAGE_PAUSE", 0)


def fake_listing(monkeypatch, pages: dict[int, object]) -> list[int]:
    """Serve ``pages[offset]``; an exception stored there is raised. Returns the offsets requested."""
    asked: list[int] = []

    def get_json(url, params=None, **kwargs):
        asked.append(params["offset"])
        result = pages[params["offset"]]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(fetch.http, "get_json", get_json)
    return asked


def test_reads_pages_until_the_limit(monkeypatch):
    asked = fake_listing(monkeypatch, {0: page_of(0), 100: page_of(100), 200: page_of(200)})
    markets = fetch.fetch_active_markets(limit=250)
    assert len(markets) == 250 and asked == [0, 100, 200]


def test_stops_at_a_short_page(monkeypatch):
    asked = fake_listing(monkeypatch, {0: page_of(0), 100: page_of(100, size=40)})
    assert len(fetch.fetch_active_markets(limit=500)) == 140 and asked == [0, 100]


def test_keeps_loaded_pages_when_a_later_page_fails(monkeypatch):
    fake_listing(monkeypatch, {0: page_of(0), 100: page_of(100), 200: FetchError("HTTP 500")})
    assert len(fetch.fetch_active_markets(limit=500)) == 200


def test_first_page_failing_is_an_error(monkeypatch):
    fake_listing(monkeypatch, {0: FetchError("HTTP 500")})
    with pytest.raises(FetchError):
        fetch.fetch_active_markets(limit=500)


def test_by_id_skips_a_market_that_fails(monkeypatch):
    def get_json(url, params=None, **kwargs):
        if url.endswith("/2"):
            raise FetchError("HTTP 404")
        return {"id": url.rsplit("/", 1)[1]}

    monkeypatch.setattr(fetch.http, "get_json", get_json)
    assert [m["id"] for m in fetch.fetch_markets_by_id(["1", "2", "3"])] == ["1", "3"]


def test_by_id_stops_asking_when_the_api_is_down(monkeypatch):
    asked = []

    def get_json(url, params=None, **kwargs):
        asked.append(url)
        raise FetchError("HTTP 500")

    monkeypatch.setattr(fetch.http, "get_json", get_json)
    assert fetch.fetch_markets_by_id([str(i) for i in range(50)]) == []
    assert len(asked) == fetch.MAX_BY_ID_FAILURES
