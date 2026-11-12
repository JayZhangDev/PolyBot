#!/usr/bin/env bash
# One PAPER trading cycle: settle, exit, enter, value the account, write the status file.
# Fake money only. Called every 5 minutes by the PolyBot-Paper scheduled task (see install_tasks.ps1).
set -u
cd "$(dirname "$0")/.."
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
"$PY" -m polybot.paper
