"""One process that runs all of PolyBot's jobs: the always-on option.

On the Windows PC the jobs are seven scheduled tasks (``scripts/install_tasks.ps1``).
A Docker container, a cloud server or a Mac has no Windows Task Scheduler, so this
module does the same work from one long-running process:

* it keeps the streamer running, and starts it again if it ever stops;
* it starts every other job on the same timetable the Windows tasks use
  (times are minutes past midnight UTC);
* it runs each job as its own child process, as a scheduled task would, so a
  crash in one job cannot take down the others. The jobs still share the
  database lock in ``polybot/jobs.py``, so two of them never write at once.

If a run is still going when its next turn comes, that turn is skipped. A run
that goes past its time limit is stopped. After the computer was asleep or the
container was down, a job that missed its turn runs once straight away (not
once for every turn it missed), just like the Windows tasks.

    polybot run                         # run everything until stopped
    polybot run --plan                  # print the timetable and exit
    polybot run --only discover,stream  # run just some of the jobs
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from polybot import rawstore
from polybot.db import PROJECT_ROOT
from polybot.logsetup import setup_logging

log = logging.getLogger("polybot.scheduler")

LOG_NAME = "scheduler.log"
TICK_SECONDS = 5.0  # the longest the loop sleeps before looking around again
STOP_GRACE_SECONDS = 20.0  # after a polite stop, how long the jobs get to finish before they are killed
KILL_AFTER_SECONDS = 15.0  # a job over its time limit is asked to stop, then killed this much later
RESTART_FIRST_SECONDS = 10.0  # wait before restarting a streamer that stopped; doubles if it keeps stopping
RESTART_MAX_SECONDS = 300.0  # never wait longer than 5 minutes, the same as the Windows watchdog
STABLE_SECONDS = 600.0  # a streamer that ran at least this long resets the restart delay
CATCH_UP_SECONDS = 120.0  # at start-up a missed turn is skipped if the next one is this close (see catch_up_now)


@dataclass(frozen=True)
class Job:
    """One of PolyBot's jobs and when it runs.

    ``commands`` run one after another; each is the arguments that follow ``python``.
    ``every_minutes`` of 0 means the job runs all the time (the streamer). Otherwise the
    job runs at ``offset_minutes`` past midnight UTC and every ``every_minutes`` after that.
    ``limit_minutes`` stops a run that takes too long (0 means no limit).
    """

    name: str
    commands: tuple[tuple[str, ...], ...]
    every_minutes: int
    offset_minutes: int = 0
    limit_minutes: int = 0

    @property
    def always_on(self) -> bool:
        return self.every_minutes == 0


# The same timetable as scripts/install_tasks.ps1. If you change one, change the other.
JOBS: tuple[Job, ...] = (
    Job("stream", (("-m", "polybot.streamer"),), every_minutes=0),
    Job("discover", (("-m", "polybot.discovery"),), every_minutes=15, offset_minutes=7, limit_minutes=14),
    Job("pipeline", (("-m", "polybot.pipeline"),), every_minutes=60, offset_minutes=5, limit_minutes=30),
    Job(
        "agent",
        (("-m", "polybot.agent"), ("scripts/export_dashboard.py",)),
        every_minutes=10,
        offset_minutes=0,
        limit_minutes=9,
    ),
    Job("paper", (("-m", "polybot.paper"),), every_minutes=5, offset_minutes=3, limit_minutes=4),
    Job("kalshi", (("-m", "polybot.crossmarket"),), every_minutes=10, offset_minutes=4, limit_minutes=9),
    Job("rollup", (("-m", "polybot.rollup"),), every_minutes=24 * 60, offset_minutes=2 * 60 + 10, limit_minutes=60),
)


# ----------------------------- the timetable -----------------------------
def next_run(job: Job, now: datetime) -> datetime:
    """The job's first turn strictly after ``now`` (``now`` must be in UTC)."""
    every = timedelta(minutes=job.every_minutes)
    first_today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=job.offset_minutes)
    turns_since = math.floor((now - first_today) / every)  # negative if the first turn is later today
    return first_today + (turns_since + 1) * every


def previous_run(job: Job, now: datetime) -> datetime:
    """The job's latest turn at or before ``now``."""
    return next_run(job, now) - timedelta(minutes=job.every_minutes)


def missed_turn(job: Job, now: datetime, last_started: datetime | None) -> bool:
    """True when the job has not started since its latest turn."""
    return last_started is None or last_started < previous_run(job, now)


