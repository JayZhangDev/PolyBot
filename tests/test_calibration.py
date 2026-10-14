from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_market

from polybot import calibration
from polybot.db import connect
from polybot.resolutions import record_resolutions
from polybot.storage import save_snapshot

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def add_history(mid: str, prices_by_days_before: dict[int, float], outcome: str):
    """Create a market that resolved at NOW with snapshots N days earlier."""
    save_snapshot([make_market(mid)])
    with connect() as c:
        c.execute("DELETE FROM snapshots WHERE market_id = ?", (mid,))
        for days, price in prices_by_days_before.items():
            c.execute("INSERT INTO snapshots (market_id, captured_at, yes_price) VALUES (?, ?, ?)",
                      (mid, (NOW - timedelta(days=days)).isoformat(), price))
    m = make_market(mid, yes=1.0 if outcome == "YES" else 0.0, closed=True)
    m["closedTime"] = NOW.isoformat()
    record_resolutions([m])


def test_brier_known_values():
    assert calibration.brier([1.0, 0.0], [1, 0]) == 0
    assert calibration.brier([0.5, 0.5], [1, 0]) == 0.25
    assert calibration.brier([0.8], [0]) == pytest.approx(0.64)


def test_brier_skill_against_base_rate():
    outcomes = [1, 0, 0, 0]
    assert calibration.brier_skill([0.25] * 4, outcomes) == pytest.approx(0)
    assert calibration.brier_skill([1, 0, 0, 0], outcomes) == pytest.approx(1)
    assert calibration.brier_skill([0.5, 0.5], [1, 1]) is None


def test_reliability_bins_group_by_price():
    bins = calibration.reliability_bins([0.05, 0.08, 0.95, 1.0], [0, 1, 1, 1])
    assert [b["count"] for b in bins] == [2, 2]
    assert bins[0]["observed_rate"] == 0.5
    assert bins[0]["mean_price"] == pytest.approx(0.065)
    assert bins[-1]["bin"] == "0.9–1.0"


def test_forecast_uses_price_from_horizon_before_resolution():
    add_history("a", {10: 0.30, 5: 0.60, 1: 0.90}, "YES")
    [f] = calibration.forecasts(7)
    assert f.price == 0.30 and f.outcome == 1
    [f] = calibration.forecasts(1)
    assert f.price == 0.90


def test_markets_without_old_enough_history_are_skipped():
    add_history("a", {2: 0.4}, "NO")
    assert calibration.forecasts(7) == []
    assert calibration.report(7) == {"horizon_days": 7, "n": 0}


def test_early_decided_markets_are_scored_before_they_pinned():
    # Effectively decided 10 days before close: price pinned to 1.0 from then on.
    add_history("a", {20: 0.40, 10: 1.0, 3: 1.0}, "YES")
    [f] = calibration.forecasts(7)
    assert f.price == 0.40


def test_void_and_pending_markets_are_excluded():
    add_history("a", {10: 0.4}, "YES")
    save_snapshot([make_market("b")])
    record_resolutions([make_market("b", yes=0.5, closed=True)])
    assert [f.market_id for f in calibration.forecasts(7)] == ["a"]


def test_report_includes_longshot_check():
    add_history("a", {10: 0.10}, "NO")
    add_history("b", {10: 0.12}, "NO")
    add_history("c", {10: 0.70}, "YES")
    r = calibration.report(7)
    assert r["n"] == 3
    assert r["longshot"] == {"n": 2, "mean_price": pytest.approx(0.11), "observed_rate": 0}
