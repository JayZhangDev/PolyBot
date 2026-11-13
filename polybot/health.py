"""``polybot health``: is data still arriving, and has anything been failing?

Answers three questions from the database alone:

1. When was the last successful fetch (the newest price snapshot)?
2. How many snapshot rows were added in the last 24 hours?
3. Which runs failed, and why?

The command exits with code 1 when the newest snapshot is too old, so a script
or a scheduled check can tell "healthy" from "stuck" without reading the text.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from polybot import rawstore
from polybot.db import connect, db_path
from polybot.jobs import init_job_tables
from polybot.logsetup import log_file
from polybot.storage import init_db
from polybot.timeutil import parse_ts

STALE_AFTER_MINUTES = 30  # the agent writes every 10 minutes, so half an hour of silence is a problem
WINDOW_HOURS = 24
MAX_ERRORS_SHOWN = 5
STREAM_FRESH_SECONDS = 45  # the streamer rewrites its status every 10 seconds
DISK_DAYS_SHOWN = 7


def _job_stats(c: sqlite3.Connection, cutoff: str) -> dict[str, dict]:
    """Per job: runs that succeeded and failed since ``cutoff``, and the last success ever."""
    stats: dict[str, dict] = {}
    for r in c.execute(
        """
        SELECT job,
               SUM(status = 'success' AND started_at >= ?) AS ok,
               SUM(status = 'failed'  AND started_at >= ?) AS failed,
               MAX(CASE WHEN status = 'success' THEN finished_at END) AS last_success
        FROM job_runs GROUP BY job ORDER BY job
    """,
        (cutoff, cutoff),
    ):
        stats[r["job"]] = {"ok": r["ok"] or 0, "failed": r["failed"] or 0, "last_success": r["last_success"]}
    return stats


def _longest_gap_minutes(c: sqlite3.Connection, cutoff: str, now: datetime) -> float | None:
    """Longest stretch with no snapshot written since ``cutoff``, in minutes.

    Every run stamps all of its rows with one timestamp, so the distinct
    timestamps are the moments data arrived. A long gap usually means the
    computer was asleep or the API was down.
    """
    times = [
        parse_ts(r[0])
        for r in c.execute(
            "SELECT DISTINCT captured_at FROM snapshots WHERE captured_at >= ? ORDER BY captured_at", (cutoff,)
        )
    ]
    times = [t for t in times if t is not None]
    if not times:
        return None
    times.append(now)  # the stretch since the newest snapshot counts too
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(times, times[1:], strict=False)]
    return max(gaps) / 60


def collect(now: datetime | None = None) -> dict:
    """Gather the health numbers into a plain dict (kept separate from printing so it can be tested)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    init_db()
    init_job_tables()
    with connect() as c:
        newest = parse_ts(c.execute("SELECT MAX(captured_at) FROM snapshots").fetchone()[0])
        rows_24h = c.execute("SELECT COUNT(*) FROM snapshots WHERE captured_at >= ?", (cutoff,)).fetchone()[0]
        # Counted from the small markets table. Counting distinct markets in snapshots would look
        # up 200,000+ rows one at a time, which takes minutes when the database is on a Windows
        # drive reached from WSL. The other snapshot queries here are answered by the index alone.
        markets_24h = c.execute("SELECT COUNT(*) FROM markets WHERE last_seen >= ?", (cutoff,)).fetchone()[0]
        errors = [
            dict(r)
            for r in c.execute(
                """
            SELECT job, started_at, error FROM job_runs
            WHERE status = 'failed' AND started_at >= ? ORDER BY id DESC LIMIT ?
        """,
                (cutoff, MAX_ERRORS_SHOWN),
            )
        ]
        report = {
            "now": now,
            "newest_snapshot": newest,
            "minutes_since_fetch": None if newest is None else (now - newest).total_seconds() / 60,
            "rows_24h": rows_24h,
            "markets_24h": markets_24h,
            "longest_gap_minutes": _longest_gap_minutes(c, cutoff, now),
            "jobs": _job_stats(c, cutoff),
            "errors": errors,
        }
    report["healthy"] = (
        report["minutes_since_fetch"] is not None and report["minutes_since_fetch"] <= STALE_AFTER_MINUTES
    )
    report["stream"] = stream_report(now)
    report["disk"] = disk_report()
    report["calibration"] = rawstore.read_json(rawstore.reports_dir() / "calibration.json")
    report["paper"] = paper_report(now)
    report["kalshi"] = kalshi_report(now)
    return report


