"""Discovery: keyset paging, parsing market metadata, and choosing a category from tags."""

from __future__ import annotations

import json

import pytest
from conftest import load_fixture

from polybot import catalog, discovery
from polybot.config import CATEGORIES, Settings
from polybot.db import connect
from polybot.discovery import category_from_tags, fetch_open_markets, parse_market
from polybot.http import FetchError

KEYSET_PAGE = load_fixture("gamma_keyset_page.json")
FLOORS = Settings(min_liquidity=10_000, min_volume=50_000)


def raw(mid: str, **extra) -> dict:
    """A minimal market as the keyset endpoint sends it."""
    market = {
        "id": mid,
        "question": f"Will {mid} happen?",
        "slug": f"m-{mid}",
        "conditionId": f"0xc{mid}",
        "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps([f"{mid}1", f"{mid}2"]),
        "volumeNum": 123456.5,
        "volume24hr": 789.0,
        "liquidityNum": 20000.25,
        "acceptingOrders": True,
        "tags": [{"slug": "politics", "label": "Politics"}],
    }
    market.update(extra)
    return market


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(discovery, "PAGE_PAUSE", 0)


def serve(monkeypatch, pages: list[object]) -> list[dict]:
    """Answer each request with the next item in ``pages`` (an exception is raised). Returns the params sent."""
    sent: list[dict] = []
    queue = list(pages)

    def get_json(url, params=None, **kwargs):
        assert url == discovery.KEYSET_URL
        sent.append(dict(params))
        page = queue.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(discovery.http, "get_json", get_json)
    return sent


# --------------------------- categories ---------------------------


@pytest.mark.parametrize(
    ("tags", "category"),
    [
        (("politics", "trump"), "politics"),
        (("politics", "geopolitics", "iran", "world"), "geopolitics"),  # the narrower category wins
        (("crypto", "bitcoin", "finance"), "crypto"),
        (("sports", "nba", "games"), "sports"),
        (("weather", "science"), "weather"),
        (("mentions", "politics", "trump"), "mentions"),
        (("some-new-tag",), "other"),
        ((), "other"),
    ],
)
def test_category_from_tags(tags, category):
    assert category_from_tags(tags) == category


def test_every_mapped_category_is_one_the_config_accepts():
    assert {name for name, _slugs in discovery.CATEGORY_TAGS} | {"other"} == set(CATEGORIES)


# --------------------------- parsing ---------------------------


def test_parse_reads_the_fields_the_catalog_needs():
    m = parse_market(raw("7", tags=[{"slug": "Crypto"}, {"slug": "bitcoin"}, {"label": "no slug"}]))
    assert (m.market_id, m.condition_id, m.question, m.slug) == ("7", "0xc7", "Will 7 happen?", "m-7")
    assert m.outcomes == ("Yes", "No") and m.token_ids == ("71", "72")
    assert (m.volume, m.volume_24h, m.liquidity) == (123456.5, 789.0, 20000.25)
    assert m.tags == ("crypto", "bitcoin") and m.category == "crypto" and m.accepting_orders is True


def test_parse_tolerates_missing_and_broken_fields():
    m = parse_market({"id": 9, "question": "Bare market?", "outcomes": "not json", "volume": "55.5"})
    assert m.market_id == "9" and m.outcomes == () and m.token_ids == ()
    assert m.volume == 55.5 and m.liquidity == 0.0 and m.category == "other" and m.accepting_orders is False


def test_parse_skips_a_market_without_id_or_question():
    assert parse_market({"question": "No id?"}) is None
    assert parse_market({"id": "1", "question": ""}) is None


@pytest.mark.parametrize("market", KEYSET_PAGE["markets"], ids=lambda m: m["id"])
def test_real_keyset_markets_parse_completely(market):
    m = parse_market(market)
    assert m is not None and m.market_id == market["id"] and m.condition_id.startswith("0x")
    assert len(m.outcomes) == 2 and len(m.token_ids) == 2 and all(t.isdigit() for t in m.token_ids)
    assert m.volume >= FLOORS.min_volume and m.liquidity >= FLOORS.min_liquidity  # the server applied the floors
    assert m.tags and m.category in CATEGORIES


def test_real_keyset_response_shape():
    assert set(KEYSET_PAGE) == {"markets", "next_cursor"} and isinstance(KEYSET_PAGE["next_cursor"], str)


# --------------------------- paging ---------------------------


