"""Paging through the market listing: full pages, short pages, and pages that fail."""

from __future__ import annotations

import pytest

from polybot import fetch
from polybot.http import FetchError


def page_of(first_id: int, size: int = fetch.PAGE_SIZE, next_cursor: str | None = None) -> dict:
    """One keyset response: ``size`` markets, and a cursor if another page follows."""
    body: dict = {"markets": [{"id": str(first_id + i)} for i in range(size)]}
    if next_cursor:
        body["next_cursor"] = next_cursor
    return body


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(fetch, "PAGE_PAUSE", 0)


def fake_listing(monkeypatch, pages: dict[str | None, object]) -> list[str | None]:
    """Serve ``pages[cursor]`` (None is the first page); an exception stored there is raised.

    Returns the cursors that were requested, in order.
    """
    asked: list[str | None] = []

    def get_json(url, params=None, **kwargs):
        assert url.endswith("/markets/keyset") and "offset" not in params
        cursor = params.get("after_cursor")
        asked.append(cursor)
        result = pages[cursor]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(fetch.http, "get_json", get_json)
    return asked


def test_reads_pages_until_the_limit(monkeypatch):
    asked = fake_listing(
        monkeypatch,
        {None: page_of(0, next_cursor="a"), "a": page_of(100, next_cursor="b"), "b": page_of(200, next_cursor="c")},
    )
    markets = fetch.fetch_active_markets(limit=250)
    assert len(markets) == 250 and asked == [None, "a", "b"]


def test_stops_when_there_is_no_next_cursor(monkeypatch):
    asked = fake_listing(monkeypatch, {None: page_of(0, next_cursor="a"), "a": page_of(100, size=40)})
    assert len(fetch.fetch_active_markets(limit=500)) == 140 and asked == [None, "a"]


def test_asks_for_open_markets_by_volume(monkeypatch):
    sent = {}

    def get_json(url, params=None, **kwargs):
        sent.update(params)
        return page_of(0, size=1)

    monkeypatch.setattr(fetch.http, "get_json", get_json)
    fetch.fetch_active_markets(limit=1)
    assert (sent["closed"], sent["order"], sent["ascending"], sent["limit"]) == ("false", "volumeNum", "false", 100)
    assert sent["include_tag"] == "true"
    assert sent["end_date_min"].endswith("Z")


def test_market_listed_twice_is_kept_once(monkeypatch):
    # Volumes change while paging, so a market can slide from one page onto the next.
    fake_listing(monkeypatch, {None: page_of(0, size=3, next_cursor="a"), "a": page_of(2, size=3)})
    assert [m["id"] for m in fetch.fetch_active_markets(limit=500)] == ["0", "1", "2", "3", "4"]


def test_keeps_loaded_pages_when_a_later_page_fails(monkeypatch):
    fake_listing(
        monkeypatch,
        {None: page_of(0, next_cursor="a"), "a": page_of(100, next_cursor="b"), "b": FetchError("HTTP 500")},
    )
    assert len(fetch.fetch_active_markets(limit=500)) == 200


def test_first_page_failing_is_an_error(monkeypatch):
    fake_listing(monkeypatch, {None: FetchError("HTTP 500")})
    with pytest.raises(FetchError):
        fetch.fetch_active_markets(limit=500)


def by_id_server(monkeypatch, known: set[str], fail_batches: set[int] = frozenset()) -> list[list[str]]:
    """Answer batched ID lookups with the IDs in ``known``. Returns the batches that were asked for."""
    batches: list[list[str]] = []

    def get_json(url, params=None, **kwargs):
        assert url.endswith("/markets/keyset")
        asked = [value for key, value in params if key == "id"]
        assert ("closed", "any") in params and ("include_tag", "true") in params
        batches.append(asked)
        if len(batches) - 1 in fail_batches:
            raise FetchError("HTTP 500")
        return {"markets": [{"id": i} for i in asked if i in known]}

    monkeypatch.setattr(fetch.http, "get_json", get_json)
    return batches


def test_by_id_asks_for_fifty_at_a_time(monkeypatch):
    ids = [str(i) for i in range(120)]
    batches = by_id_server(monkeypatch, known=set(ids))
    assert [m["id"] for m in fetch.fetch_markets_by_id(ids)] == ids
    assert [len(b) for b in batches] == [50, 50, 20]


def test_by_id_leaves_out_markets_the_api_does_not_know(monkeypatch):
    by_id_server(monkeypatch, known={"1", "3"})
    assert [m["id"] for m in fetch.fetch_markets_by_id(["1", "2", "3"])] == ["1", "3"]


def test_by_id_skips_a_batch_that_fails_and_carries_on(monkeypatch):
    ids = [str(i) for i in range(120)]
    by_id_server(monkeypatch, known=set(ids), fail_batches={1})
    assert len(fetch.fetch_markets_by_id(ids)) == 70


def test_by_id_stops_asking_when_the_api_is_down(monkeypatch):
    ids = [str(i) for i in range(1000)]
    batches = by_id_server(monkeypatch, known=set(ids), fail_batches=set(range(100)))
    assert fetch.fetch_markets_by_id(ids) == []
    assert len(batches) == fetch.MAX_BY_ID_FAILURES


def test_by_id_with_nothing_to_ask_makes_no_request(monkeypatch):
    assert fetch.fetch_markets_by_id([]) == [] and by_id_server(monkeypatch, known=set()) == []
