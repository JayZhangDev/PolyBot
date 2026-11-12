"""The day files and the small JSON files the streamer uses."""

from __future__ import annotations

import csv

from polybot import rawstore

COLUMNS = ("ts", "market_id", "price")


def read(table: str, day: str) -> list[list[str]]:
    with rawstore.raw_file(table, day).open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_first_batch_writes_a_header_and_later_batches_do_not():
    assert rawstore.append_rows("trades", COLUMNS, [("2026-11-12T00:00:01.000Z", "m1", 0.5)]) == 1
    rawstore.append_rows(
        "trades", COLUMNS, [("2026-11-12T00:00:03.000Z", "m2", 0.6), ("2026-11-12T00:00:04.000Z", "m1", 0.7)]
    )
    assert read("trades", "2026-11-12") == [
        ["ts", "market_id", "price"],
        ["2026-11-12T00:00:01.000Z", "m1", "0.5"],
        ["2026-11-12T00:00:03.000Z", "m2", "0.6"],
        ["2026-11-12T00:00:04.000Z", "m1", "0.7"],
    ]


def test_rows_go_to_the_file_for_their_own_utc_day():
    rawstore.append_rows(
        "trades", COLUMNS, [("2026-11-12T23:59:59.900Z", "m1", 0.5), ("2026-11-13T00:00:00.100Z", "m1", 0.6)]
    )
    assert len(read("trades", "2026-11-12")) == 2 and len(read("trades", "2026-11-13")) == 2
    assert rawstore.count_rows("trades", "2026-11-12") == 1 and rawstore.count_rows("trades", "2026-11-14") == 0


def test_tables_are_kept_in_separate_folders_and_sizes_are_reported():
    rawstore.append_rows("book_top", COLUMNS, [("2026-11-12T00:00:01.000Z", "m1", 0.5)])
    sizes = rawstore.day_bytes("2026-11-12")
    assert sizes["book_top"] > 0 and sizes["trades"] == 0
    assert rawstore.raw_file("book_top", "2026-11-12").parent.name == "book_top"


def test_question_with_a_comma_survives_the_round_trip():
    rawstore.append_rows("trades", COLUMNS, [("2026-11-12T00:00:01.000Z", 'Will "A, B" win?', 0.5)])
    assert read("trades", "2026-11-12")[1][1] == 'Will "A, B" win?'


def test_json_is_written_whole_and_read_back():
    path = rawstore.status_path()
    rawstore.write_json_atomic(path, {"state": "connected", "n": 3})
    assert rawstore.read_json(path) == {"state": "connected", "n": 3}
    assert [p.name for p in path.parent.iterdir()] == ["stream_status.json"]  # no temp file left behind


def test_missing_or_broken_json_reads_as_none():
    assert rawstore.read_json(rawstore.selection_path()) is None
    rawstore.selection_path().parent.mkdir(parents=True, exist_ok=True)
    rawstore.selection_path().write_text("{half a file", encoding="utf-8")
    assert rawstore.read_json(rawstore.selection_path()) is None
    rawstore.selection_path().write_text("[1, 2]", encoding="utf-8")
    assert rawstore.read_json(rawstore.selection_path()) is None
