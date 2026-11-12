"""The warehouse: data in Parquet files, DuckDB to ask questions of it.

DuckDB is a database engine that runs inside Python (like SQLite) but is built
for analysis: it reads whole columns at a time, so "average spread per market
per minute over a million rows" takes a moment instead of minutes.

There is no DuckDB database file here on purpose. The data lives in the
Parquet and CSV files (see ``polybot.rawstore`` for the layout), and each time
we connect we tell DuckDB where they are by creating *views*: saved queries
that look like tables. That way the streamer, the jobs and a person running a
query never fight over one database file.

Five tables are available:

* ``book_top``   top of each order book over time (live, from the websocket)
* ``trades``     every trade (live, from the websocket)
* ``snapshots``  the 10-minute and hourly price snapshots (copied from SQLite)
* ``markets``    one row per market: question, category, end date, outcome
* ``bars_1m``    one-minute bars built from ``book_top`` and ``trades``

Each of the first two is today's CSV plus the Parquet of finished days, joined
into one table, so a query always sees data up to the last couple of seconds.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from polybot import rawstore
from polybot.recorder import BOOK_COLUMNS, TRADE_COLUMNS

# Column types per table. Timestamps travel through CSV as text and become real
# timestamps (UTC) on the way into Parquet; see ``_TS``.
SCHEMAS: dict[str, dict[str, str]] = {
    "book_top": {name: "VARCHAR" if name in ("ts", "market_id") else "DOUBLE" for name in BOOK_COLUMNS},
    "trades": dict(
        zip(
            TRADE_COLUMNS,
            ("VARCHAR", "VARCHAR", "TINYINT", "DOUBLE", "DOUBLE", "VARCHAR", "DOUBLE", "VARCHAR"),
            strict=True,
        )
    ),
    "snapshots": {
        "ts": "VARCHAR",
        "market_id": "VARCHAR",
        "yes_price": "DOUBLE",
        "no_price": "DOUBLE",
        "best_bid": "DOUBLE",
        "best_ask": "DOUBLE",
        "volume": "DOUBLE",
        "liquidity": "DOUBLE",
        "snapshot_id": "BIGINT",
    },
    "markets": {
        "market_id": "VARCHAR",
        "question": "VARCHAR",
        "slug": "VARCHAR",
        "category": "VARCHAR",
        "end_date": "VARCHAR",
        "outcomes": "VARCHAR",
        "yes_token": "VARCHAR",
        "no_token": "VARCHAR",
        "volume": "DOUBLE",
        "volume_24h": "DOUBLE",
        "liquidity": "DOUBLE",
        "first_seen": "VARCHAR",
        "last_seen": "VARCHAR",
        "outcome": "VARCHAR",
        "resolved_at": "VARCHAR",
        "in_catalog": "BOOLEAN",
    },
}
TIME_COLUMNS = {"ts", "end_date", "first_seen", "last_seen", "resolved_at"}
BAR_COLUMNS = {
    "market_id": "VARCHAR",
    "minute": "TIMESTAMP",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "avg_spread": "DOUBLE",
    "volume": "DOUBLE",
    "dollar_volume": "DOUBLE",
    "trades": "BIGINT",
    "book_updates": "BIGINT",
}


def sql_path(path: Path) -> str:
    """A path as a quoted SQL string. DuckDB wants forward slashes, even on Windows."""
    return "'" + str(path).replace("\\", "/").replace("'", "''") + "'"


def _time(column: str) -> str:
    """SQL that turns our ISO text (ending in ``Z`` or ``+00:00``) into a UTC timestamp; unparseable text is NULL."""
    return f"TRY_CAST(replace(replace({column}, 'Z', ''), '+00:00', '') AS TIMESTAMP)"


def csv_select(table: str, files: list[Path]) -> str:
    """A SELECT that reads CSV files for ``table`` with the right types (and a ``date`` column for day tables)."""
    schema = SCHEMAS[table]
    columns = ", ".join(f"'{name}': '{kind}'" for name, kind in schema.items())
    fields = [f"{_time(name)} AS {name}" if name in TIME_COLUMNS else name for name in schema]
    if "ts" in schema:
        fields.append(f"CAST({_time('ts')} AS DATE) AS date")
    sources = "[" + ", ".join(sql_path(f) for f in files) + "]"
    return (
        f"SELECT {', '.join(fields)} FROM read_csv({sources}, header = true, nullstr = '', "
        f"ignore_errors = true, columns = {{{columns}}})"
    )


def _empty_select(table: str) -> str:
    """A SELECT with the right columns and no rows, for a table that has no files yet."""
    if table == "bars_1m":
        fields = [f"CAST(NULL AS {kind}) AS {name}" for name, kind in BAR_COLUMNS.items()]
    else:
        fields = [
            f"CAST(NULL AS {'TIMESTAMP' if name in TIME_COLUMNS else kind}) AS {name}"
            for name, kind in SCHEMAS[table].items()
        ]
    if table != "markets":
        fields.append("CAST(NULL AS DATE) AS date")
    return f"SELECT {', '.join(fields)} WHERE false"


def _parquet_select(folder: Path) -> str:
    """Every Parquet file in the ``date=...`` folders under ``folder``; the folder name becomes a ``date`` column."""
    files = sql_path(folder / "date=*" / "*.parquet")
    return f"SELECT * FROM read_parquet({files}, hive_partitioning = true, union_by_name = true)"


def table_sql(table: str) -> str:
    """The query behind one view, built from whichever files exist right now."""
    data = rawstore.data_dir()
    if table == "markets":
        file = rawstore.markets_file()
        return f"SELECT * FROM read_parquet({sql_path(file)})" if file.exists() else _empty_select(table)
    if table == "bars_1m":
        folder = data / "bars" / "bars_1m"
        has_bars = folder.exists() and any(folder.glob("date=*/*.parquet"))
        return _parquet_select(folder) if has_bars else _empty_select(table)
    packed = rawstore.parquet_days(table)
    # A day that is already packed is read from Parquet only, so nothing is counted twice
    # in the moment between packing a day and deleting its CSV.
    loose = [rawstore.raw_file(table, day) for day in rawstore.csv_days(table) if day not in packed]
    parts = []
    if packed:
        parts.append(_parquet_select(data / "raw" / table))
    if loose:
        parts.append(csv_select(table, loose))
    return " UNION ALL BY NAME ".join(parts) if parts else _empty_select(table)


VIEWS = ("book_top", "trades", "snapshots", "markets", "bars_1m")
READ_ONLY_STARTS = ("select", "with", "describe", "show", "summarize")


def connect() -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB with one view per table. Cheap to open; close it when done."""
    con = duckdb.connect()
    for table in VIEWS:
        con.execute(f"CREATE VIEW {table} AS {table_sql(table)}")
    return con


