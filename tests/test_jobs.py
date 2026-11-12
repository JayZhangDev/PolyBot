"""The job lock and run records: one job at a time, and a failed run never blocks the next."""

from __future__ import annotations

import os
import time

import pytest

from polybot import jobs
from polybot.db import connect
from polybot.jobs import LockTimeout, acquire_lock, lock_path, release_lock, run_job
from polybot.logsetup import log_file


def no_sleep(_seconds: float) -> None:
    pass


def runs() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM job_runs ORDER BY id")]


def test_lock_is_created_and_released():
    token = acquire_lock("agent", 0)
    assert lock_path().exists()
    release_lock(token)
    assert not lock_path().exists()


def test_second_job_cannot_take_a_held_lock():
    acquire_lock("pipeline", 0)
    with pytest.raises(LockTimeout):
        acquire_lock("agent", 0, sleep=no_sleep)


def test_waiting_job_gets_the_lock_once_it_is_released():
    first = acquire_lock("pipeline", 0)
    token = acquire_lock("agent", 60, sleep=lambda _s: release_lock(first))  # released during the first wait
    assert lock_path().read_text(encoding="utf-8") == token


def test_stale_lock_from_a_killed_run_is_taken_over():
    acquire_lock("agent", 0)
    long_ago = time.time() - jobs.LOCK_STALE_SECONDS - 60
    os.utime(lock_path(), (long_ago, long_ago))
    token = acquire_lock("pipeline", 0, sleep=no_sleep)
    assert lock_path().read_text(encoding="utf-8") == token


def test_release_leaves_someone_elses_lock_alone():
    acquire_lock("pipeline", 0)
    release_lock("agent pid=1 at=earlier")
    assert lock_path().exists()


def test_successful_run_is_recorded_and_logged():
    assert run_job("pipeline", lambda: "500 snapshots") == 0
    (run,) = runs()
    assert (run["job"], run["status"], run["summary"], run["error"]) == ("pipeline", "success", "500 snapshots", None)
    assert run["finished_at"] and not lock_path().exists()
    text = log_file().read_text(encoding="utf-8")
    assert "pipeline run #1 finished: 500 snapshots" in text
    assert text[:4].isdigit() and "Z INFO" in text  # every line starts with a UTC timestamp


def test_failed_run_is_recorded_and_does_not_block_the_next_one():
    def boom() -> str:
        raise ValueError("API exploded")

    assert run_job("agent", boom) == 1
    assert run_job("agent", lambda: "fine") == 0
    first, second = runs()
    assert first["status"] == "failed" and first["error"] == "ValueError: API exploded"
    assert second["status"] == "success" and not lock_path().exists()
    assert "Traceback" in log_file().read_text(encoding="utf-8")


def test_run_is_skipped_while_another_job_holds_the_lock():
    acquire_lock("pipeline", 0)
    called = []
    assert run_job("agent", lambda: called.append(1) or "ran", wait_seconds=0) == 0
    assert called == [] and "skipped" in log_file().read_text(encoding="utf-8")


def test_run_left_running_by_a_killed_job_is_closed_out():
    jobs.init_job_tables()
    with connect() as c:
        c.execute(
            "INSERT INTO job_runs (job, started_at, status) VALUES ('agent', '2026-11-11T14:00:02+00:00', 'running')"
        )
    run_job("pipeline", lambda: "ok")
    killed, _new = runs()
    assert killed["status"] == "failed" and killed["error"] == jobs.INTERRUPTED


# --------------------------- prepare: slow work before the lock ---------------------------


def test_prepare_runs_without_the_lock_and_feeds_work():
    seen = {}

    def prepare() -> list[int]:
        seen["locked_during_prepare"] = lock_path().exists()
        return [1, 2, 3]

    def work(items: list[int]) -> str:
        seen["locked_during_work"] = lock_path().exists()
        return f"{len(items)} items"

    assert run_job("discover", work, prepare=prepare) == 0
    assert seen == {"locked_during_prepare": False, "locked_during_work": True}
    assert runs()[0]["summary"] == "3 items"


def test_failure_in_prepare_is_recorded_and_work_is_not_called():
    called = []

    def prepare() -> None:
        raise ConnectionError("no network")

    assert run_job("discover", lambda _x: called.append(1) or "ran", prepare=prepare) == 1
    (run,) = runs()
    assert called == [] and run["status"] == "failed" and run["error"] == "ConnectionError: no network"
    assert not lock_path().exists()