def kalshi_report(now: datetime) -> dict | None:
    """The Kalshi comparison's pairing and today's price rows, or None if it has never paired anything."""
    pairing = rawstore.read_json(rawstore.data_dir() / "kalshi_pairs.json")
    if pairing is None:
        return None
    today = rawstore.raw_file("price_gaps", now.date().isoformat())
    return {
        "paired_at": pairing.get("updated_at"),
        "pairs": len(pairing.get("matched") or []),
        "kalshi_markets": pairing.get("kalshi_markets", 0),
        "rows_today": rawstore.count_rows("price_gaps", now.date().isoformat()) if today.exists() else 0,
        "last_row_seconds": max(0.0, now.timestamp() - today.stat().st_mtime) if today.exists() else None,
    }


def paper_report(now: datetime) -> dict | None:
    """The PAPER account's numbers, or None if the paper job has never run.

    The ledger is opened read-only: looking at the account must never change it.
    """
    from polybot import ledger

    path = ledger.ledger_path()
    if not path.exists():
        return None
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        last = ledger.last_run(c)
        return {**ledger.stats(c, now), "last_run": dict(last) if last else None}
    finally:
        c.close()


def disk_report(days: int = DISK_DAYS_SHOWN) -> dict:
    """Bytes on disk per day and per table for the most recent days, plus the two totals.

    Only file sizes are read, so this stays quick however much data there is.
    """
    recent = rawstore.usage_days()[-days:]
    data = rawstore.data_dir()
    total = sum(p.stat().st_size for p in data.rglob("*") if p.is_file()) if data.exists() else 0
    return {
        "days": [{"day": day, "bytes": rawstore.day_bytes(day)} for day in reversed(recent)],
        "data_bytes": total,
        "sqlite_bytes": db_path().stat().st_size if db_path().exists() else 0,
    }


def _seconds_since(stamp: object, now: datetime) -> float | None:
    moment = parse_ts(stamp) if isinstance(stamp, str) else None
    return None if moment is None else max(0.0, (now - moment).total_seconds())


def stream_report(now: datetime) -> dict | None:
    """What the live streamer last said about itself, or None if it has never run.

    The streamer rewrites its status file every 10 seconds. If that file is
    older than ``STREAM_FRESH_SECONDS`` the streamer is not running, whatever
    the file claims.
    """
    status = rawstore.read_json(rawstore.status_path())
    if status is None:
        return None
    age = _seconds_since(status.get("updated_at"), now)
    running = age is not None and age < STREAM_FRESH_SECONDS and status.get("state") != "stopped"
    return {
        **status,
        "status_age_seconds": age,
        "running": running,
        "seconds_since_message": _seconds_since(status.get("last_message_at"), now),
        "bytes_today": rawstore.day_bytes(now.date().isoformat()),
    }


def _when(ts: datetime | str | None) -> str:
    """A timestamp as '2026-11-11 21:34 UTC', or 'never'."""
    dt = parse_ts(ts) if isinstance(ts, str) else ts
    return "never" if dt is None else dt.strftime("%Y-%m-%d %H:%M UTC")


def format_report(r: dict) -> str:
    """Turn the dict from :func:`collect` into the text the command prints."""
    lines = [f"PolyBot health at {_when(r['now'])}", ""]
    if r["newest_snapshot"] is None:
        lines.append("Last successful fetch : never (the database has no snapshots)")
    else:
        flag = "OK" if r["healthy"] else f"STALE: more than {STALE_AFTER_MINUTES} minutes old"
        lines.append(
            f"Last successful fetch : {_when(r['newest_snapshot'])} ({r['minutes_since_fetch']:.0f} min ago)  [{flag}]"
        )
    lines.append(f"Rows added, last {WINDOW_HOURS}h  : {r['rows_24h']:,} snapshots across {r['markets_24h']:,} markets")
    if r["longest_gap_minutes"] is not None:
        lines.append(f"Longest gap, last {WINDOW_HOURS}h : {r['longest_gap_minutes']:.0f} min with no data")

    lines += ["", f"Runs in the last {WINDOW_HOURS}h:"]
    if not r["jobs"]:
        lines.append("  none recorded yet")
    for job, s in r["jobs"].items():
        lines.append(f"  {job:<9} {s['ok']:>4} ok  {s['failed']:>3} failed   last success {_when(s['last_success'])}")

    lines += ["", f"Errors in the last {WINDOW_HOURS}h:"]
    if not r["errors"]:
        lines.append("  none")
    for e in r["errors"]:
        lines.append(f"  {_when(e['started_at'])}  {e['job']:<9} {(e['error'] or 'no message')[:110]}")

    lines += ["", *_stream_lines(r.get("stream"))]
    lines += ["", *_disk_lines(r["disk"])]
    lines += ["", *_calibration_lines(r.get("calibration"))]
    lines += ["", *_paper_lines(r.get("paper"))]
    lines += ["", *_kalshi_lines(r.get("kalshi"))]
    lines += [
        "",
        f"Database   : {db_path()}",
        f"Log file   : {log_file()}",
        f"Stream log : {log_file('stream.log')}",
        f"Live data  : {rawstore.data_dir() / 'raw'}",
    ]
    return "\n".join(lines)


