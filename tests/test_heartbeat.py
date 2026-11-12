"""The two heartbeat files, checked against the exact patterns the Peak Watcher matches."""

from __future__ import annotations

import re

from polybot import heartbeat
from polybot.jobs import run_job

# Copied from the watcher (HKWallpaper/peak_watcher/agent.py). If these change there, change them here.
PIPELINE_HEADER = re.compile(r"^=== PolyBot pipeline \S+ (\S+) ===", re.M)
PIPELINE_DONE = "=== Pipeline complete ==="
AGENT_HEADER = re.compile(r"^=== PolyBot agent \S+ (\S+) ===", re.M)
AGENT_DONE = "=== Agent cycle complete ==="
OPEN_POSITIONS = re.compile(r"Open:\s*(\d+)")
AGENT_OPENED = re.compile(r"Opened:\s*(\d+)")
EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit))\b", re.M)


def read(name: str) -> str:
    return (heartbeat.heartbeat_dir() / name).read_text(encoding="utf-8")


def test_successful_pipeline_run_matches_what_the_watcher_looks_for():
    assert run_job("pipeline", lambda: "500 snapshots, 1 newly resolved, opened 0, closed 0, 10 positions open") == 0
    text = read("pipeline.log")
    started = PIPELINE_HEADER.search(text).group(1)
    assert started.startswith("20") and "+00:00" in started  # a time the watcher can parse
    assert PIPELINE_DONE in text and OPEN_POSITIONS.search(text).group(1) == "10"
    assert "Traceback" not in text


def test_successful_agent_run_matches_what_the_watcher_looks_for():
    run_job("agent", lambda: "parsed 2000 markets, 1523 eligible, 17 opportunities, opened 2, skipped 3, settled 0")
    text = read("agent.log")
    assert AGENT_HEADER.search(text) and AGENT_DONE in text and AGENT_OPENED.search(text).group(1) == "2"


def test_failed_run_reads_as_a_crash():
    def boom() -> str:
        raise ConnectionError("API unreachable")

    assert run_job("pipeline", boom) == 1
    text = read("pipeline.log")
    assert PIPELINE_HEADER.search(text) and "Traceback" in text and PIPELINE_DONE not in text
    assert EXC_LINE.search(text).group(1) == "ConnectionError"


def test_other_jobs_leave_no_heartbeat_file():
    run_job("discover", lambda: "listed 5 markets")
    assert not (heartbeat.heartbeat_dir() / "pipeline.log").exists()
    assert not (heartbeat.heartbeat_dir() / "agent.log").exists()


def test_runs_are_appended_oldest_first():
    for n in (1, 2, 3):
        heartbeat.record("agent", f"2026-11-12T10:0{n}:00+00:00", summary=f"opened {n}")
    assert [m for m in AGENT_HEADER.findall(read("agent.log"))] == [
        "2026-11-12T10:01:00+00:00",
        "2026-11-12T10:02:00+00:00",
        "2026-11-12T10:03:00+00:00",
    ]


def test_large_file_is_trimmed_to_whole_recent_runs(monkeypatch):
    monkeypatch.setattr(heartbeat, "MAX_BYTES", 2_000)
    monkeypatch.setattr(heartbeat, "KEEP_BYTES", 600)
    for n in range(60):
        heartbeat.record("agent", f"2026-11-12T10:{n:02d}:00+00:00", summary=f"opened {n}")
    text = read("agent.log")
    assert len(text) < 2_600 and text.startswith("=== PolyBot agent")
    assert AGENT_HEADER.findall(text)[-1] == "2026-11-12T10:59:00+00:00"  # the newest run is always kept


def test_heartbeat_that_cannot_be_written_does_not_break_the_job(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("POLYBOT_HEARTBEAT_DIR", str(tmp_path / "no" / "such" / "folder"))
    with caplog.at_level("WARNING"):
        assert run_job("agent", lambda: "opened 0") == 0
    assert "Could not write the agent heartbeat file" in caplog.text