def catch_up_now(job: Job, now: datetime, last_started: datetime | None) -> bool:
    """At start-up: run a job that missed its turn straight away, unless its next turn is moments off.

    Without the second check, a container started at 03:36:50 would scan every market
    for the missed 03:22 turn and then again ten seconds later for the 03:37 one.
    "Moments" is two minutes, or a quarter of the interval for a job that runs more often.
    """
    window = min(CATCH_UP_SECONDS, job.every_minutes * 60 / 4)
    soon = next_run(job, now) - now < timedelta(seconds=window)
    return missed_turn(job, now, last_started) and not soon


def describe(job: Job) -> str:
    """The timetable in words, for ``polybot run --plan``."""
    if job.always_on:
        return "all the time; started again if it stops"
    if job.every_minutes == 24 * 60:
        hours, minutes = divmod(job.offset_minutes, 60)
        return f"daily at {hours:02d}:{minutes:02d} UTC"
    if job.every_minutes == 60:
        return f"hourly at :{job.offset_minutes % 60:02d}"
    marks = [f":{m:02d}" for m in range(job.offset_minutes % job.every_minutes, 60, job.every_minutes)]
    shown = ", ".join(marks) if len(marks) <= 4 else f"{marks[0]}, {marks[1]}, ..."
    return f"every {job.every_minutes} min at {shown}"


def plan(jobs: Sequence[Job], now: datetime) -> str:
    """The timetable and each job's next turn, as printed by ``polybot run --plan``."""
    lines = [f"PolyBot scheduler at {now:%Y-%m-%d %H:%M} UTC: what runs and when", ""]
    for job in jobs:
        line = f"  {job.name:<9} {describe(job):<42}"
        if not job.always_on:
            nxt = next_run(job, now)
            day = "" if nxt.date() == now.date() else " tomorrow"
            line += f"next {nxt:%H:%M}{day}, time limit {job.limit_minutes} min"
        lines.append(line.rstrip())
    return "\n".join(lines)


# ----------------------------- child processes -----------------------------
class Process(Protocol):
    """The parts of ``subprocess.Popen`` the scheduler uses (tests pass a fake)."""

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


def launch(args: Sequence[str]) -> Process:
    """Start ``python <args>`` in the project folder.

    ``start_new_session`` puts the job in its own process group, so pressing Ctrl+C
    in a terminal reaches only the scheduler, which then stops each job itself.
    """
    return subprocess.Popen([sys.executable, *args], cwd=PROJECT_ROOT, start_new_session=True)


def ask_to_stop(process: Process) -> None:
    """Interrupt a job the way Ctrl+C would.

    Python then runs the job's clean-up code before it exits: the database lock is
    released and the streamer writes its last batch. A plain "terminate" would end
    the job on the spot and leave the lock behind. (Windows has no Ctrl+C signal
    for another process, so there it falls back to terminate; the Windows PC uses
    scheduled tasks rather than this scheduler anyway.)
    """
    process.send_signal(signal.SIGTERM if os.name == "nt" else signal.SIGINT)


@dataclass
class Run:
    """A job that is running now."""

    job: Job
    process: Process
    started: datetime  # when its first command started
    step: int = 0  # which of the job's commands is running
    stop_asked: datetime | None = None  # when the scheduler asked it to stop


def default_state_path() -> Path:
    return rawstore.data_dir() / "scheduler.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(text: object) -> datetime | None:
    """A saved time, or None if it is unreadable or has no time zone."""
    try:
        when = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


