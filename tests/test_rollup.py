"""The daily rollup: packing days, copying SQLite snapshots, the markets table and one-minute bars."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import make_market
from test_warehouse import ask, quote, trade

from polybot import rawstore, rollup, warehouse
from polybot.catalog import MarketMeta, save_catalog
from polybot.db import connect, db_path
from polybot.recorder import BOOK_COLUMNS, TRADE_COLUMNS
from polybot.resolutions import record_resolutions
from polybot.storage import save_snapshot

DAY = "2026-11-10"
TODAY = datetime.now(timezone.utc).date().isoformat()


@pytest.fixture(autouse=True)
def no_category_lookups(monkeypatch):
    """The rollup looks up missing categories over the network; in tests that lookup finds nothing."""
    monkeypatch.setattr(rollup.resolutions, "fetch_markets_by_id", lambda ids: [])


def make_old(path) -> None:
    """Pretend a file was last written an hour ago, so it counts as settled."""
    an_hour_ago = time.time() - 3600
    os.utime(path, (an_hour_ago, an_hour_ago))


def pack(table: str, day: str) -> dict:
    con = warehouse.connect()
    try:
        return rollup.pack_day(con, table, day)
    finally:
        con.close()


def run(step):
    con = warehouse.connect()
    try:
        return step(con)
    finally:
        con.close()


# --------------------------- step 1: packing ---------------------------


def test_pack_writes_parquet_keeps_every_row_and_removes_the_csv():
    rawstore.append_rows(
        "trades", TRADE_COLUMNS, [trade(f"{DAY}T10:00:02.000Z", "m2"), trade(f"{DAY}T10:00:01.000Z", "m1")]
    )
    result = pack("trades", DAY)
    assert result["rows"] == 2 and result["parquet_bytes"] > 0
    assert (rawstore.partition_dir("trades", DAY) / "data.parquet").exists()
    assert not rawstore.raw_file("trades", DAY).exists()
    assert ask("SELECT market_id FROM trades") == [("m1",), ("m2",)]  # stored in time order


def test_late_rows_for_a_packed_day_are_merged_in():
    rawstore.append_rows("trades", TRADE_COLUMNS, [trade(f"{DAY}T10:00:00.000Z", "m1")])
    pack("trades", DAY)
    rawstore.append_rows("trades", TRADE_COLUMNS, [trade(f"{DAY}T23:59:59.000Z", "m2")])
    assert pack("trades", DAY)["rows"] == 2
    assert ask("SELECT COUNT(*), COUNT(DISTINCT market_id) FROM trades") == [(2, 2)]
    assert not rawstore.raw_file("trades", DAY).exists()


def test_csv_with_a_damaged_line_is_packed_but_not_deleted(caplog):
    rawstore.append_rows("trades", TRADE_COLUMNS, [trade(f"{DAY}T10:00:00.000Z", "m1")])
    with rawstore.raw_file("trades", DAY).open("a", encoding="utf-8") as f:
        f.write(f"{DAY}T10:00:01.000Z,m1,0,not-a-number,1,BUY,0,0x\n")
    with caplog.at_level("WARNING"):
        assert pack("trades", DAY)["rows"] == 1
    assert rawstore.raw_file("trades", DAY).exists() and "keeping the CSV" in caplog.text


def test_only_finished_and_settled_days_are_packed():
    for day in (DAY, TODAY):
        rawstore.append_rows("book_top", BOOK_COLUMNS, [quote(f"{day}T10:00:00.000Z", "m1")])
    assert run(rollup.pack_finished_days) == []  # yesterday's file was written a moment ago
    make_old(rawstore.raw_file("book_top", DAY))
    make_old(rawstore.raw_file("book_top", TODAY))
    assert [(p["table"], p["day"]) for p in run(rollup.pack_finished_days)] == [("book_top", DAY)]
    assert rawstore.raw_file("book_top", TODAY).exists()  # today is still being written to


# --------------------------- step 2: snapshots from SQLite ---------------------------


def test_snapshots_are_copied_once_and_only_new_rows_after_that():
    save_snapshot([make_market("1", yes=0.30, bid=0.29, ask=0.31), make_market("2", yes=0.70)])
    first = run(rollup.copy_snapshots)
    assert (first["rows"], first["days"], first["last_snapshot_id"]) == (2, 1, 2)
    assert run(rollup.copy_snapshots)["rows"] == 0  # nothing new, nothing copied
    save_snapshot([make_market("1", yes=0.35)])
    assert run(rollup.copy_snapshots)["rows"] == 1
    assert ask("SELECT snapshot_id, market_id, yes_price, best_bid FROM snapshots ORDER BY snapshot_id") == [
        (1, "1", 0.30, 0.29),
        (2, "2", 0.70, None),
        (3, "1", 0.35, None),
    ]
    ((ts, day),) = ask("SELECT ts, date FROM snapshots WHERE snapshot_id = 1")
    assert (
        day == date.fromisoformat(TODAY)
        and abs((datetime.now(timezone.utc).replace(tzinfo=None) - ts).total_seconds()) < 60
    )


def test_snapshots_land_in_the_folder_for_their_own_day():
    save_snapshot([make_market("1"), make_market("2")])
    with connect() as c:
        c.execute("UPDATE snapshots SET captured_at = '2026-10-30T01:36:41.055532+00:00' WHERE market_id = '1'")
    assert run(rollup.copy_snapshots)["days"] == 2
    assert rawstore.parquet_days("snapshots") == ["2026-10-30", TODAY]
    assert ask("SELECT ts FROM snapshots WHERE market_id = '1'") == [(datetime(2026, 10, 30, 1, 36, 41, 55532),)]


def test_files_from_a_copy_that_crashed_are_removed_before_copying_again():
    save_snapshot([make_market("1")])
    run(rollup.copy_snapshots)
    save_snapshot([make_market("2")])
    run(rollup.copy_snapshots)
    # Pretend the second copy crashed after writing its file but before recording that it finished.
    rawstore.write_json_atomic(rollup._state_path(), {"last_snapshot_id": 1})
    assert run(rollup.copy_snapshots)["rows"] == 1
    assert ask("SELECT COUNT(*), COUNT(DISTINCT snapshot_id) FROM snapshots") == [(2, 2)]  # no row twice


def test_copying_never_changes_the_sqlite_file():
    save_snapshot([make_market("1"), make_market("2")])
    before = hashlib.sha256(db_path().read_bytes()).hexdigest()
    run(rollup.copy_snapshots)
    run(rollup.copy_markets)
    assert hashlib.sha256(db_path().read_bytes()).hexdigest() == before


def test_copy_does_nothing_on_a_database_with_no_snapshot_table():
    with connect() as c:
        c.execute("CREATE TABLE unrelated (x)")
    assert run(rollup.copy_snapshots) == {"rows": 0, "days": 0, "last_snapshot_id": 0}
    assert run(rollup.copy_markets) == 0


# --------------------------- step 3: markets ---------------------------


def test_markets_table_merges_catalog_history_and_outcomes():
    save_snapshot([make_market("1", question="Old market?", closed=True, yes=1.0), make_market("2", question="Both?")])
    record_resolutions([make_market("1", closed=True, yes=1.0)])
    save_catalog(
        [
            MarketMeta(
                "2",
                "0xc2",
                "Both?",
                "both",
                "crypto",
                ("crypto",),
                "2027-01-01T00:00:00Z",
                ("Yes", "No"),
                ("21", "22"),
                500.0,
                50.0,
                80.0,
                True,
            ),
            MarketMeta(
                "3",
                "0xc3",
                "Catalog only?",
                "cat",
                "sports",
                ("sports",),
                None,
                ("Yes", "No"),
                ("31", "32"),
                900.0,
                90.0,
                70.0,
                True,
            ),
        ]
    )
    assert run(rollup.copy_markets) == 3
    rows = ask(
        "SELECT market_id, question, category, yes_token, no_token, outcome, in_catalog FROM markets ORDER BY market_id"
    )
    assert rows == [
        ("1", "Old market?", None, None, None, "YES", False),
        ("2", "Both?", "crypto", "21", "22", None, True),
        ("3", "Catalog only?", "sports", "31", "32", None, True),
    ]
    ((end, resolved),) = ask("SELECT end_date, resolved_at FROM markets WHERE market_id = '2'")
    assert end == datetime(2027, 1, 1) and resolved is None
    assert ask("SELECT resolved_at IS NOT NULL, first_seen IS NOT NULL FROM markets WHERE market_id = '1'") == [
        (True, True)
    ]


# --------------------------- step 4: bars ---------------------------


def bar_data() -> None:
    rawstore.append_rows(
        "book_top",
        BOOK_COLUMNS,
        [
            quote(f"{DAY}T10:00:05.000Z", "m1", 0.40, 0.44),  # midpoint 0.42
            quote(f"{DAY}T10:00:30.000Z", "m1", 0.42, 0.44),  # midpoint 0.43
            quote(f"{DAY}T10:00:50.000Z", "m1", 0.38, 0.42),  # midpoint 0.40
            quote(f"{DAY}T10:02:10.000Z", "m1", 0.44, 0.46),  # midpoint 0.45
            quote(f"{DAY}T10:00:00.000Z", "m2", "", 0.01),  # nobody bidding: no midpoint
        ],
    )
    rawstore.append_rows(
        "trades",
        TRADE_COLUMNS,
        [
            trade(f"{DAY}T10:00:10.000Z", "m1", price=0.43, size=10.0),
            trade(f"{DAY}T10:00:40.000Z", "m1", price=0.58, size=5.0, outcome=1),
            trade(f"{DAY}T10:01:20.000Z", "m1", price=0.41, size=2.0),
        ],
    )


def test_bars_have_open_high_low_close_spread_and_volume():
    bar_data()
    assert rollup.build_bars(DAY) == 3
    bars = ask(
        "SELECT strftime(minute, '%H:%M'), open, high, low, close, avg_spread, volume, dollar_volume, trades, "
        "book_updates FROM bars_1m ORDER BY minute"
    )
    assert bars[0] == ("10:00", 0.42, 0.43, 0.40, 0.40, pytest.approx(0.033333), 15.0, pytest.approx(7.2), 2, 3)
    assert bars[2] == ("10:02", 0.45, 0.45, 0.45, 0.45, 0.02, 0.0, 0.0, 0, 1)
    assert ask("SELECT DISTINCT market_id, date FROM bars_1m") == [
        ("m1", date(2026, 11, 10))
    ]  # m2 never had a midpoint


def test_minute_with_trades_but_no_book_change_carries_the_previous_close():
    bar_data()
    rollup.build_bars(DAY)
    assert ask(
        "SELECT open, high, low, close, avg_spread, volume, book_updates FROM bars_1m "
        "WHERE strftime(minute, '%H:%M') = '10:01'"
    ) == [(0.40, 0.40, 0.40, 0.40, None, 2.0, 0)]


def test_bars_are_rebuilt_only_when_the_days_data_changed():
    bar_data()
    assert rollup.build_stale_bars() == {DAY: 3}
    assert rollup.build_stale_bars() == {}  # nothing changed since
    rawstore.append_rows("trades", TRADE_COLUMNS, [trade(f"{DAY}T10:05:00.000Z", "m1")])
    later = time.time() + 5
    os.utime(rawstore.raw_file("trades", DAY), (later, later))
    assert rollup.build_stale_bars() == {DAY: 4}


def test_bars_read_the_same_from_csv_and_from_parquet():
    bar_data()
    rollup.build_bars(DAY)
    from_csv = ask("SELECT * FROM bars_1m ORDER BY minute")
    pack("book_top", DAY)
    pack("trades", DAY)
    rollup.build_bars(DAY)
    assert ask("SELECT * FROM bars_1m ORDER BY minute") == from_csv


# --------------------------- the whole job ---------------------------


def test_rollup_job_runs_every_step_and_records_the_run():
    bar_data()
    for table in rawstore.TABLES:
        make_old(rawstore.raw_file(table, DAY))
    save_snapshot([make_market("1"), make_market("2")])
    assert rollup.main() == 0
    with connect() as c:
        run_row = dict(c.execute("SELECT job, status, summary FROM job_runs").fetchone())
    assert run_row == {
        "job": "rollup",
        "status": "success",
        "summary": "packed 2 day files, copied 2 snapshots, 2 markets, built 3 bars for 1 days, "
        "calibration report rebuilt",
    }
    assert rawstore.csv_days("book_top") == [] and rawstore.parquet_days("book_top") == [DAY]
    assert not any((rawstore.data_dir() / "tmp").iterdir())  # no temp files left behind
    assert rollup.main() == 0  # a second run finds nothing to do and is fine
    assert ask("SELECT COUNT(*) FROM snapshots") == [(2,)]


def test_migrate_reports_counts_from_both_sides(capsys):
    save_snapshot([make_market("1"), make_market("2")])
    record_resolutions([make_market("1", closed=True, yes=0.0)])
    assert rollup.migrate() == 0
    out = capsys.readouterr().out
    assert "SQLite snapshots    : 2 rows" in out and "Warehouse snapshots : 2 rows" in out
    assert "Warehouse markets   : 2 markets, 1 with an outcome" in out
    assert "Copy complete       : yes" in out and "opened read-only" in out


def test_migrate_without_a_database_says_so(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("POLYBOT_DB", str(tmp_path / "missing.db"))
    assert rollup.migrate() == 1
    assert "nothing to migrate" in capsys.readouterr().out


def test_yesterday_is_before_today_helper():
    assert (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat() < rollup._today()


# --------------------------- categories and the report step ---------------------------


def test_markets_without_a_category_get_one_looked_up(monkeypatch):
    save_snapshot([make_market("1"), make_market("2"), make_market("3")])
    asked = []

    def lookup(ids):
        asked.extend(ids)
        return [{"id": "1", "tags": [{"slug": "nba"}]}, {"id": "2", "tags": []}]  # "3" is unknown to the API

    monkeypatch.setattr(rollup.resolutions, "fetch_markets_by_id", lookup)
    assert rollup.resolutions.backfill_categories() == {"missing": 3, "filled": 1}
    assert asked == ["1", "2", "3"]
    run(rollup.copy_markets)
    assert ask("SELECT market_id, category FROM markets ORDER BY market_id") == [
        ("1", "sports"),
        ("2", None),
        ("3", None),
    ]


def test_category_lookup_failing_does_not_fail_the_rollup(monkeypatch, caplog):
    from polybot.http import FetchError

    save_snapshot([make_market("1")])
    monkeypatch.setattr(
        rollup.resolutions, "fetch_markets_by_id", lambda ids: (_ for _ in ()).throw(FetchError("HTTP 500"))
    )
    with caplog.at_level("WARNING"):
        assert rollup.main() == 0
    assert "Could not look up missing categories" in caplog.text


def test_rollup_rebuilds_the_calibration_report():
    from polybot import tracker

    save_snapshot([make_market("1")])
    assert rollup.main() == 0
    assert (tracker.reports_dir() / "calibration.md").exists() and (tracker.reports_dir() / "calibration.json").exists()
