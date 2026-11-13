"""The Kalshi comparison job, with a fake catalog and fake APIs on both sides (no network)."""

from __future__ import annotations

import csv
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from polybot import cli, crossmarket, health, rawstore, rollup, warehouse
from polybot.catalog import MarketMeta, save_catalog
from polybot.config import ConfigError, Settings, parse_settings
from polybot.crossmarket import Quote, gap_row, kalshi_quote, polymarket_quote
from polybot.http import FetchError
from polybot.kalshi import KalshiMarket
from polybot.kalshi_match import Pair

NOW = datetime(2026, 11, 13, 4, 4, tzinfo=timezone.utc)


def meta(mid: str, question: str, end: str = "2027-05-09T00:00:00Z", outcomes=("Yes", "No")) -> MarketMeta:
    return MarketMeta(
        market_id=mid,
        condition_id=f"0x{mid}",
        question=question,
        slug=None,
        category="politics",
        tags=("politics",),
        end_date=end,
        outcomes=tuple(outcomes),
        token_ids=(f"{mid}1", f"{mid}2"),
        volume=100_000.0,
        volume_24h=1_000.0,
        liquidity=20_000.0,
        accepting_orders=True,
    )


def km(ticker, title, outcome="", bid=0.40, ask=0.42, last=0.41, status="active", close="2027-05-01T00:00:00Z"):
    return KalshiMarket(ticker, ticker.rsplit("-", 1)[0], title, outcome, close, bid, ask, last, 50.0, status)


CATALOG = [
    meta("100", "Will Marine Le Pen win the 2027 French presidential election?"),
    meta("200", "Anthropic IPO before 2027?", end="2026-12-31T00:00:00Z"),
    meta("300", "Lakers vs. Celtics", outcomes=("Lakers", "Celtics")),  # not a yes/no market: never paired
]
LISTING = [
    km("KXFRENCHPRES-27-MLEP", "Will Marine Le Pen win the 2027 French presidential election?"),
    km("KXIPO-26-ANTHROPIC", "Who will IPO before 2027?", "Anthropic", close="2027-01-01T00:00:00Z"),
    km("KXSNOW-26", "Will it snow in Denver on Christmas?"),
]


def gamma(mid: str, bid: float | None, ask: float | None, last: float | None = None, **extra) -> dict:
    return {"id": mid, "bestBid": bid, "bestAsk": ask, "lastTradePrice": last, "closed": False, **extra}


class Fakes:
    """Both APIs. Le Pen: Polymarket 0.33/0.35 against Kalshi 0.30/0.32, so the books overlap by a cent."""

    def __init__(self) -> None:
        self.listings = 0
        self.kalshi = {
            "KXFRENCHPRES-27-MLEP": km("KXFRENCHPRES-27-MLEP", "", bid=0.30, ask=0.32),
            "KXIPO-26-ANTHROPIC": km("KXIPO-26-ANTHROPIC", "", bid=0.10, ask=0.12),
            "KXSNOW-26": km("KXSNOW-26", "", bid=0.70, ask=0.72),
        }
        self.polymarket = {"100": gamma("100", 0.33, 0.35), "200": gamma("200", 0.10, 0.11)}

    def list_kalshi(self):
        self.listings += 1
        return iter(LISTING)

    def fetch_kalshi(self, tickers):
        return {t: self.kalshi[t] for t in tickers if t in self.kalshi}

    def fetch_polymarket(self, ids):
        return [self.polymarket[i] for i in ids if i in self.polymarket]


def run(fakes: Fakes, now: datetime = NOW, settings: Settings | None = None, list_kalshi=None) -> str:
    return crossmarket.collect(
        settings or Settings(),
        now,
        list_kalshi=list_kalshi or fakes.list_kalshi,
        fetch_kalshi=fakes.fetch_kalshi,
        fetch_polymarket=fakes.fetch_polymarket,
    )


def rows_today(day: str = "2026-11-13") -> list[dict]:
    with rawstore.raw_file("price_gaps", day).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def fakes() -> Fakes:
    save_catalog(CATALOG, NOW)
    return Fakes()


