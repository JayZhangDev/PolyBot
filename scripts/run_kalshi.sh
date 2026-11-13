#!/usr/bin/env bash
# The Kalshi comparison: pair questions again once a day, then record both sites' prices.
# Public data only. Called every 10 minutes by the PolyBot-Kalshi scheduled task (see install_tasks.ps1).
set -u
cd "$(dirname "$0")/.."
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
"$PY" -m polybot.crossmarket