def _calibration_lines(summary: dict | None) -> list[str]:
    """The market's Brier score at each horizon, from the last calibration report."""
    if not summary:
        return ["Calibration: no report yet (run `polybot calibrate`)"]
    lines = [f"Calibration (reports/calibration.md, built {_when(summary.get('built_at'))}):"]
    for days in (1, 7):
        score, markets = summary.get(f"brier_market_{days}d"), summary.get(f"markets_{days}d", 0)
        name = "1 day before " if days == 1 else f"{days} days before"
        detail = f"market Brier {score:.4f} on {markets} markets" if score is not None else "no resolved markets yet"
        lines.append(f"  {name}: {detail}")
    return lines


def _paper_lines(p: dict | None) -> list[str]:
    """The "PAPER account" block."""
    if p is None:
        return ["PAPER account: not started (run `polybot paper`)"]
    win_rate = "n/a" if p["win_rate"] is None else f"{p['win_rate']:.0%}"
    last = p["last_run"]
    ran = "never finished a run" if last is None else f"{_when(last['finished_at'])}, {last['status']}"
    return [
        "PAPER account (fake money; no real orders):",
        f"  Equity     : ${p['equity']:,.2f}  PnL {p['pnl_total']:+,.2f} total, {p['pnl_today']:+,.2f} today",
        f"  Positions  : {p['open_positions']} open, {p['closed_positions']} closed, win rate {win_rate}, "
        f"max drawdown {p['max_drawdown']:.1%}",
        f"  Last run   : {ran}",
    ]


def _kalshi_lines(k: dict | None) -> list[str]:
    """The "Kalshi comparison" block."""
    if k is None:
        return ["Kalshi comparison: not paired yet (run `polybot kalshi`)"]
    last = "none today" if k["last_row_seconds"] is None else f"last written {k['last_row_seconds'] / 60:.0f} min ago"
    return [
        "Kalshi comparison (public prices only):",
        f"  Pairs      : {k['pairs']} matched, paired {_when(k['paired_at'])} "
        f"from {k['kalshi_markets']:,} Kalshi markets",
        f"  Prices     : {k['rows_today']:,} rows today, {last}  (`polybot gaps` for the summary)",
    ]


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def _disk_lines(disk: dict) -> list[str]:
    """The "Disk usage" block: one line per day, newest first, then the totals."""
    lines = ["Disk usage by day (UTC), newest first:"]
    if not disk["days"]:
        lines.append("  no data files yet")
    for entry in disk["days"]:
        sizes = entry["bytes"]
        detail = ", ".join(f"{table} {_megabytes(size)}" for table, size in sizes.items())
        lines.append(f"  {entry['day']}  {_megabytes(sum(sizes.values())):>9}   {detail}")
    lines.append(
        f"  All of data/ : {_megabytes(disk['data_bytes'])}    SQLite database : {_megabytes(disk['sqlite_bytes'])}"
    )
    return lines


def _stream_lines(s: dict | None) -> list[str]:
    """The "Live stream" block of the report."""
    if s is None:
        return ["Live stream: never started"]
    if s["running"]:
        state = f"{s['state']}, status written {s['status_age_seconds']:.0f}s ago"
    else:
        state = f"NOT RUNNING (last status {_when(s.get('updated_at'))}, state '{s.get('state')}')"
    quiet = s["seconds_since_message"]
    sizes = s["bytes_today"]
    lines = [
        "Live stream:",
        f"  State          : {state}",
        f"  Markets        : {s.get('markets_streamed', 0)} ({s.get('tokens_subscribed', 0)} tokens)",
        f"  Last message   : {'never' if quiet is None else f'{quiet:.0f}s ago'}",
        f"  Today (UTC)    : {s.get('trades_today', 0):,} trades, {s.get('book_rows_today', 0):,} book rows, "
        f"{s.get('reconnects_today', 0)} reconnects",
        f"  Raw data today : {_megabytes(sizes['book_top'] + sizes['trades'])} "
        f"(book_top {_megabytes(sizes['book_top'])}, trades {_megabytes(sizes['trades'])})",
    ]
    last = s.get("last_disconnect")
    if last:
        lines.append(
            f"  Last disconnect: {_when(last.get('at'))} after {last.get('after_seconds')}s: "
            f"{str(last.get('reason'))[:90]}"
        )
    return lines


def main() -> int:
    """Print the report. Exit code 0 when data is fresh, 1 when it is stale or missing."""
    try:
        report = collect()
    except sqlite3.DatabaseError as exc:
        print(f"Could not read the database ({exc}). A job may be writing; try again in a minute.")
        return 1
    print(format_report(report))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
