"""Where the data files live, and the plain-file operations on them (no DuckDB needed here).

Layout, under ``data/`` (never committed to git)::

    data/raw/book_top/2026-11-12.csv                  today's live rows, still growing
    data/raw/book_top/date=2026-11-11/data.parquet    a finished day, packed
    data/raw/trades/...                               the same two forms
    data/raw/price_gaps/...                           Polymarket and Kalshi prices side by side, the same two forms
    data/raw/snapshots/date=2026-11-11/part-*.parquet the 10-minute and hourly price snapshots
    data/raw/markets/markets.parquet                  one row per market
    data/bars/bars_1m/date=2026-11-11/data.parquet    one-minute bars
    data/stream_selection.json                        which markets to stream (written by discovery)
    data/stream_status.json                           what the streamer is doing right now

Live data is only ever *added*, so the streamer appends to a plain CSV: no
locking against the other jobs, and a crash can lose at most the last couple of
seconds. Once a day is over, the rollup job packs its CSV into Parquet, a
column-by-column compressed format that is several times smaller and much
faster to query. Folders named ``date=...`` are how DuckDB recognises the day
without opening the file. Days are in UTC.
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from polybot.db import PROJECT_ROOT

TABLES = ("book_top", "trades")  # written live by the streamer, as CSV
CSV_TABLES = (*TABLES, "price_gaps")  # every table written as daily CSV and packed into Parquet by the rollup
DAY_TABLES = ("book_top", "trades", "snapshots", "price_gaps")  # everything stored one folder per day
USAGE_TABLES = (*DAY_TABLES, "bars")  # what the disk-usage report lists


def data_dir() -> Path:
    """``data/`` in the project, or the folder named by ``POLYBOT_DATA_DIR`` (tests use this)."""
    return Path(os.environ.get("POLYBOT_DATA_DIR", PROJECT_ROOT / "data"))


def reports_dir() -> Path:
    """``reports/`` in the project, or the folder named by ``POLYBOT_REPORTS_DIR``."""
    return Path(os.environ.get("POLYBOT_REPORTS_DIR", PROJECT_ROOT / "reports"))


def selection_path() -> Path:
    return data_dir() / "stream_selection.json"


def status_path() -> Path:
    return data_dir() / "stream_status.json"


def raw_file(table: str, day: str) -> Path:
    """The CSV for one table on one UTC day, e.g. ``data/raw/trades/2026-11-12.csv``."""
    return data_dir() / "raw" / table / f"{day}.csv"


def append_rows(table: str, columns: Sequence[str], rows: Sequence[Sequence[object]]) -> int:
    """Add rows to the day files for ``table``. Returns how many rows were written.

    The first value of every row must be its timestamp (``2026-11-12T02:38:43.376Z``);
    its first ten characters decide which day's file the row belongs in. A new
    file starts with a header line naming the columns.
    """
    by_day: dict[str, list[Sequence[object]]] = defaultdict(list)
    for row in rows:
        by_day[str(row[0])[:10]].append(row)
    for day, day_rows in by_day.items():
        path = raw_file(table, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(columns)
            writer.writerows(day_rows)
    return len(rows)


def count_rows(table: str, day: str) -> int:
    """Rows already stored for a table on a day (the header line is not counted)."""
    path = raw_file(table, day)
    if not path.exists():
        return 0
    with path.open("rb") as f:
        return max(0, sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b"")) - 1)


def partition_dir(table: str, day: str) -> Path:
    """The folder holding a table's Parquet files for one day, e.g. ``data/raw/trades/date=2026-11-12``."""
    return data_dir() / "raw" / table / f"date={day}"


def bars_dir(day: str) -> Path:
    """The folder holding the one-minute bars for one day."""
    return data_dir() / "bars" / "bars_1m" / f"date={day}"


def markets_file() -> Path:
    return data_dir() / "raw" / "markets" / "markets.parquet"


def csv_days(table: str) -> list[str]:
    """Days that have a CSV file for ``table``, oldest first."""
    folder = data_dir() / "raw" / table
    return sorted(p.stem for p in folder.glob("????-??-??.csv")) if folder.exists() else []


def parquet_days(table: str) -> list[str]:
    """Days that have at least one Parquet file for ``table``, oldest first."""
    folder = data_dir() / "raw" / table
    if not folder.exists():
        return []
    return sorted(p.name[len("date=") :] for p in folder.glob("date=*") if any(p.glob("*.parquet")))


def _folder_bytes(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.glob("*.parquet")) if folder.exists() else 0


def table_day_bytes(table: str, day: str) -> int:
    """Bytes on disk for one table on one day: its CSV (if any) plus its Parquet files."""
    if table == "bars":
        return _folder_bytes(bars_dir(day))
    csv_file = raw_file(table, day)
    return (csv_file.stat().st_size if csv_file.exists() else 0) + _folder_bytes(partition_dir(table, day))


def day_bytes(day: str) -> dict[str, int]:
    """Size on disk of each table for a day: ``{"book_top": 1234, "trades": 56, ...}``."""
    return {table: table_day_bytes(table, day) for table in USAGE_TABLES}


def usage_days() -> list[str]:
    """Every day that has any data file, oldest first."""
    days: set[str] = set()
    for table in DAY_TABLES:
        days.update(csv_days(table))
        days.update(parquet_days(table))
    bars_root = data_dir() / "bars" / "bars_1m"
    if bars_root.exists():
        days.update(p.name[len("date=") :] for p in bars_root.glob("date=*"))
    return sorted(days)


def write_json_atomic(path: Path, payload: object) -> None:
    """Write JSON so a reader never sees half a file: write a temp file, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    """The JSON object in ``path``, or None if the file is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
