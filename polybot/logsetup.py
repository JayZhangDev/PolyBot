"""Logging for the scheduled jobs: one rotating file with a UTC timestamp on every line.

"Rotating" means the file is capped in size. When ``logs/polybot.log`` reaches
about 2 MB it is renamed to ``polybot.log.1`` and a fresh file starts; five old
files are kept, so the logs can never slowly fill the disk.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from polybot.db import PROJECT_ROOT

LOG_NAME = "polybot.log"
MAX_BYTES = 2_000_000
BACKUP_COUNT = 5
LINE_FORMAT = "%(asctime)sZ %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def log_dir() -> Path:
    """Where the log lives: ``logs/`` in the project, or ``POLYBOT_LOG_DIR`` when set (tests use this)."""
    return Path(os.environ.get("POLYBOT_LOG_DIR", PROJECT_ROOT / "logs"))


def log_file() -> Path:
    return log_dir() / LOG_NAME


def _formatter() -> logging.Formatter:
    formatter = logging.Formatter(LINE_FORMAT, TIME_FORMAT)
    formatter.converter = time.gmtime  # UTC, to match the timestamps stored in the database
    return formatter


def setup_logging(console: bool | None = None) -> Path:
    """Send all log messages to the rotating file (and to the screen when run by hand).

    Safe to call more than once: handlers from an earlier call are replaced,
    not stacked, so a message is never written twice. Returns the log file path.
    """
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "polybot_handler", False)]:
        root.removeHandler(old)
        old.close()

    log_dir().mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    ]
    # Under the scheduler there is no screen, so only echo when a person is watching.
    if console is None:
        console = sys.stderr.isatty()
    if console:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(_formatter())
        handler.polybot_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    return log_file()
