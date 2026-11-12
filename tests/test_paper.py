"""The PAPER job end to end, with recorded books and markets handed in: no network, no real files."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from polybot import cli, ledger, paper, rawstore, statusfile
from polybot.config import Settings
from polybot.fills import Book
from polybot.tracker import Model

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
LIVE = {
    "state": "connected",
    "updated_at": NOW.isoformat(),
    "markets_streamed": 2,
    "trades_today": 10,
    "reconnects_today": 0,
}
SETTINGS = Settings(paper_min_edge=0.03, paper_stake_fraction=0.02, paper_max_positions=20)
BOOK = Book(bids=((0.40, 500.0), (0.39, 500.0)), asks=((0.42, 500.0), (0.43, 500.0)))


def market(market_id: str, days: float = 30.0, category: str = "geopolitics", fee_rate: float | None = None) -> dict:
    return {
        "market_id": market_id,
        "question": f"Will {market_id} happen?",
        "category": category,
        "end_date": (NOW + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "volume": 1e6,
        "liquidity": 5e4,
        "fee_rate": fee_rate,
    }


def says(probability: float | None) -> Model:
    return Model("fixed", "always the same", lambda view: probability)


def cycle(markets, books, model, *, stream=None, settings=SETTINGS, now=NOW, lookup=lambda ids: []):
    stream = {**LIVE, "updated_at": now.isoformat()} if stream is None else stream  # connected right now
    return paper.run_cycle(settings, now, books=books, markets=markets, lookup=lookup, stream=stream, model=model)


def positions() -> list[dict]:
    c = ledger.open_ledger(1_000.0)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM positions ORDER BY id")]
    finally:
        c.close()


def test_buys_at_the_recorded_ask_with_two_percent_of_the_account():
    s = cycle([market("1")], {"1": (BOOK, None)}, says(0.50))
    (p,) = positions()
    assert (p["side"], p["entry_price"], p["cost"]) == ("YES", 0.42, pytest.approx(20.0))
    assert p["shares"] == pytest.approx(20 / 0.42) and p["model"] == "fixed" and p["model_probability"] == 0.50
    assert s["opened"] == 1 and s["cash"] == pytest.approx(980.0)
    assert s["equity"] == pytest.approx(980 + (20 / 0.42) * 0.40)  # valued at the bid, so it starts a little down


def test_fee_uses_the_markets_own_rate_and_falls_back_to_its_category():
    cycle(
        [market("own", category="crypto", fee_rate=0.03), market("cat", category="crypto")],
        {"own": (BOOK, None), "cat": (BOOK, None)},
        says(0.60),
    )
    c = ledger.open_ledger(1_000.0)
    fees = {r["market_id"]: r["fees"] / r["shares"] for r in c.execute("SELECT * FROM fills")}
    c.close()
    assert fees["own"] == pytest.approx(0.03 * 0.42 * 0.58, rel=1e-3)
    assert fees["cat"] == pytest.approx(0.07 * 0.42 * 0.58, rel=1e-3)  # crypto's rate from the table


def test_never_fills_more_than_the_book_held():
    thin = Book(bids=((0.40, 500.0),), asks=((0.42, 10.0),))  # only 10 shares on offer
    cycle([market("1")], {"1": (thin, None)}, says(0.90))
    (p,) = positions()
    assert p["shares"] == pytest.approx(10.0) and p["cost"] == pytest.approx(4.2)


def test_order_smaller_than_the_minimum_is_not_placed():
    tiny = Book(bids=((0.40, 500.0),), asks=((0.42, 3.0),))  # fewer than 5 shares available
    assert cycle([market("1")], {"1": (tiny, None)}, says(0.90))["opened"] == 0


def test_skips_markets_that_close_too_late_or_have_no_date_or_no_book():
    markets = [market("far", days=200), {**market("undated"), "end_date": None}, market("bookless"), market("ok")]
    s = cycle(markets, {"far": (BOOK, None), "undated": (BOOK, None), "ok": (BOOK, None)}, says(0.50))
    assert s["opened"] == 1 and positions()[0]["market_id"] == "ok"


def test_best_edges_first_up_to_the_position_limit():
    cheap = Book(bids=((0.20, 500.0),), asks=((0.22, 500.0),))
    books = {"a": (BOOK, None), "b": (cheap, None), "c": (BOOK, None)}
    cycle(
        [market("a"), market("b"), market("c")],
        books,
        says(0.50),
        settings=Settings(paper_min_edge=0.03, paper_max_positions=2),
    )
    assert [p["market_id"] for p in positions()] == ["b", "a"]  # b has the biggest edge


def test_one_position_per_market_however_many_cycles_run():
    for _ in range(3):
        cycle([market("1")], {"1": (BOOK, None)}, says(0.50))
    assert len(positions()) == 1


def test_no_trading_when_the_stream_is_not_connected():
    stale = {**LIVE, "updated_at": (NOW - timedelta(minutes=10)).isoformat()}
    s = cycle([market("1")], {"1": (BOOK, None)}, says(0.50), stream=stale)
    assert s["opened"] == 0 and s["live"] is False and positions() == []
    status = json.loads(statusfile.resolve_path(SETTINGS.status_path).read_text(encoding="utf-8"))
    assert status["status"] == "idle" and status["mode"] == "paper"  # the file is still written


def test_sells_into_the_bid_when_the_model_changes_its_mind():
    cycle([market("1")], {"1": (BOOK, None)}, says(0.50))
    # The model now says 40%: holding YES is worth exactly what the bid pays, and NO is not cheap enough to buy.
    s = cycle([market("1")], {"1": (BOOK, None)}, says(0.40), now=NOW + timedelta(minutes=5))
    (p,) = positions()
    assert (s["sold"], s["opened"], p["status"]) == (1, 0, "CLOSED")
    assert p["pnl"] == pytest.approx((20 / 0.42) * 0.40 - 20.0)  # bought at the ask, sold at the bid
    assert s["win_rate"] == 0 and s["pnl_total"] == pytest.approx(p["pnl"])


def test_switches_sides_when_the_model_swings_far_enough():
    cycle([market("1")], {"1": (BOOK, None)}, says(0.50))
    s = cycle([market("1")], {"1": (BOOK, None)}, says(0.35), now=NOW + timedelta(minutes=5))
    first, second = positions()
    assert (s["sold"], s["opened"]) == (1, 1)
    assert (first["side"], first["status"]) == ("YES", "CLOSED") and (second["side"], second["status"]) == (
        "NO",
        "OPEN",
    )
    assert second["entry_price"] == pytest.approx(0.60)  # NO is bought at 1 minus the YES bid


def test_resolved_market_pays_out_and_frees_the_position():
    cycle([market("1")], {"1": (BOOK, None)}, says(0.50))
    resolved = [{"id": "1", "closed": True, "outcomePrices": '["1", "0"]'}]
    s = cycle([], {}, says(0.50), now=NOW + timedelta(days=1), lookup=lambda ids: resolved)
    (p,) = positions()
    assert s["settled"] == 1 and p["status"] == "CLOSED" and p["pnl"] == pytest.approx(20 / 0.42 - 20.0)
    assert s["win_rate"] == 1.0 and s["cash"] == pytest.approx(980 + 20 / 0.42)


def test_losing_side_gets_nothing_and_a_draw_pays_half():
    cycle([market("1"), market("2")], {"1": (BOOK, None), "2": (BOOK, None)}, says(0.50))
    done = [
        {"id": "1", "closed": True, "outcomePrices": '["0", "1"]'},
        {"id": "2", "closed": True, "outcomePrices": '["0.5", "0.5"]'},
    ]
    cycle([], {}, says(0.50), now=NOW + timedelta(days=1), lookup=lambda ids: done)
    lost, drawn = positions()
    assert lost["pnl"] == pytest.approx(-20.0) and drawn["pnl"] == pytest.approx(0.5 * 20 / 0.42 - 20.0)


def test_open_market_and_failed_lookup_leave_positions_alone(caplog):
    from polybot.http import FetchError

    cycle([market("1")], {"1": (BOOK, None)}, says(0.50))
    still_open = [{"id": "1", "closed": False, "outcomePrices": '["0.42", "0.58"]'}]
    assert cycle([market("1")], {"1": (BOOK, None)}, says(0.50), lookup=lambda ids: still_open)["settled"] == 0

    def down(ids):
        raise FetchError("HTTP 500")

    with caplog.at_level("WARNING"):
        assert cycle([market("1")], {"1": (BOOK, None)}, says(0.50), lookup=down)["settled"] == 0
    assert "Could not check held markets" in caplog.text and positions()[0]["status"] == "OPEN"


def test_model_that_passes_or_misbehaves_opens_nothing():
    for model in (says(None), says(1.5), Model("broken", "", lambda view: 1 / 0)):
        assert cycle([market("1")], {"1": (BOOK, None)}, model)["opened"] == 0


def test_model_is_given_the_live_view():
    seen = {}

    def predict(view: dict) -> float:
        seen.update(view)
        return 0.5

    cycle([market("1", days=10, category="crypto")], {"1": (BOOK, 0.35)}, Model("spy", "", predict))
    assert seen["price"] == pytest.approx(0.41) and seen["change_24h"] == pytest.approx(0.06)
    assert seen["days_to_end"] == pytest.approx(10.0) and seen["category"] == "crypto" and "outcome" not in seen


def test_books_are_read_from_the_recorded_files():
    from polybot.book import BookTop
    from polybot.recorder import BOOK_COLUMNS, book_row

    older = BookTop(bids=((0.30, 100.0),), asks=((0.34, 100.0),))
    newer = BookTop(bids=((0.40, 500.0), (0.39, 200.0)), asks=((0.42, 300.0),))
    ms = int(NOW.timestamp() * 1000)
    rawstore.append_rows(
        "book_top", BOOK_COLUMNS, [book_row(ms - 26 * 3_600_000, "1", older), book_row(ms - 60_000, "1", newer)]
    )
    book, mid_before = paper.load_books(NOW)["1"]
    assert book.bids == ((0.40, 500.0), (0.39, 200.0)) and book.asks == ((0.42, 300.0),)
    assert mid_before == pytest.approx(0.32)  # the midpoint a day ago


def test_unknown_model_name_is_a_clear_error():
    with pytest.raises(LookupError, match="no model named 'nope'"):
        paper.find_model("nope")


def test_scheduled_run_is_recorded_and_a_failure_does_not_hang(monkeypatch):
    monkeypatch.setattr(
        paper,
        "run_cycle",
        lambda settings: {
            "equity": 1000.0,
            "pnl_total": 0.0,
            "open_positions": 0,
            "opened": 0,
            "sold": 0,
            "settled": 0,
            "live": False,
        },
    )
    assert paper.main() == 0
    monkeypatch.setattr(paper, "run_cycle", lambda settings: 1 / 0)
    assert paper.main() == 1
    c = ledger.open_ledger(1_000.0)
    runs = [(r["status"], r["summary"], r["error"]) for r in c.execute("SELECT * FROM runs ORDER BY id")]
    c.close()
    assert runs[0][0] == "success" and "stream not connected" in runs[0][1]
    assert runs[1][0] == "failed" and runs[1][2].startswith("ZeroDivisionError")


def test_account_command_says_paper_and_lists_positions(capsys):
    cycle([market("1")], {"1": (BOOK, None)}, says(0.50))
    assert cli.main(["account"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PAPER account (fake money") and "1 open, 0 closed" in out and "Will 1 happen?" in out
