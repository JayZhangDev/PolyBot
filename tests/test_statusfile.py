"""The status file for the Hong Kong island page: exact field names, always paper, never a secret."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polybot import rawstore, statusfile

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
ACCOUNT = {
    "pnl_today": 1.234,
    "pnl_total": -5.678,
    "pnl_history": [-6.0, -5.68],
    "open_positions": 3,
    "trades_today": 2,
    "win_rate": 0.5,
    "max_drawdown": 0.01234,
    "equity": 994.322,
}
STREAM = {
    "state": "connected",
    "updated_at": (NOW - timedelta(seconds=5)).isoformat(),
    "markets_streamed": 300,
    "trades_today": 4567,
    "reconnects_today": 2,
    "last_disconnect": {
        "at": (NOW - timedelta(hours=1)).isoformat(),
        "after_seconds": 900,
        "reason": "ConnectionClosedError: received 1013 (try again later) slow consumer",
    },
}
FILL = {
    "ts": (NOW - timedelta(minutes=30)).isoformat(),
    "action": "BUY",
    "side": "NO",
    "shares": 23.0,
    "avg_price": 0.87,
    "dollars": 20.0,
    "question": "Will it happen?",
}


def test_field_names_are_exactly_the_ones_the_page_expects():
    payload = statusfile.build(ACCOUNT, [FILL], STREAM, {"brier_market_7d": 0.34153902}, NOW)
    assert list(payload) == ["id", "updated_at", "status", "mode", "sample", "current_task", "pnl", "metrics", "events"]
    assert list(payload["pnl"]) == ["today", "total", "currency", "history"]
    assert list(payload["metrics"])[:4] == [
        "markets_streamed",
        "trades_recorded_today",
        "ws_reconnects_today",
        "brier_market_7d",
    ]
    assert list(payload["events"][0]) == ["time", "level", "message"]


def test_values_when_the_stream_is_live():
    p = statusfile.build(ACCOUNT, [FILL], STREAM, {"brier_market_7d": 0.34153902}, NOW)
    assert (p["id"], p["status"], p["mode"], p["sample"]) == ("polybot", "running", "paper", False)
    assert p["current_task"] == "Streaming 300 markets"
    assert p["pnl"] == {"today": 1.23, "total": -5.68, "currency": "USD", "history": [-6.0, -5.68]}
    assert p["metrics"]["markets_streamed"] == 300 and p["metrics"]["trades_recorded_today"] == 4567
    assert p["metrics"]["ws_reconnects_today"] == 2 and p["metrics"]["brier_market_7d"] == 0.3415
    assert p["metrics"]["open_positions"] == 3 and p["metrics"]["win_rate"] == 0.5


def test_time_carries_a_utc_offset():
    stamp = statusfile.build(ACCOUNT, [], STREAM, None, NOW)["updated_at"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None and parsed == NOW  # the same moment, written with an offset


def test_idle_when_the_stream_is_stale_missing_or_reconnecting():
    stale = {**STREAM, "updated_at": (NOW - timedelta(minutes=10)).isoformat()}
    for stream in (stale, None, {**STREAM, "state": "reconnecting"}):
        p = statusfile.build(ACCOUNT, [], stream, None, NOW)
        assert p["status"] == "idle" and p["metrics"]["markets_streamed"] == 0 and "on hold" in p["current_task"]
    assert statusfile.build(ACCOUNT, [], None, None, NOW)["metrics"]["brier_market_7d"] is None


def test_events_are_oldest_first_and_say_paper():
    p = statusfile.build(ACCOUNT, [FILL], STREAM, None, NOW)
    assert [e["level"] for e in p["events"]] == ["warning", "info"]  # the drop an hour ago, then the trade
    assert p["events"][0]["message"] == "Stream reconnected after a drop (ConnectionClosedError)"
    assert p["events"][1]["message"] == "PAPER bought 23.0 NO at 0.870 ($20.00): Will it happen?"
    many = [{**FILL, "ts": (NOW - timedelta(minutes=n)).isoformat()} for n in range(40, 0, -1)]
    assert len(statusfile.build(ACCOUNT, many, None, None, NOW)["events"]) == 15


def test_nothing_secret_can_appear_in_the_file():
    text = json.dumps(statusfile.build(ACCOUNT, [FILL], STREAM, None, NOW)).lower()
    names = [k.lower() for part in (statusfile.build(ACCOUNT, [FILL], STREAM, None, NOW),) for k in _all_keys(part)]
    for banned in ("key", "secret", "token", "password", "wallet"):
        assert not any(banned in name for name in names)
    assert "0x" not in text


def _all_keys(value) -> list[str]:
    if isinstance(value, dict):
        return [*value, *[k for v in value.values() for k in _all_keys(v)]]
    if isinstance(value, list):
        return [k for v in value for k in _all_keys(v)]
    return []


def test_written_whole_and_readable():
    path = rawstore.data_dir() / "status" / "polybot.json"
    statusfile.write(statusfile.build(ACCOUNT, [], STREAM, None, NOW), path)
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "paper"
    assert [p.name for p in path.parent.iterdir()] == ["polybot.json"]


def test_path_setting_is_resolved_for_where_polybot_runs():
    assert statusfile.resolve_path("status/polybot.json") == rawstore.data_dir() / "status" / "polybot.json"
    assert (
        statusfile.wsl_path(r"C:\Users\me\HKWallpaper\data\polybot.json")
        == "/mnt/c/Users/me/HKWallpaper/data/polybot.json"
    )
    assert statusfile.wsl_path("D:/data/x.json") == "/mnt/d/data/x.json"
    assert statusfile.wsl_path("status/polybot.json") is None and statusfile.wsl_path("/home/me/x.json") is None
    if os.name == "nt":  # on Windows itself a drive path is used as it is
        assert statusfile.resolve_path(r"C:\Users\me\x.json") == Path(r"C:\Users\me\x.json")
