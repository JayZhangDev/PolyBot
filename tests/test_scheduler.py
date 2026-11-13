"""The always-on scheduler, driven by a made-up clock and fake processes (nothing really runs)."""

from __future__ import annotations

import os
import plistlib
import re
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from polybot import cli, scheduler
from polybot.logsetup import console_wanted
from polybot.scheduler import JOBS, Job, Scheduler, choose_jobs, missed_turn, next_run, plan, previous_run

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 13, 3, 31, 20, tzinfo=timezone.utc)
POLITE = signal.SIGTERM if os.name == "nt" else signal.SIGINT  # what ask_to_stop sends here
JOB = {job.name: job for job in JOBS}


class FakeProcess:
    """Stands in for a child process. ``code`` stays None while it "runs"."""

    def __init__(self, args, exit_on_signal: bool = True) -> None:
        self.args = tuple(args)
        self.code: int | None = None
        self.signals: list[int] = []
        self.killed = False
        self.exit_on_signal = exit_on_signal

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if self.exit_on_signal:
            self.code = 1

    def kill(self) -> None:
        self.killed = True
        self.code = -9


class Launcher:
    """Records every process the scheduler starts, and can refuse some (like a missing program)."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.started: list[FakeProcess] = []
        self.fail = fail

    def __call__(self, args) -> FakeProcess:
        if any(part in self.fail for part in args):
            raise OSError("no such program")
        process = FakeProcess(args)
        self.started.append(process)
        return process

    def modules(self) -> list[str]:
        return [p.args[-1] for p in self.started]


def make(jobs=JOBS, launcher: Launcher | None = None) -> tuple[Scheduler, Launcher]:
    launcher = launcher or Launcher()
    return Scheduler(jobs, start_process=launcher), launcher


# ----------------------------- the timetable -----------------------------
def test_next_turns_match_the_windows_timetable():
    expected = {
        "discover": datetime(2026, 11, 13, 3, 37, tzinfo=timezone.utc),
        "pipeline": datetime(2026, 11, 13, 4, 5, tzinfo=timezone.utc),
        "agent": datetime(2026, 11, 13, 3, 40, tzinfo=timezone.utc),
        "paper": datetime(2026, 11, 13, 3, 33, tzinfo=timezone.utc),
        "rollup": datetime(2026, 11, 14, 2, 10, tzinfo=timezone.utc),
    }
    for name, when in expected.items():
        assert next_run(JOB[name], NOW) == when, name


def test_next_turn_is_strictly_later_and_previous_turn_is_at_or_before():
    on_the_dot = datetime(2026, 11, 13, 3, 40, tzinfo=timezone.utc)
    assert next_run(JOB["agent"], on_the_dot) == on_the_dot + timedelta(minutes=10)
    assert previous_run(JOB["agent"], on_the_dot) == on_the_dot
    # Just after midnight, the daily rollup's turn is later the same day.
    early = datetime(2026, 11, 13, 0, 1, tzinfo=timezone.utc)
    assert next_run(JOB["rollup"], early) == datetime(2026, 11, 13, 2, 10, tzinfo=timezone.utc)
    assert previous_run(JOB["rollup"], early) == datetime(2026, 11, 12, 2, 10, tzinfo=timezone.utc)


def test_a_missed_turn_is_caught_up_once():
    paper = JOB["paper"]  # turns at 03:28 and 03:33 around NOW
    assert missed_turn(paper, NOW, None)
    assert missed_turn(paper, NOW, datetime(2026, 11, 13, 3, 23, 5, tzinfo=timezone.utc))
    assert not missed_turn(paper, NOW, datetime(2026, 11, 13, 3, 28, 1, tzinfo=timezone.utc))


def test_a_missed_turn_waits_when_the_next_turn_is_moments_away():
    """Started at 03:36:49, discover should not scan for the missed 03:22 turn and again at 03:37."""
    started = datetime(2026, 11, 13, 3, 36, 49, tzinfo=timezone.utc)
    sched, launcher = make([JOB["stream"], JOB["discover"], JOB["paper"]])
    sched.begin(started)
    sched.tick(started)
    assert launcher.modules() == ["polybot.streamer"]  # paper's 03:38 turn is 71 seconds off, too
    assert sched.due["discover"] == datetime(2026, 11, 13, 3, 37, tzinfo=timezone.utc)
    assert scheduler.catch_up_now(JOB["discover"], datetime(2026, 11, 13, 3, 34, 0, tzinfo=timezone.utc), None)


def test_jobs_match_install_tasks_and_the_shell_scripts():
    """The Windows tasks and this scheduler must run the same programs on the same timetable."""
    text = (ROOT / "scripts" / "install_tasks.ps1").read_text(encoding="utf-8")
    windows_name = {
        "Hourly": "pipeline",
        "Agent": "agent",
        "Discover": "discover",
        "Stream": "stream",
        "Rollup": "rollup",
        "Paper": "paper",
    }
    seen = set()
    for call in text.split("Register-PolyBotTask -Name ")[1:]:  # one piece per task
        name = re.match(r'"PolyBot-(\w+)"', call).group(1)
        job = JOB[windows_name[name]]
        seen.add(job.name)
        every = int(re.search(r"-EveryMinutes (\d+)", call).group(1))
        start = re.search(r"-StartMinute (\S+)", call).group(1)
        assert int(re.search(r"-LimitMinutes (\d+)", call).group(1)) == job.limit_minutes, name
        if not job.always_on:  # (the stream task's 5 minutes is its watchdog, not a timetable)
            assert every == job.every_minutes, name
            if start.isdigit():  # the rollup's start is worked out from 02:10 UTC, checked below
                assert int(start) % every == job.offset_minutes % every, name
        shell = (ROOT / "scripts" / re.search(r'-Script "(\w+\.sh)"', call).group(1)).read_text(encoding="utf-8")
        for command in job.commands:
            assert " ".join(command) in shell, (name, command)
    assert seen == set(JOB)
    assert "AddHours(2).AddMinutes(10)" in text and JOB["rollup"].offset_minutes == 2 * 60 + 10


def test_plan_shows_every_job_and_its_next_turn():
    text = plan(JOBS, NOW)
    assert "stream    all the time" in text
    assert "discover  every 15 min at :07, :22, :37, :52" in text
    assert "paper     every 5 min at :03, :08, ..." in text
    assert "next 03:33, time limit 4 min" in text
    assert "daily at 02:10 UTC" in text and "next 02:10 tomorrow" in text


def test_choose_jobs():
    assert [j.name for j in choose_jobs(None)] == [j.name for j in JOBS]
    assert [j.name for j in choose_jobs("stream, discover")] == ["stream", "discover"]
    with pytest.raises(ValueError, match="unknown job"):
        choose_jobs("stream,trade")


# ----------------------------- starting jobs -----------------------------
def test_first_start_runs_every_job_once():
    sched, launcher = make()
    sched.begin(NOW)
    sched.tick(NOW)
    assert sorted(launcher.modules()) == sorted(job.commands[0][-1] for job in JOBS)
    saved = scheduler.rawstore.read_json(sched.state_path)
    assert saved["running"] == sorted(JOB)
    assert saved["last_started"]["paper"] == NOW.isoformat(timespec="seconds")


def test_a_restart_only_runs_jobs_that_missed_their_turn():
    first, _ = make()
    first.last_started = {
        "paper": datetime(2026, 11, 13, 3, 28, 1, tzinfo=timezone.utc),  # ran at its 03:28 turn
        "discover": datetime(2026, 11, 13, 3, 7, 2, tzinfo=timezone.utc),  # missed 03:22
    }
    first._save_state(NOW)

    sched, launcher = make()  # a new scheduler reads what the last one saved
    sched.begin(NOW)
    sched.tick(NOW)
    assert "polybot.discovery" in launcher.modules()
    assert "polybot.paper" not in launcher.modules()
    assert sched.due["paper"] == datetime(2026, 11, 13, 3, 33, tzinfo=timezone.utc)


def test_an_unreadable_state_file_is_ignored():
    path = scheduler.default_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"last_started": {"paper": "not a time", "agent": "2026-11-13T03:30:00"}}', encoding="utf-8")
    sched, _ = make()
    assert sched.last_started == {}  # a bad time, and a time without a time zone, are both dropped


def test_jobs_start_again_on_their_next_turn():
    sched, launcher = make([JOB["paper"]])
    sched.begin(NOW)
    sched.tick(NOW)
    launcher.started[0].code = 0
    sched.tick(NOW + timedelta(seconds=40))  # finished, not due yet
    assert len(launcher.started) == 1
    sched.tick(datetime(2026, 11, 13, 3, 33, 0, tzinfo=timezone.utc))
    assert len(launcher.started) == 2


def test_a_turn_is_skipped_while_the_last_run_is_still_going():
    slow = Job("slow", (("-m", "slow"),), every_minutes=5, offset_minutes=3)
    sched, launcher = make([slow])
    sched.begin(NOW)
    sched.tick(NOW)
    sched.tick(datetime(2026, 11, 13, 3, 33, 1, tzinfo=timezone.utc))  # its next turn; still running
    assert len(launcher.started) == 1
    assert sched.due["slow"] == datetime(2026, 11, 13, 3, 38, tzinfo=timezone.utc)


def test_steps_run_in_order_and_a_failed_step_does_not_stop_the_next():
    sched, launcher = make([JOB["agent"]])
    sched.begin(NOW)
    sched.tick(NOW)
    launcher.started[0].code = 1  # the agent failed
    sched.tick(NOW + timedelta(seconds=30))
    assert launcher.modules() == ["polybot.agent", "scripts/export_dashboard.py"]
    launcher.started[1].code = 0
    sched.tick(NOW + timedelta(seconds=35))
    assert sched.running == {}


def test_a_run_over_its_time_limit_is_asked_to_stop_then_killed():
    sched, launcher = make([JOB["paper"]])
    sched.begin(NOW)
    sched.tick(NOW)
    run = sched.running["paper"]
    run.process.exit_on_signal = False  # a job that ignores the request
    sched.tick(NOW + timedelta(minutes=3))
    assert run.process.signals == []
    sched.tick(NOW + timedelta(minutes=4, seconds=1))
    assert run.process.signals == [POLITE] and not run.process.killed
    sched.tick(NOW + timedelta(minutes=4, seconds=17))
    assert run.process.killed
    sched.tick(NOW + timedelta(minutes=4, seconds=18))
    assert "paper" not in sched.running


# ----------------------------- the always-on streamer -----------------------------
def test_the_streamer_is_restarted_more_slowly_each_time_it_stops_quickly():
    sched, launcher = make([JOB["stream"]])
    sched.begin(NOW)
    sched.tick(NOW)
    assert "stream" not in sched.due  # running: no start time until it stops

    delays = []
    now = NOW
    for _ in range(3):
        launcher.started[-1].code = 1  # it stops after a few seconds
        now += timedelta(seconds=5)
        sched.tick(now)
        delays.append((sched.due["stream"] - now).total_seconds())
        now = sched.due["stream"]
        sched.tick(now)
    assert delays == [10, 20, 40]
    assert len(launcher.started) == 4

    launcher.started[-1].code = 0  # this time it ran for an hour first
    now += timedelta(hours=1)
    sched.tick(now)
    assert (sched.due["stream"] - now).total_seconds() == 10


def test_the_restart_delay_never_passes_five_minutes():
    sched, _ = make([JOB["stream"]])
    sched.quick_stops["stream"] = 9
    sched._schedule_restart(JOB["stream"], 3.0, NOW)
    assert sched.due["stream"] == NOW + timedelta(seconds=300)


def test_a_streamer_that_cannot_start_is_tried_again_later():
    sched, launcher = make([JOB["stream"]], Launcher(fail=("polybot.streamer",)))
    sched.begin(NOW)
    sched.tick(NOW)
    assert launcher.started == []
    assert sched.due["stream"] == NOW + timedelta(seconds=10)


# ----------------------------- stopping -----------------------------
def test_shutdown_asks_each_job_to_stop_and_kills_one_that_hangs():
    sched, launcher = make([JOB["stream"], JOB["rollup"]])
    sched.begin(NOW)
    sched.tick(NOW)
    stream, rollup = launcher.started
    rollup.exit_on_signal = False

    clock = [NOW]

    def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)

    sched.shutdown(clock=lambda: clock[0], sleep=sleep, grace_seconds=20)
    assert stream.signals == [POLITE] and not stream.killed
    assert rollup.signals == [POLITE] and rollup.killed
    assert clock[0] - NOW >= timedelta(seconds=20)
    assert scheduler.rawstore.read_json(sched.state_path)["running"] == []


def test_run_forever_stops_when_asked():
    sched, launcher = make([JOB["paper"]])
    sched.stop.wait = lambda timeout=None: sched.stop.set()  # the first sleep is "interrupted" by a stop
    sched.run_forever(clock=lambda: NOW)
    assert len(launcher.started) == 1
    assert launcher.started[0].signals == [POLITE]
    assert sched.running == {}


def test_seconds_until_next_is_short_but_not_zero():
    sched, _ = make([JOB["paper"]])
    sched.due["paper"] = NOW + timedelta(seconds=2)
    assert sched.seconds_until_next(NOW) == 2
    sched.due["paper"] = NOW + timedelta(hours=1)
    assert sched.seconds_until_next(NOW) == scheduler.TICK_SECONDS
    sched.due["paper"] = NOW - timedelta(seconds=1)
    assert sched.seconds_until_next(NOW) == 0.2


# ----------------------------- command line and logging -----------------------------
def test_polybot_run_plan_prints_the_timetable(capsys):
    assert cli.main(["run", "--plan", "--only", "paper,stream"]) == 0
    out = capsys.readouterr().out
    assert "paper     every 5 min" in out and "stream    all the time" in out
    assert "discover" not in out


def test_console_echo_can_be_forced_on_or_off(monkeypatch):
    monkeypatch.setenv("POLYBOT_LOG_CONSOLE", "1")
    assert console_wanted()
    monkeypatch.setenv("POLYBOT_LOG_CONSOLE", "0")
    assert not console_wanted()


# ----------------------------- the container and Mac files -----------------------------
def test_dockerignore_keeps_secrets_and_data_out_of_the_image():
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for must in (".env", ".env.*", ".git", "data", "logs", "reports", "**/*.db", ".streamlit/secrets.toml"):
        assert must in ignored, must


def test_dockerfile_runs_the_scheduler_as_an_ordinary_user_with_state_in_one_volume():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["polybot", "run"]' in text
    assert "USER polybot" in text
    for variable in ("POLYBOT_DATA_DIR=/app/data", "POLYBOT_DB=/app/data/polybot.db", "POLYBOT_LOG_DIR=/app/data/"):
        assert variable in text, variable
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "polybot-data:/app/data" in compose and "restart: unless-stopped" in compose


def test_mac_launchd_file_runs_the_scheduler_and_keeps_it_alive():
    plist = plistlib.loads((ROOT / "scripts" / "com.jayzhang.polybot.plist").read_bytes())
    assert plist["ProgramArguments"][1:] == ["-m", "polybot", "run", "--keep-awake"]
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ExitTimeOut"] > scheduler.STOP_GRACE_SECONDS  # launchd waits for the jobs to stop
