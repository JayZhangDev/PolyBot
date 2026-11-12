"""The warehouse views: CSV for today, Parquet for finished days, one table to query."""

from __future__ import annotations

from datetime import date, datetime

from polybot import cli, rawstore, rollup, warehouse
from polybot.recorder import BOOK_COLUMNS, TRADE_COLUMNS


def quote(ts: str, market: str, bid: float | str = 0.40, ask: float | str = 0.44) -> tuple:
    """One book_top row; only the first seven columns matter to these tests."""
    spread = round(ask - bid, 6) if isinstance(bid, float) and isinstance(ask, float) else ""
    return (ts, market, bid, ask, spread, 100.0, 50.0, *[""] * (len(BOOK_COLUMNS) - 7))


def trade(ts: str, market: str, price: float = 0.42, size: float = 10.0, outcome: int = 0) -> tuple:
    return (ts, market, outcome, price, size, "BUY", 0, "0xabc")


def ask(sql: str) -> list[tuple]:
    con = warehouse.connect()
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_every_table_can_be_queried_before_any_data_exists():
    for table in warehouse.VIEWS:
        assert ask(f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert [r[0] for r in ask("DESCRIBE trades")] == [*TRADE_COLUMNS, "date"]
    assert "minute" in [r[0] for r in ask("DESCRIBE bars_1m")]


def test_todays_csv_is_visible_with_real_types():
    rawstore.append_rows("book_top", BOOK_COLUMNS, [quote("2026-11-12T02:45:22.853Z", "m1")])
    ((ts, market, bid, spread, day),) = ask("SELECT ts, market_id, best_bid, spread, date FROM book_top")
    assert ts == datetime(2026, 11, 12, 2, 45, 22, 853000) and day == date(2026, 11, 12)
    assert (market, bid, spread) == ("m1", 0.40, 0.04)


def test_blank_cells_are_null_not_zero():
    rawstore.append_rows("book_top", BOOK_COLUMNS, [quote("2026-11-12T02:45:22.853Z", "m1", bid="")])
    assert ask("SELECT best_bid, best_ask, bid_px_1 FROM book_top") == [(None, 0.44, None)]


def test_packed_days_and_loose_days_form_one_table():
    rawstore.append_rows(
        "trades", TRADE_COLUMNS, [trade("2026-11-10T10:00:00.000Z", "m1"), trade("2026-11-10T11:00:00.000Z", "m2")]
    )
    con = warehouse.connect()
    rollup.pack_day(con, "trades", "2026-11-10")
    con.close()
    rawstore.append_rows("trades", TRADE_COLUMNS, [trade("2026-11-12T09:00:00.000Z", "m1")])
    assert ask("SELECT date, COUNT(*) FROM trades GROUP BY date ORDER BY date") == [
        (date(2026, 11, 10), 2),
        (date(2026, 11, 12), 1),
    ]
    assert ask("SELECT SUM(size), MAX(outcome) FROM trades WHERE market_id = 'm1'") == [(20.0, 0)]


def test_day_that_is_both_packed_and_loose_is_not_counted_twice():
    rows = [trade("2026-11-10T10:00:00.000Z", "m1")]
    rawstore.append_rows("trades", TRADE_COLUMNS, rows)
    con = warehouse.connect()
    rollup.pack_day(con, "trades", "2026-11-10")
    con.close()
    rawstore.append_rows("trades", TRADE_COLUMNS, rows)  # the same day's CSV shows up again
    assert ask("SELECT COUNT(*) FROM trades") == [(1,)]


def test_cut_off_last_line_is_skipped_instead_of_breaking_the_table():
    rawstore.append_rows("trades", TRADE_COLUMNS, [trade("2026-11-12T09:00:00.000Z", "m1")])
    with rawstore.raw_file("trades", "2026-11-12").open("a", encoding="utf-8") as f:
        f.write("2026-11-12T09:00:01.000Z,m1,0,not-a-number")  # the streamer died mid-write
    assert ask("SELECT COUNT(*) FROM trades") == [(1,)]


def test_format_rows_is_a_plain_aligned_table():
    text = warehouse.format_rows(["market_id", "n"], [("m1", 3), ("long-market", None)], limit=1)
    assert text.splitlines() == ["market_id  n", "---------  -", "m1         3", "... 1 more rows not shown"]


def test_query_command_answers_questions_and_refuses_anything_else(capsys):
    rawstore.append_rows("trades", TRADE_COLUMNS, [trade("2026-11-12T09:00:00.000Z", "m1", size=7.5)])
    assert cli.main(["query", "SELECT market_id, size FROM trades"]) == 0
    assert "m1         7.5" in capsys.readouterr().out
    assert cli.main(["query", "DROP VIEW trades"]) == 2
    assert "Only questions are allowed" in capsys.readouterr().out
    assert cli.main(["query", "SELECT nope FROM trades"]) == 1
    assert "Query failed" in capsys.readouterr().out
    assert cli.main(["query"]) == 2
