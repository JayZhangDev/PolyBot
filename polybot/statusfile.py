"""The status file that the Hong Kong island page reads.

One small JSON file saying what PolyBot is doing and how the PAPER account
stands. The field names are fixed by the page (see its ``data/SCHEMA.md``), so
they are spelled out here exactly and tested.

Three promises:

* **Always labelled paper.** ``"mode": "paper"`` is written on every file.
* **No secrets.** Only counts, scores and paper dollars go in. No keys, no
  wallet addresses, no transaction hashes.
* **Never half-written.** The file is written under a temporary name and then
  renamed, so the page cannot read a partial file.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polybot import rawstore

STREAM_FRESH_SECONDS = 60  # a streamer status older than this means the streamer is not running
MAX_EVENTS = 15  # the page shows the newest 15
MAX_TEXT = 160
_WINDOWS_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def wsl_path(text: str) -> str | None:
    r"""``C:\Users\me\x.json`` as WSL sees it (``/mnt/c/Users/me/x.json``), or None if it is not a drive path."""
    drive = _WINDOWS_DRIVE.match(text)
    if not drive:
        return None
    return f"/mnt/{drive.group(1).lower()}/" + drive.group(2).replace("\\", "/")


def resolve_path(text: str) -> Path:
    """Turn the ``status.path`` setting into a real path.

    A relative path is inside PolyBot's ``data`` folder. A Windows path also
    works when PolyBot runs inside WSL, where the drive appears under ``/mnt``.
    """
    inside_wsl = wsl_path(text) if os.name != "nt" else None
    if inside_wsl:
        return Path(inside_wsl)
    path = Path(text)
    return path if path.is_absolute() else rawstore.data_dir() / path


def local_time(moment: datetime) -> str:
    """A time as ISO text with this computer's UTC offset, e.g. ``2026-11-12T06:40:00-06:00``."""
    return moment.astimezone().isoformat(timespec="seconds")


def stream_is_live(stream: dict | None, now: datetime) -> bool:
    """True when the streamer said "connected" within the last minute."""
    if not stream or stream.get("state") != "connected":
        return False
    try:
        age = (now - datetime.fromisoformat(stream["updated_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return age < STREAM_FRESH_SECONDS


def _events(fills: list, stream: dict | None, now: datetime) -> list[dict]:
    """Recent paper trades and the last stream disconnect, oldest first."""
    events = []
    for f in fills:
        verb = {"BUY": "bought", "SELL": "sold", "SETTLE": "settled"}[f["action"]]
        text = (
            f"PAPER {verb} {f['shares']:.1f} {f['side']} at {f['avg_price']:.3f} (${f['dollars']:.2f}): {f['question']}"
        )
        events.append({"time": local_time(datetime.fromisoformat(f["ts"])), "level": "info", "message": text[:200]})
    last = (stream or {}).get("last_disconnect")
    if last and last.get("at"):
        when = datetime.fromisoformat(last["at"])
        if now - when < timedelta(hours=24):
            reason = str(last.get("reason", "")).split(":")[0]  # the kind of failure, without the long detail
            events.append(
                {
                    "time": local_time(when),
                    "level": "warning",
                    "message": f"Stream reconnected after a drop ({reason})"[:200],
                }
            )
    events.sort(key=lambda e: datetime.fromisoformat(e["time"]))
    return events[-MAX_EVENTS:]


def _rounded(value: object) -> float | None:
    return round(value, 4) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build(
    account: dict, fills: list, stream: dict | None, calibration: dict | None, now: datetime | None = None
) -> dict:
    """Assemble the status file. ``account`` is ``ledger.stats()``; the others may be missing."""
    now = now or datetime.now(timezone.utc)
    live = stream_is_live(stream, now)
    streamed = int((stream or {}).get("markets_streamed") or 0)
    if live:
        task = f"Streaming {streamed} markets"
    elif stream:
        task = f"Stream is {stream.get('state', 'not running')}; paper account on hold"
    else:
        task = "Stream has not started; paper account on hold"
    return {
        "id": "polybot",
        "updated_at": local_time(now),
        "status": "running" if live else "idle",
        "mode": "paper",
        "sample": False,
        "current_task": task[:MAX_TEXT],
        "pnl": {
            "today": round(account["pnl_today"], 2),
            "total": round(account["pnl_total"], 2),
            "currency": "USD",
            "history": account["pnl_history"],
        },
        "metrics": {
            "markets_streamed": streamed if live else 0,
            "trades_recorded_today": int((stream or {}).get("trades_today") or 0),
            "ws_reconnects_today": int((stream or {}).get("reconnects_today") or 0),
            "brier_market_7d": _rounded((calibration or {}).get("brier_market_7d")),
            # The next four are names the page's PolyBot panel has widgets for.
            "markets_tracked": streamed,
            "open_positions": account["open_positions"],
            "trades_today": account["trades_today"],
            "win_rate": account["win_rate"],
            "max_drawdown": round(account["max_drawdown"], 4),
            "paper_equity": round(account["equity"], 2),
        },
        "events": _events(fills, stream, now),
    }


def write(payload: dict, path: Path) -> None:
    """Write the status file atomically (temporary file, then rename)."""
    rawstore.write_json_atomic(path, payload)
