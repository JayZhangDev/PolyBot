"""The daily rollup: pack finished days, copy new snapshots out of SQLite, and build one-minute bars.

Five steps, each safe to run again at any time:

1. **Pack.** A day that is over has its CSV files rewritten as Parquet. The
   row counts are compared, and only if they match is the CSV deleted.
2. **Copy snapshots.** Price snapshots the pipeline and agent wrote to SQLite
   since the last run are copied into the warehouse. SQLite is opened
   read-only and is never changed. On the first run this copies the whole
   history, which is the migration.
3. **Copy markets.** One fresh ``markets`` file: every market PolyBot knows,
   with its category and (once resolved) its outcome.
4. **Bars.** For each day whose data changed, one row per market per minute:
   open, high, low and close of the midpoint, average spread, and volume.
5. **Calibration report.** ``reports/calibration.md`` and its charts are rebuilt
   from the refreshed data (see ``polybot.tracker``).

A *bar* squeezes everything that happened in one minute into a handful of
numbers, which is why analysis over months of data stays fast. The *midpoint*
is halfway between the best bid and best ask, a fair single price for a market.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from polybot import rawstore, resolutions, tracker, warehouse
from polybot.db import db_path
from polybot.http import FetchError
from polybot.jobs import run_job

SETTLE_SECONDS = 600  # a day's CSV must be untouched this long before it is packed
BATCH_ROWS = 100_000  # snapshot rows read from SQLite at a time
LOCK_WAIT_SECONDS = 900

log = logging.getLogger("polybot.rollup")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _state_path() -> Path:
    return rawstore.data_dir() / "warehouse_state.json"


def _tmp_dir() -> Path:
    path = rawstore.data_dir() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Step 1: pack finished days
# ---------------------------------------------------------------------------
def pack_day(con: duckdb.DuckDBPyConnection, table: str, day: str) -> dict:
    """Rewrite one day's CSV as Parquet and delete the CSV once the row counts agree.

    If the day was already packed and more rows turned up in a new CSV, the two
    are merged, so running this twice never loses or doubles anything.
    """
    csv_file = rawstore.raw_file(table, day)
    target = rawstore.partition_dir(table, day) / "data.parquet"
    csv_rows = rawstore.count_rows(table, day)
    csv_bytes = csv_file.stat().st_size
    old_rows = 0
    source = f"SELECT * EXCLUDE (date) FROM ({warehouse.csv_select(table, [csv_file])})"
    if target.exists():
        old_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet({warehouse.sql_path(target)})").fetchone()[0]
        source = f"SELECT * FROM read_parquet({warehouse.sql_path(target)}) UNION ALL BY NAME {source}"
    rows = warehouse.write_parquet(con, f"SELECT * FROM ({source}) ORDER BY ts", target)
    if rows == old_rows + csv_rows:
        csv_file.unlink()
    else:  # a damaged line, most likely. Keep the CSV so nothing is lost; the Parquet is what queries read.
        log.warning(
            "%s %s: the CSV has %d rows but %d reached Parquet; keeping the CSV for inspection.",
            table,
            day,
            csv_rows,
            rows - old_rows,
        )
    return {"table": table, "day": day, "rows": rows, "csv_bytes": csv_bytes, "parquet_bytes": target.stat().st_size}


def pack_finished_days(con: duckdb.DuckDBPyConnection, now: float | None = None) -> list[dict]:
    """Pack every CSV for a day that is over and has not been written to for a while."""
    now = now or time.time()
    packed = []
    for table in rawstore.CSV_TABLES:
        for day in rawstore.csv_days(table):
            settled = now - rawstore.raw_file(table, day).stat().st_mtime > SETTLE_SECONDS
            if day < _today() and settled:
                packed.append(pack_day(con, table, day))
    return packed


# ---------------------------------------------------------------------------
# Step 2: copy new snapshots out of SQLite
# ---------------------------------------------------------------------------
def _sqlite_readonly() -> sqlite3.Connection:
    """Open polybot.db so that it cannot be modified, whatever this code does."""
    conn = sqlite3.connect(f"file:{db_path().as_posix()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _remove_unfinished_parts(last_id: int) -> None:
    """Delete snapshot files from a copy that crashed before it was recorded as done.

    A part is named after the range of row IDs it holds. Anything starting
    after the last recorded ID was never counted, so it would be copied again;
    removing it first keeps every row in the warehouse exactly once.
    """
    for part in (rawstore.data_dir() / "raw" / "snapshots").glob("date=*/part-*.parquet"):
        if int(part.stem.split("-")[1]) > last_id:
            part.unlink()


def _dump_snapshots(c: sqlite3.Connection, last_id: int) -> tuple[dict[str, Path], int, int]:
    """Read snapshot rows newer than ``last_id`` in order and split them into one temp CSV per day.

    Reading in ID order walks the table from start to finish, which is fast even
    over the slow WSL bridge (jumping around by index is what takes minutes).
    Returns ``(csv per day, rows read, highest ID read)``.
    """
    columns = list(warehouse.SCHEMAS["snapshots"])
    files: dict[str, Path] = {}
    writers: dict = {}  # day -> the CSV writer for that day's temp file
    handles = []
    rows_read, high = 0, last_id
    cursor = c.execute(
        """
        SELECT captured_at, market_id, yes_price, no_price, best_bid, best_ask, volume, liquidity, id
        FROM snapshots WHERE id > ? ORDER BY id
    """,
        (last_id,),
    )
    try:
        while batch := cursor.fetchmany(BATCH_ROWS):
            for row in batch:
                day = str(row["captured_at"])[:10]
                if day not in writers:
                    files[day] = _tmp_dir() / f"snapshots-{day}.csv"
                    handle = files[day].open("w", newline="", encoding="utf-8")
                    handles.append(handle)
                    writers[day] = csv.writer(handle)
                    writers[day].writerow(columns)
                writers[day].writerow(tuple(row))
            rows_read += len(batch)
            high = batch[-1]["id"]
    finally:
        for handle in handles:
            handle.close()
    return files, rows_read, high


def copy_snapshots(con: duckdb.DuckDBPyConnection) -> dict:
    """Copy snapshot rows added to SQLite since the last run into the warehouse. Returns counts."""
    state = rawstore.read_json(_state_path()) or {}
    last_id = int(state.get("last_snapshot_id", 0))
    _remove_unfinished_parts(last_id)
    with closing(_sqlite_readonly()) as c:
        if not _has_table(c, "snapshots"):
            return {"rows": 0, "days": 0, "last_snapshot_id": last_id}
        files, rows_read, high = _dump_snapshots(c, last_id)
    written = 0
    for day, csv_file in sorted(files.items()):
        target = rawstore.partition_dir("snapshots", day) / f"part-{last_id + 1:010d}-{high:010d}.parquet"
        select = f"SELECT * EXCLUDE (date) FROM ({warehouse.csv_select('snapshots', [csv_file])}) ORDER BY ts"
        written += warehouse.write_parquet(con, select, target)
        csv_file.unlink()
    if written != rows_read:
        raise RuntimeError(f"read {rows_read} snapshot rows from SQLite but wrote {written}; not recording progress")
    if rows_read:  # only now is the copy "done"; a crash before this line is cleaned up next run
        rawstore.write_json_atomic(_state_path(), {**state, "last_snapshot_id": high})
    return {"rows": rows_read, "days": len(files), "last_snapshot_id": high}


# ---------------------------------------------------------------------------
# Step 3: the markets table
# ---------------------------------------------------------------------------
def _market_rows(c: sqlite3.Connection) -> dict[str, dict]:
    """One dict per market, merged from the catalog, the snapshotted markets and the recorded outcomes."""
    rows: dict[str, dict] = {}
    if _has_table(c, "markets"):
        for m in c.execute("SELECT id, question, slug, category, end_date, first_seen, last_seen FROM markets"):
            rows[m["id"]] = {
                "market_id": m["id"],
                "question": m["question"],
                "slug": m["slug"],
                "category": m["category"],
                "end_date": m["end_date"],
                "first_seen": m["first_seen"],
                "last_seen": m["last_seen"],
                "in_catalog": False,
            }
    if _has_table(c, "market_catalog"):
        for m in c.execute("SELECT * FROM market_catalog"):
            tokens = json.loads(m["token_ids"] or "[]")
            known = rows.get(m["market_id"], {})
            rows[m["market_id"]] = {
                **known,
                "market_id": m["market_id"],
                "question": m["question"],
                "slug": m["slug"],
                "category": m["category"],
                "end_date": m["end_date"],
                "outcomes": m["outcomes"],
                "yes_token": tokens[0] if len(tokens) == 2 else None,
                "no_token": tokens[1] if len(tokens) == 2 else None,
                "volume": m["volume"],
                "volume_24h": m["volume_24h"],
                "liquidity": m["liquidity"],
                # Keep the earliest time either table saw the market.
                "first_seen": min(filter(None, (known.get("first_seen"), m["first_seen"]))),
                "last_seen": max(filter(None, (known.get("last_seen"), m["last_seen"]))),
                "in_catalog": True,
            }
    if _has_table(c, "resolutions"):
        for r in c.execute("SELECT market_id, outcome, resolved_at FROM resolutions WHERE outcome IS NOT NULL"):
            if r["market_id"] in rows:
                rows[r["market_id"]].update(outcome=r["outcome"], resolved_at=r["resolved_at"])
    return rows


def copy_markets(con: duckdb.DuckDBPyConnection) -> int:
    """Rewrite the ``markets`` file from SQLite. Small, so it is replaced whole each time."""
    with closing(_sqlite_readonly()) as c:
        rows = _market_rows(c)
    if not rows:
        return 0
    columns = list(warehouse.SCHEMAS["markets"])
    tmp = _tmp_dir() / "markets.csv"
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows({name: row.get(name) for name in columns} for row in rows.values())
    count = warehouse.write_parquet(
        con, f"{warehouse.csv_select('markets', [tmp])} ORDER BY market_id", rawstore.markets_file()
    )
    tmp.unlink()
    return count


def fill_missing_categories() -> None:
    """Look up categories for markets that have none.

    A network problem here is logged, not fatal: the rollup is mostly local
    work and should not fail because the API is having a bad minute.
    """
    try:
        result = resolutions.backfill_categories()
    except FetchError as exc:
        log.warning("Could not look up missing categories this time: %s", exc)
        return
    if result["missing"]:
        log.info("%d markets had no category; filled in %d.", result["missing"], result["filled"])


# ---------------------------------------------------------------------------
# Step 4: one-minute bars
# ---------------------------------------------------------------------------
def bars_sql(day: str) -> str:
    """The query that turns one day of ``book_top`` and ``trades`` into one-minute bars.

    A minute with trades but no change at the top of the book has no prices of
    its own, so it is given the previous minute's close: the price did not move.
    """
    return f"""
    WITH quotes AS (
        SELECT market_id, date_trunc('minute', ts) AS minute, ts, (best_bid + best_ask) / 2 AS mid, spread
        FROM book_top
        WHERE date = DATE '{day}' AND best_bid IS NOT NULL AND best_ask IS NOT NULL
    ), price_bars AS (
        SELECT market_id, minute, arg_min(mid, ts) AS open, max(mid) AS high, min(mid) AS low,
               arg_max(mid, ts) AS close, avg(spread) AS avg_spread, count(*) AS book_updates
        FROM quotes GROUP BY market_id, minute
    ), trade_bars AS (
        SELECT market_id, date_trunc('minute', ts) AS minute, sum(size) AS volume,
               sum(price * size) AS dollar_volume, count(*) AS trades
        FROM trades WHERE date = DATE '{day}' GROUP BY market_id, minute
    ), joined AS (
        SELECT COALESCE(p.market_id, t.market_id) AS market_id, COALESCE(p.minute, t.minute) AS minute,
               p.open, p.high, p.low, p.close, p.avg_spread,
               COALESCE(t.volume, 0) AS volume, COALESCE(t.dollar_volume, 0) AS dollar_volume,
               COALESCE(t.trades, 0) AS trades, COALESCE(p.book_updates, 0) AS book_updates
        FROM price_bars p FULL OUTER JOIN trade_bars t ON p.market_id = t.market_id AND p.minute = t.minute
    ), filled AS (
        SELECT *, last_value(close IGNORE NULLS) OVER (
                   PARTITION BY market_id ORDER BY minute ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
               ) AS previous_close
        FROM joined
    )
    SELECT market_id, minute,
           round(COALESCE(open, previous_close), 6) AS open, round(COALESCE(high, previous_close), 6) AS high,
           round(COALESCE(low, previous_close), 6) AS low, round(COALESCE(close, previous_close), 6) AS close,
           round(avg_spread, 6) AS avg_spread, round(volume, 6) AS volume, round(dollar_volume, 6) AS dollar_volume,
           trades, book_updates
    FROM filled ORDER BY market_id, minute
    """


def _newest_source_time(day: str) -> float:
    """When the live data for a day last changed (0 if there is none)."""
    files = [rawstore.raw_file(t, day) for t in rawstore.TABLES]
    files += [p for t in rawstore.TABLES for p in rawstore.partition_dir(t, day).glob("*.parquet")]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def build_bars(day: str) -> int:
    """Build (or rebuild) the bars for one day. Returns the number of bars."""
    con = warehouse.connect()  # a fresh connection, so the views see files written earlier in this run
    try:
        return warehouse.write_parquet(con, bars_sql(day), rawstore.bars_dir(day) / "data.parquet")
    finally:
        con.close()


def build_stale_bars() -> dict[str, int]:
    """Build bars for every day whose live data is newer than its bars. Today is always rebuilt."""
    built = {}
    days = sorted({d for t in rawstore.TABLES for d in rawstore.csv_days(t) + rawstore.parquet_days(t)})
    for day in days:
        bars = rawstore.bars_dir(day) / "data.parquet"
        if not bars.exists() or bars.stat().st_mtime < _newest_source_time(day):
            built[day] = build_bars(day)
    return built


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------
def run_rollup() -> str:
    """All five steps. Returns the one-line summary recorded for the run."""
    con = duckdb.connect()
    try:
        packed = pack_finished_days(con)
        for p in packed:
            log.info(
                "Packed %s %s: %d rows, %.1f MB of CSV became %.1f MB of Parquet.",
                p["table"],
                p["day"],
                p["rows"],
                p["csv_bytes"] / 1e6,
                p["parquet_bytes"] / 1e6,
            )
        snapshots = copy_snapshots(con)
        log.info("Copied %d snapshot rows from SQLite across %d days.", snapshots["rows"], snapshots["days"])
        fill_missing_categories()
        markets = copy_markets(con)
    finally:
        con.close()
    bars = build_stale_bars()
    report = tracker.build_report()
    tracker.write_report(report)
    log.info("Calibration report rebuilt. %s", tracker.console_summary(report))
    for day, count in bars.items():
        log.info("Built %d one-minute bars for %s.", count, day)
    return (
        f"packed {len(packed)} day files, copied {snapshots['rows']} snapshots, {markets} markets, "
        f"built {sum(bars.values())} bars for {len(bars)} days, calibration report rebuilt"
    )


def main() -> int:
    """Entry point for the daily scheduled task. Returns the process exit code."""
    return run_job("rollup", run_rollup, wait_seconds=LOCK_WAIT_SECONDS)


def migrate() -> int:
    """``polybot migrate``: copy the existing SQLite data into the warehouse and prove the old file is untouched.

    This is the same copy the daily job does; run by hand it also prints a
    comparison of row counts on both sides.
    """
    if not db_path().exists():
        print(f"No database at {db_path()}; nothing to migrate.")
        return 1
    code = run_job("migrate", run_rollup, wait_seconds=LOCK_WAIT_SECONDS)
    if code != 0:
        print("Migration did not finish; see logs/polybot.log. Nothing in polybot.db was changed.")
        return code
    with closing(_sqlite_readonly()) as c:
        in_sqlite = c.execute("SELECT COUNT(*), MIN(captured_at), MAX(captured_at) FROM snapshots").fetchone()
    con = warehouse.connect()
    try:
        copied = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT date) FROM snapshots").fetchone()
        markets = con.execute("SELECT COUNT(*), COUNT(outcome), COUNT(category) FROM markets").fetchone()
    finally:
        con.close()
    if not copied[0]:
        print("No snapshots were found to copy.")
        return 1
    print(f"SQLite snapshots    : {in_sqlite[0]:,} rows, {in_sqlite[1][:16]} to {in_sqlite[2][:16]} UTC")
    print(
        f"Warehouse snapshots : {copied[0]:,} rows, {copied[1]:%Y-%m-%dT%H:%M} to {copied[2]:%Y-%m-%dT%H:%M} UTC, "
        f"in {copied[3]} day folders"
    )
    print(
        f"Warehouse markets   : {markets[0]:,} markets, {markets[1]:,} with an outcome, {markets[2]:,} with a category"
    )
    # A job may add rows to SQLite after our copy finished, so the warehouse can be slightly behind, never ahead.
    print(
        f"Copy complete       : {'yes' if copied[0] <= in_sqlite[0] else 'NO: the warehouse has more rows than SQLite'}"
    )
    print(
        f"polybot.db          : opened read-only and left as it was ({db_path().stat().st_size:,} bytes, {db_path()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
