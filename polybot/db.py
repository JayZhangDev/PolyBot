"""Single place that decides where the SQLite database lives and opens connections.

The path comes from the POLYBOT_DB environment variable when set, otherwise
`polybot.db` in the project root. Resolving it here (instead of a relative
path in every module) means the pipeline, dashboard and tests all hit the same
file no matter which directory they are launched from.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "polybot.db"


def db_path() -> Path:
    return Path(os.environ.get("POLYBOT_DB", DEFAULT_DB_PATH))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn
