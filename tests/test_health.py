"""`polybot health`: last fetch, rows in 24 hours, and recent errors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import make_market

from polybot import cli, health
from polybot.jobs import run_job
from polybot.storage import save_snapshot


def later(minutes: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_empty_database_is_not_healthy(capsys):
    report = health.collect()
    assert report["healthy"] is False and report["rows_24h"] == 0
    assert health.main() == 1
    assert "never" in capsys.readouterr().out


def test_fresh_snapshot_is_healthy():
    save_snapshot([make_market("1"), make_market("2")])
    report = health.collect()
    assert report["healthy"] is True
    assert report["rows_24h"] == 2 and report["markets_24h"] == 2
    assert "[OK]" in health.format_report(report)


def test_old_snapshot_is_flagged_stale():
    save_snapshot([make_market("1")])
    report = health.collect(now=later(45))
    assert report["healthy"] is False
    assert 44 < report["minutes_since_fetch"] < 46
    assert 44 < report["longest_gap_minutes"] < 46
    assert "STALE" in health.format_report(report)


def test_rows_older_than_a_day_are_not_counted():
    save_snapshot([make_market("1")])
    assert health.collect(now=later(25 * 60))["rows_24h"] == 0


def test_failed_runs_are_listed_with_their_error():
    def boom() -> str:
        raise RuntimeError("500 Server Error")

    run_job("agent", boom)
    run_job("agent", lambda: "ok")
    report = health.collect()
    assert report["jobs"]["agent"]["ok"] == 1 and report["jobs"]["agent"]["failed"] == 1
    assert report["errors"][0]["error"] == "RuntimeError: 500 Server Error"
    assert "RuntimeError: 500 Server Error" in health.format_report(report)


def test_command_line_entry_point(capsys):
    save_snapshot([make_market("1")])
    assert cli.main(["health"]) == 0
    out = capsys.readouterr().out
    assert "Last successful fetch" in out and "Rows added, last 24h" in out
