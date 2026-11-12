#!/usr/bin/env bash
# The daily rollup: pack finished days into Parquet, copy new snapshots out of SQLite,
# and build one-minute bars. Called by the PolyBot-Rollup scheduled task (see install_tasks.ps1).
set -u
cd "$(dirname "$0")/.."
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
"$PY" -m polybot.rollup
