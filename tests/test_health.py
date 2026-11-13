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


# --------------------------- live stream section ---------------------------


def stream_status(age_seconds: float, **changes) -> None:
    from polybot import rawstore

    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(timespec="seconds")
    status = {
        "updated_at": stamp,
        "state": "connected",
        "markets_streamed": 300,
        "tokens_subscribed": 600,
        "last_message_at": stamp,
        "trades_today": 1234,
        "book_rows_today": 5678,
        "reconnects_today": 2,
        "last_disconnect": {"at": stamp, "after_seconds": 900, "reason": "DeadConnection: nothing received"},
    }
    rawstore.write_json_atomic(rawstore.status_path(), {**status, **changes})


def test_stream_section_before_the_streamer_has_ever_run():
    assert health.collect()["stream"] is None
    assert "Live stream: never started" in health.format_report(health.collect())


def test_stream_section_while_running():
    from polybot import rawstore

    today = datetime.now(timezone.utc).date().isoformat()
    rawstore.append_rows("trades", ("ts", "x"), [(f"{today}T00:00:01.000Z", "y" * 2_000_000)])
    stream_status(age_seconds=4)
    report = health.collect()
    text = health.format_report(report)
    assert report["stream"]["running"] is True
    assert "State          : connected, status written" in text and "Markets        : 300 (600 tokens)" in text
    assert "1,234 trades, 5,678 book rows, 2 reconnects" in text
    assert "Raw data today : 2.0 MB (book_top 0.0 MB, trades 2.0 MB)" in text
    assert "after 900s: DeadConnection: nothing received" in text


def test_stream_section_when_the_status_is_stale_or_stopped():
    stream_status(age_seconds=600)
    assert health.collect()["stream"]["running"] is False
    assert "NOT RUNNING" in health.format_report(health.collect())
    stream_status(age_seconds=2, state="stopped")
    assert health.collect()["stream"]["running"] is False


# --------------------------- disk usage ---------------------------


def test_disk_usage_is_listed_per_day_newest_first():
    from polybot import rawstore

    rawstore.append_rows("trades", ("ts", "x"), [("2026-11-10T00:00:01.000Z", "y" * 1_000_000)])
    rawstore.append_rows("book_top", ("ts", "x"), [("2026-11-11T00:00:01.000Z", "y" * 3_000_000)])
    save_snapshot([make_market("1")])
    report = health.collect()
    assert [d["day"] for d in report["disk"]["days"]] == ["2026-11-11", "2026-11-10"]
    assert report["disk"]["data_bytes"] > 4_000_000 and report["disk"]["sqlite_bytes"] > 0
    text = health.format_report(report)
    assert "Disk usage by day (UTC), newest first:" in text
    assert (
        "2026-11-11     3.0 MB   book_top 3.0 MB, trades 0.0 MB, snapshots 0.0 MB, price_gaps 0.0 MB, bars 0.0 MB"
        in text
    )
    assert "All of data/ : 4.0 MB" in text


def test_disk_usage_with_no_data_yet():
    assert "no data files yet" in health.format_report(health.collect())


# --------------------------- calibration summary ---------------------------


def test_calibration_summary_appears_once_a_report_exists():
    from polybot import rawstore

    assert "Calibration: no report yet" in health.format_report(health.collect())
    rawstore.write_json_atomic(
        rawstore.reports_dir() / "calibration.json",
        {
            "built_at": "2026-11-12T07:32:00+00:00",
            "brier_market_1d": 0.0835,
            "markets_1d": 70,
            "brier_market_7d": None,
            "markets_7d": 0,
        },
    )
    text = health.format_report(health.collect())
    assert "Calibration (reports/calibration.md, built 2026-11-12 07:32 UTC):" in text
    assert "1 day before : market Brier 0.0835 on 70 markets" in text
    assert "7 days before: no resolved markets yet" in text


# --------------------------- paper account ---------------------------


def test_paper_account_block():
    from polybot import ledger
    from polybot.fills import Fill

    assert "PAPER account: not started" in health.format_report(health.collect())
    c = ledger.open_ledger(1_000.0)
    ledger.record_buy(
        c,
        {"market_id": "1", "question": "Q?"},
        "YES",
        Fill(50.0, 0.40, 0.0, 20.0, "budget"),
        model="m",
        probability=0.5,
        reason="edge",
    )
    ledger.record_equity(c)
    ledger.finish_run(c, ledger.start_run(c), "success", summary="opened 1")
    c.close()
    text = health.format_report(health.collect())
    assert "PAPER account (fake money; no real orders):" in text
    assert "Equity     : $1,000.00  PnL +0.00 total" in text and "1 open, 0 closed, win rate n/a" in text
    assert ", success" in text
