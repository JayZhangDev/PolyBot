"""Bookkeeping for the scheduled jobs: one at a time, and every run leaves a record.

Two problems this solves:

1. **Two writers.** The hourly pipeline and the 10-minute agent share one SQLite
   file. Both writing at once is risky, especially on a Windows drive reached
   from WSL. A lock file makes the second job wait for the first to finish.
2. **Silent failures.** Each run gets a row in ``job_runs`` (started, finished,
   status, error), so ``polybot health`` can show what failed and when. A crash
   is caught, logged with a timestamp and recorded; it never stops the next run.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from polybot.db import connect, db_path
from polybot.logsetup import setup_logging

# A scheduled run is killed after 9 minutes (agent) and normally finishes much
# sooner, so a lock older than this belongs to a run that died without tidying up.
LOCK_STALE_SECONDS = 12 * 60
POLL_SECONDS = 2
INTERRUPTED = "interrupted: the run was killed or crashed before it could finish"

log = logging.getLogger(__name__)


class LockTimeout(RuntimeError):
    """Another job held the lock for longer than we were willing to wait."""


# ---------------------------------------------------------------------------
# The lock file
# ---------------------------------------------------------------------------
def lock_path() -> Path:
    """The lock sits next to the database it protects (``polybot.db.lock``)."""
    db = db_path()
    return db.with_name(db.name + ".lock")


def _try_create(path: Path, token: str) -> bool:
    """Create the lock file, or return False if it already exists.

    ``O_EXCL`` makes "check that it is missing, then create it" one step, so two
    jobs starting in the same instant cannot both succeed.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    return True


def _clear_if_stale(path: Path, now: float) -> bool:
    """Remove a lock left behind by a run that was killed. Returns True if the lock is now gone.

    The file is renamed before it is deleted: if two waiting jobs notice the
    stale lock together, only one rename can succeed, so only one takes over.
    """
    try:
        age = now - path.stat().st_mtime
    except FileNotFoundError:
        return True
    if age <= LOCK_STALE_SECONDS:
        return False
    grave = path.with_name(f"{path.name}.stale.{os.getpid()}")
    try:
        os.replace(path, grave)
    except FileNotFoundError:
        return True
    grave.unlink(missing_ok=True)
    log.warning("Removed a stale lock (%d minutes old): the run that made it never finished", age // 60)
    return True


def acquire_lock(
    owner: str,
    wait_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> str:
    """Take the lock, waiting up to ``wait_seconds`` for another job to finish.

    Returns a token that :func:`release_lock` needs. Raises :class:`LockTimeout`
    if the lock is still held when the wait runs out.
    """
    path = lock_path()
    token = f"{owner} pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}"
    deadline = clock() + wait_seconds
    announced = False
    while not _try_create(path, token):
        if _clear_if_stale(path, clock()):
            continue  # the lock is free now, so try to take it straight away
        if clock() >= deadline:
            raise LockTimeout(f"another job still held {path.name} after {wait_seconds:.0f}s")
        if not announced:
            log.info("%s is waiting for another job to finish", owner)
            announced = True
        sleep(POLL_SECONDS)
    return token


def release_lock(token: str) -> None:
    """Delete the lock, but only if it is still ours (it is not if we were judged stale)."""
    path = lock_path()
    try:
        if path.read_text(encoding="utf-8") == token:
            path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def job_lock(owner: str, wait_seconds: float) -> Iterator[None]:
    """``with job_lock("export", 60): ...`` holds the lock for the block."""
    token = acquire_lock(owner, wait_seconds)
    try:
        yield
    finally:
        release_lock(token)


# ---------------------------------------------------------------------------
# The job_runs table
# ---------------------------------------------------------------------------
def init_job_tables() -> None:
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job TEXT NOT NULL,               -- 'pipeline' or 'agent'
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,            -- running | success | failed
            summary TEXT,                    -- one line describing what the run did
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at);
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_run(job: str, started_at: str) -> int:
    """Record that a run began. Must be called while holding the lock.

    Because we hold the lock, no other job can be mid-run, so any row still
    marked 'running' belongs to a run that was killed. It is closed out as
    failed here instead of looking busy forever.
    """
    init_job_tables()
    with connect() as c:
        c.execute(
            "UPDATE job_runs SET status = 'failed', finished_at = ?, error = ? WHERE status = 'running'",
            (_now(), INTERRUPTED),
        )
        cur = c.execute("INSERT INTO job_runs (job, started_at, status) VALUES (?, ?, 'running')", (job, started_at))
        return int(cur.lastrowid)


def _finish_run(run_id: int, status: str, summary: str | None = None, error: str | None = None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, summary = ?, error = ? WHERE id = ?",
            (_now(), status, summary, error, run_id),
        )


def run_job(
    job: str, work: Callable[..., str], *, prepare: Callable[[], object] | None = None, wait_seconds: float = 300
) -> int:
    """Run one scheduled job safely. Returns a process exit code: 0 fine, 1 failed.

    ``work`` does the real job and returns a one-line summary. Whatever happens
    inside it, this function logs the outcome, records it and releases the
    lock, so a failed run can never block or crash the next one.

    ``prepare`` is optional and runs first, before the lock is taken. Slow
    network work belongs there, so other jobs are not kept waiting while we
    download; whatever it returns is handed to ``work``.
    """
    setup_logging()
    started = _now()
    prepared: object = None
    early_failure: Exception | None = None
    if prepare is not None:
        try:
            prepared = prepare()
        except Exception as exc:
            early_failure = exc  # recorded below, once we hold the lock and may write to the database
    try:
        token = acquire_lock(job, wait_seconds)
    except LockTimeout as exc:
        log.warning("%s run skipped: %s", job, exc)
        return 0
    run_id: int | None = None
    try:
        run_id = _start_run(job, started)
        log.info("%s run #%d started", job, run_id)
        if early_failure is not None:
            raise early_failure
        summary = work() if prepare is None else work(prepared)
        _finish_run(run_id, "success", summary=summary)
        log.info("%s run #%d finished: %s", job, run_id, summary)
        return 0
    except Exception as exc:  # a scheduled job must always leave a record, whatever went wrong
        log.exception("%s run failed", job)
        _record_failure(run_id, f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        release_lock(token)


def _record_failure(run_id: int | None, error: str) -> None:
    """Mark the run failed. If even that fails (database trouble), the log line above still exists."""
    if run_id is None:
        return
    try:
        _finish_run(run_id, "failed", error=error)
    except Exception:
        log.exception("Could not record the failure in job_runs")
