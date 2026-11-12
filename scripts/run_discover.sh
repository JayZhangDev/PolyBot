#!/usr/bin/env bash
# Refresh the market catalog: every open market that passes the floors in config.yaml.
# Called every 15 minutes by the PolyBot-Discover scheduled task (see install_tasks.ps1).
set -u
cd "$(dirname "$0")/.."
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
"$PY" -m polybot.discovery