def write_parquet(con: duckdb.DuckDBPyConnection, select_sql: str, target: Path) -> int:
    """Run a SELECT and save the result as one Parquet file. Returns the number of rows.

    The file is written under a temporary name and then renamed into place, so a
    reader never opens a half-written file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    con.execute(f"COPY ({select_sql}) TO {sql_path(tmp)} (FORMAT parquet, COMPRESSION zstd)")
    rows = con.execute(f"SELECT COUNT(*) FROM read_parquet({sql_path(tmp)})").fetchone()[0]
    os.replace(tmp, target)
    return int(rows)


def format_rows(columns: list[str], rows: list[tuple], limit: int = 50) -> str:
    """A plain-text table (ASCII only, so it prints on any console)."""
    shown = [[("" if v is None else str(v)) for v in row] for row in rows[:limit]]
    widths = [max(len(c), *(len(r[i]) for r in shown)) if shown else len(c) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)), "  ".join("-" * w for w in widths)]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in shown]
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows not shown")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """``polybot query "SELECT ..."``: run one read-only question against the warehouse and print the answer."""
    if not argv:
        print('Usage: polybot query "SELECT market_id, count(*) FROM trades GROUP BY 1 ORDER BY 2 DESC LIMIT 10"')
        print(f"Tables: {', '.join(VIEWS)}")
        return 2
    sql = " ".join(argv).strip()
    if sql.split(None, 1)[0].lower() not in READ_ONLY_STARTS:
        print(f"Only questions are allowed here (statements starting with {', '.join(READ_ONLY_STARTS)}).")
        return 2
    con = connect()
    try:
        result = con.execute(sql)
        print(format_rows([d[0] for d in result.description], result.fetchall()))
    except duckdb.Error as exc:
        print(f"Query failed: {exc}")
        return 1
    finally:
        con.close()
    return 0
