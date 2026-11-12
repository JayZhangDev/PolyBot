"""Two small files that other programs read to see that PolyBot is alive.

The Peak Watcher (the program behind the Hong Kong island page) decides whether
the pipeline and the agent are running by reading ``pipeline.log`` and
``agent.log`` in the project folder. It looks for a start line, an end line and
the word "Traceback". PolyBot's real log is ``logs/polybot.log``; these two
files exist only so that watcher keeps working, and each is trimmed so it can
never grow large.

The exact wording matters, because the watcher matches it::

    === PolyBot pipeline <dash> 2026-11-12T12:05:01+00:00 ===
      500 snapshots, 1 newly resolved, opened 0, closed 0, 10 positions open
      Open: 10
    === Pipeline complete ===
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from polybot.db import PROJECT_ROOT

MAX_BYTES = 200_000  # trim a file once it passes this size...
KEEP_BYTES = 60_000  # ...down to about this much, cut at the start of a run
DASH = "\u2014"  # the long dash the original log lines used

# job name -> (file, name in the start line, end line, pattern in the summary, label the watcher looks for)
FORMATS = {
    "pipeline": ("pipeline.log", "pipeline", "=== Pipeline complete ===", r"(\d+) positions open", "Open"),
    "agent": ("agent.log", "agent", "=== Agent cycle complete ===", r"opened (\d+)", "Opened"),
}

log = logging.getLogger(__name__)


def heartbeat_dir() -> Path:
    """The project folder, or the folder named by ``POLYBOT_HEARTBEAT_DIR`` (tests use this)."""
    return Path(os.environ.get("POLYBOT_HEARTBEAT_DIR", PROJECT_ROOT))


def block(job: str, started_at: str, summary: str | None, error: str | None) -> str | None:
    """The lines for one run, or None for a job the watcher does not follow."""
    if job not in FORMATS:
        return None
    _file, name, done, pattern, label = FORMATS[job]
    lines = ["", f"=== PolyBot {name} {DASH} {started_at} ==="]
    if error is not None:  # a failed run: no end line, and the two lines the watcher reads as a crash
        lines += ["Traceback (most recent call last):", error]
    else:
        lines.append(f"  {summary}")
        found = re.search(pattern, summary or "")
        if found:
            lines.append(f"  {label}: {found.group(1)}")
        lines.append(done)
    return "\n".join(lines) + "\n"


def _trim(path: Path) -> None:
    """Keep only the newest runs once the file is large, cutting at the start of a run."""
    if not path.exists() or path.stat().st_size <= MAX_BYTES:
        return
    tail = path.read_bytes()[-KEEP_BYTES:].decode("utf-8", errors="replace")
    start = tail.find("=== PolyBot ")
    path.write_text(tail[start:] if start >= 0 else "", encoding="utf-8")


def record(job: str, started_at: str, summary: str | None = None, error: str | None = None) -> None:
    """Append one run to the job's heartbeat file. Never raises: a heartbeat must not break a job."""
    text = block(job, started_at, summary, error)
    if text is None:
        return
    path = heartbeat_dir() / FORMATS[job][0]
    try:
        _trim(path)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as exc:
        log.warning("Could not write the %s heartbeat file: %s", job, exc)
