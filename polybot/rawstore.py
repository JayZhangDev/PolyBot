"""Files for live data: one CSV per table per day, plus two small JSON files.

Layout, under ``data/`` (never committed to git)::

    data/raw/book_top/2026-11-12.csv     top of the order book over time
    data/raw/trades/2026-11-12.csv       every trade
    data/stream_selection.json           which markets to stream (written by discovery)
    data/stream_status.json              what the streamer is doing right now

Live data is only ever *added*, so plain files that grow at the end are the
simplest safe store: no locking against the hourly jobs, and a crash can lose
at most the last couple of seconds. Days are in UTC. These files are the raw
record; a later step packs them into a compact, query-friendly format.
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from polybot.db import PROJECT_ROOT

TABLES = ("book_top", "trades")


def data_dir() -> Path:
    """``data/`` in the project, or the folder named by ``POLYBOT_DATA_DIR`` (tests use this)."""
    return Path(os.environ.get("POLYBOT_DATA_DIR", PROJECT_ROOT / "data"))


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


def day_bytes(day: str) -> dict[str, int]:
    """Size on disk of each table's file for a day: ``{"book_top": 1234, "trades": 56}``."""
    return {table: raw_file(table, day).stat().st_size if raw_file(table, day).exists() else 0 for table in TABLES}


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
