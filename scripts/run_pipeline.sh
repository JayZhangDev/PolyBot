#!/usr/bin/env bash
# One hourly pipeline cycle: snapshot, resolutions, paper trades.
# Called by the PolyBot-Hourly scheduled task (see install_tasks.ps1). Works from any directory.
set -u
cd "$(dirname "$0")/.."
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
"$PY" -m polybot.pipeline