# ----------------------------- a run -----------------------------
def test_first_run_pairs_the_questions_and_records_both_prices(fakes):
    summary = run(fakes)
    assert summary.startswith("2 pairs, 2 priced (0 not trading on one side)")
    assert "1 crossed by a point or more before fees" in summary
    assert "paired again: 2 pairs among 3 Kalshi markets" in summary

    rows = {r["kalshi_ticker"]: r for r in rows_today()}
    assert set(rows) == {"KXFRENCHPRES-27-MLEP", "KXIPO-26-ANTHROPIC"}
    le_pen = rows["KXFRENCHPRES-27-MLEP"]
    assert le_pen["ts"] == "2026-11-13T04:04:00+00:00" and le_pen["polymarket_id"] == "100"
    assert (float(le_pen["pm_mid"]), float(le_pen["k_mid"])) == (0.34, 0.31)
    assert float(le_pen["gap"]) == pytest.approx(0.03) and float(le_pen["crossing"]) == pytest.approx(0.01)

    report = crossmarket.report_path().read_text(encoding="utf-8")
    assert "Marine Le Pen" in report and "(KXIPO-26-ANTHROPIC)" in report


def test_the_pairing_is_reused_until_it_is_a_day_old(fakes):
    run(fakes)
    run(fakes, NOW + timedelta(minutes=10))
    assert fakes.listings == 1
    save_catalog(CATALOG, NOW + timedelta(hours=25))  # the discover job keeps the catalog fresh meanwhile
    run(fakes, NOW + timedelta(hours=25))
    assert fakes.listings == 2
    assert len(rows_today()) == 4 and len(rows_today("2026-11-14")) == 2


def test_changing_the_minimum_score_pairs_again(fakes):
    run(fakes)
    run(fakes, NOW + timedelta(minutes=10), Settings(kalshi_min_score=0.9))
    assert fakes.listings == 2


def test_hand_corrections_remove_and_add_pairs(fakes):
    crossmarket.overrides_path().write_text(
        "remove:\n  - {polymarket: '100', kalshi: KXFRENCHPRES-27-MLEP}\n"
        "add:\n  - {polymarket: '200', kalshi: KXSNOW-26, flip: true}\n",
        encoding="utf-8",
    )
    run(fakes)
    rows = rows_today()
    assert [(r["polymarket_id"], r["kalshi_ticker"]) for r in rows] == [("200", "KXSNOW-26")]  # replaced the IPO pair
    assert (float(rows[0]["k_bid"]), float(rows[0]["k_ask"])) == (0.28, 0.3)  # Kalshi's NO side: 1 - 0.72, 1 - 0.70
    assert "| added | Anthropic IPO before 2027? (200) |" in crossmarket.report_path().read_text(encoding="utf-8")


def test_a_broken_corrections_file_is_ignored_with_a_note(fakes):
    crossmarket.overrides_path().write_text("add:\n  - {oops: 1}\n", encoding="utf-8")
    summary = run(fakes)
    assert "kalshi_pairs.yaml has a mistake" in summary and len(rows_today()) == 2


def test_markets_that_stopped_trading_are_skipped(fakes):
    fakes.polymarket["200"]["closed"] = True
    fakes.kalshi["KXFRENCHPRES-27-MLEP"] = replace(fakes.kalshi["KXFRENCHPRES-27-MLEP"], status="finalized")
    assert run(fakes).startswith("2 pairs, 0 priced (2 not trading on one side)")


def test_a_failed_kalshi_listing_keeps_the_last_pairing(fakes):
    run(fakes)

    def failing_listing():
        raise FetchError("Kalshi is down")
        yield  # never reached; makes this a generator, like the real listing

    save_catalog(CATALOG, NOW + timedelta(hours=25))
    summary = run(fakes, NOW + timedelta(hours=25), list_kalshi=failing_listing)
    assert "could not list Kalshi markets, kept the last pairing" in summary
    assert summary.startswith("2 pairs, 2 priced")


def test_nothing_is_paired_before_the_catalog_exists():
    assert run(Fakes()) == "pairing waits for the market catalog (run the discover job)"


def test_the_job_can_be_switched_off(fakes):
    assert "switched off" in run(fakes, settings=Settings(kalshi_enabled=False))
    assert fakes.listings == 0


