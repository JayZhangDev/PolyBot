#!/usr/bin/env bash
# One 10-minute agent cycle, then refresh the static dashboard data (docs/data.json).
# Run from the project root by the Windows scheduled task (see install_agent_task.ps1).
set -u
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
"$PY" -m polybot.agent
"$PY" scripts/export_dashboard.py