# ----------------------------- the scheduler -----------------------------
class Scheduler:
    """Keeps the always-on jobs running and starts the others when their turn comes.

    ``tick(now)`` does one round of work and never sleeps, so tests can drive it
    with a made-up clock. ``run_forever`` calls it in a loop.
    """

    def __init__(
        self,
        jobs: Sequence[Job] = JOBS,
        *,
        start_process: Callable[[Sequence[str]], Process] = launch,
        state_path: Path | None = None,
    ) -> None:
        self.jobs = list(jobs)
        self.start_process = start_process
        self.state_path = state_path or default_state_path()
        self.running: dict[str, Run] = {}
        self.due: dict[str, datetime] = {}  # when each job should next start
        self.quick_stops: dict[str, int] = {}  # always-on jobs: how many short runs in a row
        self.last_started: dict[str, datetime] = self._load_last_started()
        self.stop = threading.Event()

    # ---- start-up and state ----
    def _load_last_started(self) -> dict[str, datetime]:
        """When each job last started, from the state file a previous scheduler left behind."""
        saved = (rawstore.read_json(self.state_path) or {}).get("last_started")
        if not isinstance(saved, dict):
            return {}
        times = {name: _parse_time(text) for name, text in saved.items()}
        return {name: when for name, when in times.items() if when is not None}

    def _save_state(self, now: datetime) -> None:
        rawstore.write_json_atomic(
            self.state_path,
            {
                "pid": os.getpid(),
                "updated_at": now.isoformat(timespec="seconds"),
                "running": sorted(self.running),
                "last_started": {name: when.isoformat(timespec="seconds") for name, when in self.last_started.items()},
            },
        )

    def begin(self, now: datetime) -> None:
        """Decide each job's first start: now if it missed its latest turn, otherwise its next turn."""
        for job in self.jobs:
            if job.always_on or catch_up_now(job, now, self.last_started.get(job.name)):
                self.due[job.name] = now
            else:
                self.due[job.name] = next_run(job, now)
        names = ", ".join(job.name for job in self.jobs)
        log.info("Scheduler started (pid %d) with %d jobs: %s", os.getpid(), len(self.jobs), names)

    # ---- one round of work ----
    def tick(self, now: datetime) -> None:
        """Look after the running jobs, then start whatever is due."""
        changed = self._check_running(now)
        changed = self._start_due(now) or changed
        if changed:
            self._save_state(now)

    def _check_running(self, now: datetime) -> bool:
        changed = False
        for run in list(self.running.values()):
            code = run.process.poll()
            if code is None:
                self._enforce_limit(run, now)
                continue
            changed = True
            last_step = run.step + 1 >= len(run.job.commands)
            if last_step or run.stop_asked is not None:
                self._finished(run, code, now)
            else:
                # Like the shell scripts, a later step runs even if an earlier one failed
                # (the dashboard export after the agent, for example).
                if code != 0:
                    log.warning("%s: step %d failed with exit code %s; going on.", run.job.name, run.step + 1, code)
                self._next_step(run, now)
        return changed

    def _enforce_limit(self, run: Run, now: datetime) -> None:
        """Ask a run that is over its time limit to stop, and kill it if it ignores that."""
        limit = run.job.limit_minutes
        if not limit:
            return
        if run.stop_asked is None and now - run.started > timedelta(minutes=limit):
            log.warning("%s has run past its %d-minute limit; asking it to stop.", run.job.name, limit)
            ask_to_stop(run.process)
            run.stop_asked = now
        elif run.stop_asked is not None and now - run.stop_asked > timedelta(seconds=KILL_AFTER_SECONDS):
            log.warning("%s did not stop when asked; killing it.", run.job.name)
            run.process.kill()

    def _next_step(self, run: Run, now: datetime) -> None:
        run.step += 1
        try:
            run.process = self.start_process(run.job.commands[run.step])
        except OSError as exc:
            log.error("Could not start step %d of %s: %s", run.step + 1, run.job.name, exc)
            del self.running[run.job.name]

    def _finished(self, run: Run, code: int, now: datetime) -> None:
        del self.running[run.job.name]
        seconds = (now - run.started).total_seconds()
        if run.stop_asked is not None:
            log.warning("%s stopped after %.0fs (over its time limit).", run.job.name, seconds)
        elif code == 0:
            log.info("%s finished in %.0fs.", run.job.name, seconds)
        else:
            log.warning(
                "%s failed with exit code %s after %.0fs; its own log has details.", run.job.name, code, seconds
            )
        if run.job.always_on:
            self._schedule_restart(run.job, seconds, now)

    def _schedule_restart(self, job: Job, lived_seconds: float, now: datetime) -> None:
        """Bring an always-on job back: soon after a long run, more slowly if it keeps stopping."""
        quick = 1 if lived_seconds >= STABLE_SECONDS else self.quick_stops.get(job.name, 0) + 1
        self.quick_stops[job.name] = quick
        delay = min(RESTART_MAX_SECONDS, RESTART_FIRST_SECONDS * 2 ** (quick - 1))
        self.due[job.name] = now + timedelta(seconds=delay)
        log.warning("%s stopped; starting it again in %.0fs.", job.name, delay)

    def _start_due(self, now: datetime) -> bool:
        changed = False
        for job in self.jobs:
            due = self.due.get(job.name)
            if due is None or due > now:
                continue
            if job.name in self.running:
                # The previous run is still going: skip this turn, as the Windows tasks do.
                log.info("%s is still running from its last turn; skipping this one.", job.name)
            else:
                changed = self._start(job, now) or changed
            if not job.always_on:
                self.due[job.name] = next_run(job, now)
            elif job.name in self.running:
                del self.due[job.name]  # an always-on job gets a new start time only if it stops
        return changed

    def _start(self, job: Job, now: datetime) -> bool:
        try:
            process = self.start_process(job.commands[0])
        except OSError as exc:
            log.error("Could not start %s: %s", job.name, exc)
            if job.always_on:
                self._schedule_restart(job, 0.0, now)
            return False
        self.running[job.name] = Run(job, process, now)
        self.last_started[job.name] = now
        return True

    def seconds_until_next(self, now: datetime) -> float:
        """How long the loop may sleep: until the next start, but never more than TICK_SECONDS."""
        waits = [(when - now).total_seconds() for when in self.due.values()]
        return max(0.2, min([TICK_SECONDS, *waits]))

    # ---- stopping ----
    def shutdown(
        self,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        grace_seconds: float = STOP_GRACE_SECONDS,
    ) -> None:
        """Ask every running job to stop (the streamer writes its last batch), then kill any that hang."""
        for run in self.running.values():
            log.info("Asking %s to stop.", run.job.name)
            ask_to_stop(run.process)
        deadline = clock() + timedelta(seconds=grace_seconds)
        while any(r.process.poll() is None for r in self.running.values()) and clock() < deadline:
            sleep(0.25)
        for run in self.running.values():
            if run.process.poll() is None:
                log.warning("%s did not stop within %.0fs; killing it.", run.job.name, grace_seconds)
                run.process.kill()
        self.running.clear()
        self._save_state(clock())
        log.info("Scheduler stopped.")

    def run_forever(self, clock: Callable[[], datetime] = _utc_now) -> None:
        """Run until ``self.stop`` is set (by a signal), then stop the jobs politely."""
        self.begin(clock())
        while not self.stop.is_set():
            try:
                self.tick(clock())
            except Exception:  # one bad round must not end the always-on process
                log.exception("A scheduler round failed; trying again shortly.")
            self.stop.wait(self.seconds_until_next(clock()))
        self.shutdown(clock)


