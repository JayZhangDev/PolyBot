"""The market catalog table and the choice of which markets to stream."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from polybot import catalog, cli
from polybot.catalog import MarketMeta, save_catalog, select_stream_markets
from polybot.config import Settings
from polybot.db import connect

NOW = datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)
OPEN = Settings(min_liquidity=0, min_volume=0)  # no floors, so each test states its own


def meta(mid: str, **changes) -> MarketMeta:
    base = MarketMeta(
        market_id=mid,
        condition_id=f"0xcond{mid}",
        question=f"Will market {mid} happen?",
        slug=f"market-{mid}",
        category="politics",
        tags=("politics",),
        end_date="2027-01-01T00:00:00Z",
        outcomes=("Yes", "No"),
        token_ids=(f"{mid}01", f"{mid}02"),
        volume=100_000.0,
        volume_24h=1_000.0,
        liquidity=20_000.0,
        accepting_orders=True,
    )
    return replace(base, **changes)


def ids(markets: list[MarketMeta]) -> list[str]:
    return [m.market_id for m in markets]


def test_save_then_read_back_gives_the_same_market():
    original = meta("1", tags=("politics", "trump"), outcomes=("Spirit", "FUT"))
    assert save_catalog([original], NOW) == {"listed": 1, "new": 1, "total": 1}
    assert catalog.open_markets(NOW) == [original]


def test_refresh_updates_numbers_and_keeps_first_seen():
    save_catalog([meta("1", volume=100.0)], NOW)
    counts = save_catalog([meta("1", volume=250.0), meta("2")], NOW + timedelta(minutes=15))
    assert counts == {"listed": 2, "new": 1, "total": 2}
    with connect() as c:
        row = c.execute("SELECT volume, first_seen, last_seen FROM market_catalog WHERE market_id = '1'").fetchone()
    assert row["volume"] == 250.0
    assert row["first_seen"] == NOW.isoformat() and row["last_seen"] == (NOW + timedelta(minutes=15)).isoformat()


def test_market_that_stopped_being_listed_drops_out_after_three_cycles():
    save_catalog([meta("gone")], NOW)
    save_catalog([meta("still")], NOW + timedelta(minutes=30))
    assert ids(catalog.open_markets(NOW + timedelta(minutes=30))) == ["gone", "still"]  # one missed refresh is fine
    assert ids(catalog.open_markets(NOW + timedelta(minutes=50))) == ["still"]


def test_liquidity_and_volume_floors():
    save_catalog([meta("thin", liquidity=500.0), meta("quiet", volume=900.0), meta("ok")], NOW)
    assert ids(select_stream_markets(Settings(min_liquidity=1_000, min_volume=1_000), NOW)) == ["ok"]


def test_include_and_skip_categories():
    save_catalog([meta("p"), meta("s", category="sports"), meta("c", category="crypto")], NOW)
    assert ids(select_stream_markets(replace(OPEN, include_categories=("sports", "crypto")), NOW)) == ["s", "c"]
    assert ids(select_stream_markets(replace(OPEN, skip_categories=("sports",)), NOW)) == ["p", "c"]


def test_only_two_token_markets_that_accept_orders_can_be_streamed():
    save_catalog([meta("paused", accepting_orders=False), meta("no-tokens", token_ids=()), meta("ok")], NOW)
    assert ids(select_stream_markets(OPEN, NOW)) == ["ok"]


def test_maximum_keeps_the_highest_volume_first():
    save_catalog([meta(str(v), volume=float(v)) for v in (10, 50, 30, 40, 20)], NOW)
    assert ids(select_stream_markets(replace(OPEN, max_stream_markets=3), NOW)) == ["50", "40", "30"]


def test_rank_by_chooses_which_number_decides_the_top():
    save_catalog([meta("old", volume=9e6, volume_24h=10.0), meta("hot", volume=1e5, volume_24h=5e4)], NOW)
    assert ids(select_stream_markets(replace(OPEN, max_stream_markets=1), NOW)) == ["old"]
    assert ids(select_stream_markets(replace(OPEN, max_stream_markets=1, rank_by="volume_24h"), NOW)) == ["hot"]


def test_description_shows_counts_categories_and_top_markets():
    save_catalog([meta("1", volume=5e6), meta("2", category="sports"), meta("3", accepting_orders=False)], NOW)
    text = catalog.describe_selection(replace(OPEN, skip_categories=("sports",)), NOW + timedelta(minutes=4))
    assert "3 open markets" in text and "refreshed 4 min ago" in text
    assert "Selected : 1 to stream (max 300, top by volume)" in text
    assert "Skipping : sports" in text and "Will market 1 happen?" in text


def test_markets_command_runs_on_an_empty_catalog(capsys):
    assert cli.main(["markets"]) == 0
    assert "never refreshed" in capsys.readouterr().out
