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

from polybot.db import connect, db_path
from polybot.jobs import init_job_tables
from polybot.logsetup import log_file
from polybot.storage import init_db
from polybot.timeutil import parse_ts

STALE_AFTER_MINUTES = 30  # the agent writes every 10 minutes, so half an hour of silence is a problem
WINDOW_HOURS = 24
MAX_ERRORS_SHOWN = 5


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
    return report


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

    lines += ["", f"Database : {db_path()}", f"Log file : {log_file()}"]
    return "\n".join(lines)


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
