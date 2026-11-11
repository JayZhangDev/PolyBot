#!/usr/bin/env bash
# One 10-minute agent cycle, then refresh the static dashboard data (docs/data.json).
# Called by the PolyBot-Agent scheduled task (see install_tasks.ps1). Works from any directory.
set -u
cd "$(dirname "$0")/.."
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
"$PY" -m polybot.agent
"$PY" scripts/export_dashboard.py