# ----------------------------- prices -----------------------------
def test_a_flipped_kalshi_market_is_read_from_its_no_side():
    quote = kalshi_quote(km("K", "", bid=0.30, ask=0.40, last=0.35), flip=True)
    assert (quote.bid, quote.ask, quote.last) == pytest.approx((0.60, 0.70, 0.65))
    assert kalshi_quote(km("K", "", status="finalized")) is None


def test_polymarket_quotes():
    assert polymarket_quote(gamma("1", 0.4, 0.5, closed=True)) is None
    assert polymarket_quote(gamma("1", 0.4, 0.5, acceptingOrders=False)) is None
    empty = polymarket_quote(gamma("1", 0, 1, last=0.45))
    assert (empty.bid, empty.ask, empty.mid) == (None, None, 0.45)  # the midpoint falls back to the last trade


def test_no_crossing_without_a_full_book_on_both_sides():
    row = gap_row("t", Pair("1", "K", 1.0, "", ""), Quote(0.40, None, 0.41), Quote(0.30, 0.32, None))
    assert row[-2] == pytest.approx(0.1) and row[-1] is None  # gap from 0.41 (last) and 0.31; no crossing


# ----------------------------- the report, the warehouse and health -----------------------------
def test_gap_report_summarises_the_last_day(fakes):
    for minutes in (0, 10, 20):
        run(fakes, NOW + timedelta(minutes=minutes))
    text = crossmarket.gap_report(NOW + timedelta(minutes=25))
    assert "Rows recorded     : 6 across 2 pairs" in text
    assert "Books crossed     : 3 observations in 1 pairs by a point or more (3 by any amount)" in text
    widest = text.splitlines()[-2]  # the Le Pen pair has the wider gap, 3 points
    assert "+3.0" in widest and "Marine Le Pen" in widest


def test_gap_report_with_nothing_recorded():
    assert "No prices recorded yet" in crossmarket.gap_report(NOW)
    assert cli.main(["gaps"]) == 0


def test_the_rollup_packs_old_price_gap_days_into_parquet():
    rawstore.append_rows("price_gaps", crossmarket.GAP_COLUMNS, [["2020-01-01T00:00:00+00:00", "1", "K", *[0.5] * 8]])
    con = warehouse.connect()
    try:
        packed = rollup.pack_finished_days(con, now=time.time() + 10_000)
    finally:
        con.close()
    assert [(p["table"], p["rows"]) for p in packed] == [("price_gaps", 1)]
    con = warehouse.connect()
    try:
        assert con.execute("SELECT count(*), max(gap) FROM price_gaps").fetchone() == (1, 0.5)
    finally:
        con.close()


def test_health_shows_the_comparison(fakes):
    assert health.kalshi_report(NOW) is None
    run(fakes)
    report = health.kalshi_report(NOW + timedelta(minutes=3))
    assert (report["pairs"], report["rows_today"], report["kalshi_markets"]) == (2, 2, 3)
    assert "2 matched" in "\n".join(health._kalshi_lines(report))


# ----------------------------- settings and corrections file -----------------------------
def test_kalshi_settings():
    s = parse_settings({"kalshi": {"enabled": False, "min_match_score": 0.7, "refresh_hours": 12}})
    assert (s.kalshi_enabled, s.kalshi_min_score, s.kalshi_refresh_hours) == (False, 0.7, 12.0)
    with pytest.raises(ConfigError, match="true or false"):
        parse_settings({"kalshi": {"enabled": "no"}})
    with pytest.raises(ConfigError, match="min_match_score"):
        parse_settings({"kalshi": {"min_match_score": 0.1}})


def test_corrections_file_mistakes_are_explained(tmp_path):
    path = tmp_path / "pairs.yaml"
    assert crossmarket.load_overrides(path) == crossmarket.Overrides()  # no file, no corrections
    path.write_text("keep: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="only contain"):
        crossmarket.load_overrides(path)
    path.write_text("add:\n  - {polymarket: '1', kalshi: K, flip: 'yes'}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="true or false"):
        crossmarket.load_overrides(path)


def test_the_shipped_corrections_file_is_valid():
    real = crossmarket.PROJECT_ROOT / "kalshi_pairs.yaml"
    overrides = crossmarket.load_overrides(real)
    assert overrides.add == () and ("2364952", "KXPUTINZELENSKYYLOCATION-28-SWZ") in overrides.remove