def test_follows_the_cursor_until_there_is_none(monkeypatch):
    sent = serve(
        monkeypatch,
        [
            {"markets": [raw("1"), raw("2")], "next_cursor": "abc"},
            {"markets": [raw("3")], "next_cursor": "def"},
            {"markets": [raw("4")]},
        ],
    )
    assert [m.market_id for m in fetch_open_markets(FLOORS)] == ["1", "2", "3", "4"]
    assert [p.get("after_cursor") for p in sent] == [None, "abc", "def"]


def test_floors_are_sent_to_the_server(monkeypatch):
    sent = serve(monkeypatch, [{"markets": []}])
    fetch_open_markets(FLOORS)
    assert sent[0] == {
        "limit": 100,
        "closed": "false",
        "include_tag": "true",
        "liquidity_num_min": 10_000,
        "volume_num_min": 50_000,
    }


def test_zero_floors_ask_for_every_open_market(monkeypatch):
    sent = serve(monkeypatch, [{"markets": []}])
    fetch_open_markets(Settings(min_liquidity=0, min_volume=0))
    assert "liquidity_num_min" not in sent[0] and "volume_num_min" not in sent[0]


def test_keeps_what_was_listed_when_a_later_page_fails(monkeypatch):
    serve(monkeypatch, [{"markets": [raw("1")], "next_cursor": "abc"}, FetchError("HTTP 500")])
    assert [m.market_id for m in fetch_open_markets(FLOORS)] == ["1"]


def test_first_page_failing_is_an_error(monkeypatch):
    serve(monkeypatch, [FetchError("HTTP 500")])
    with pytest.raises(FetchError):
        fetch_open_markets(FLOORS)


def test_stops_at_the_safety_limit_if_the_cursor_never_ends(monkeypatch):
    monkeypatch.setattr(discovery, "MAX_PAGES", 3)
    sent = serve(monkeypatch, [{"markets": [raw(str(i))], "next_cursor": "again"} for i in range(10)])
    assert len(fetch_open_markets(FLOORS)) == 3 and len(sent) == 3


# --------------------------- the scheduled job ---------------------------


def job_runs() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM job_runs ORDER BY id")]


def test_job_fills_the_catalog_and_records_the_run(monkeypatch):
    serve(monkeypatch, [{"markets": [raw("1"), raw("2", acceptingOrders=False)]}])
    assert discovery.main() == 0
    assert [m.market_id for m in catalog.open_markets()] == ["1", "2"]
    (run,) = job_runs()
    assert run["job"] == "discover" and run["status"] == "success"
    assert run["summary"] == "listed 2 open markets (2 new), 1 selected for streaming (max 300)"


def test_job_records_a_failed_download(monkeypatch):
    serve(monkeypatch, [FetchError("HTTP 500 after 5 tries")])
    assert discovery.main() == 1
    (run,) = job_runs()
    assert run["status"] == "failed" and "HTTP 500" in run["error"]


def test_job_publishes_the_selection_for_the_streamer(monkeypatch):
    from polybot import rawstore
    from polybot.streamer import load_selection

    serve(monkeypatch, [{"markets": [raw("1"), raw("2", acceptingOrders=False), raw("3", volumeNum=9e6)]}])
    assert discovery.main() == 0
    selection = load_selection()
    assert [m.market_id for m in selection] == ["3", "1"]  # highest volume first; "2" cannot be streamed
    assert (selection[0].yes_token, selection[0].no_token, selection[0].category) == ("31", "32", "politics")
    assert rawstore.read_json(rawstore.selection_path())["updated_at"]


# --------------------------- fee rates and the fuller selection file ---------------------------


def test_fee_rate_comes_from_the_markets_own_schedule():
    assert parse_market(raw("1", feesEnabled=True, feeSchedule={"rate": 0.04, "exponent": 1})).fee_rate == 0.04
    assert parse_market(raw("2", feesEnabled=False)).fee_rate == 0.0  # fees switched off for this market
    assert parse_market(raw("3")).fee_rate is None  # the API did not say
    assert parse_market(raw("4", feesEnabled=True, feeSchedule={"rate": "bad"})).fee_rate is None


def test_selection_file_carries_what_the_paper_job_needs(monkeypatch):
    from polybot import rawstore

    serve(monkeypatch, [{"markets": [raw("1", feesEnabled=True, feeSchedule={"rate": 0.07}), raw("2")]}])
    assert discovery.main() == 0
    first, second = rawstore.read_json(rawstore.selection_path())["markets"]
    assert (first["market_id"], first["fee_rate"], first["end_date"]) == ("1", 0.07, "2027-01-01T00:00:00Z")
    assert (first["volume"], first["liquidity"]) == (123456.5, 20000.25) and second["fee_rate"] is None
