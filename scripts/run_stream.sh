#!/usr/bin/env bash
# The live streamer: records order books and trades from Polymarket's websocket until stopped.
# Started by the PolyBot-Stream scheduled task (see install_tasks.ps1). If one is already
# running, a second copy notices and exits straight away.
set -u
cd "$(dirname "$0")/.."
PY="${POLYBOT_PYTHON:-$HOME/venvs/polybot/bin/python}"
exec "$PY" -m polybot.streamer