# ----------------------------- command line -----------------------------
def choose_jobs(only: str | None, jobs: Sequence[Job] = JOBS) -> list[Job]:
    """All jobs, or just the comma-separated names in ``only``. Unknown names are an error."""
    if not only:
        return list(jobs)
    wanted = [name.strip() for name in only.split(",") if name.strip()]
    known = {job.name for job in jobs}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise ValueError(f"unknown job(s): {', '.join(unknown)}. Choose from: {', '.join(sorted(known))}")
    return [job for job in jobs if job.name in wanted]


def keep_mac_awake() -> subprocess.Popen | None:
    """On a Mac, stop the computer sleeping while this process runs and the charger is plugged in.

    ``caffeinate -s -w <pid>`` holds the "do not sleep" request until our process
    exits, then lets go by itself. Other systems have no such command here: on
    Windows, change the power settings instead (see docs/always-on.md).
    """
    if sys.platform != "darwin":
        log.info("--keep-awake only works on a Mac; ignoring it here.")
        return None
    try:
        return subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])
    except OSError as exc:
        log.warning("Could not start caffeinate (%s); the Mac may still sleep.", exc)
        return None


def _stop_on_signals(stop: threading.Event) -> None:
    """Ctrl+C, ``docker stop`` or ``launchctl`` ask the scheduler to stop; it then stops the jobs politely."""

    def handler(signum: int, _frame: object) -> None:
        log.info("Received signal %d; stopping.", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handler)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``polybot run`` (or ``python -m polybot.scheduler``)."""
    parser = argparse.ArgumentParser(
        prog="polybot run", description="Run all of PolyBot's jobs on their timetable from one process."
    )
    parser.add_argument("--plan", action="store_true", help="print the timetable and each job's next turn, then exit")
    parser.add_argument("--only", metavar="JOBS", help="comma-separated jobs to run, e.g. discover,stream")
    parser.add_argument("--keep-awake", action="store_true", help="on a Mac, stop it sleeping while plugged in")
    args = parser.parse_args(argv)
    try:
        jobs = choose_jobs(args.only)
    except ValueError as exc:
        parser.error(str(exc))
    if args.plan:
        print(plan(jobs, _utc_now()))
        return 0

    setup_logging(name=LOG_NAME)
    scheduler = Scheduler(jobs)
    _stop_on_signals(scheduler.stop)
    if args.keep_awake:
        keep_mac_awake()
    scheduler.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
