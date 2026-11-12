"""The PAPER ledger: cash, positions, PnL, win rate and max drawdown."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybot import ledger
from polybot.fills import Fill

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
MARKET = {"market_id": "m1", "question": "Will it happen?", "category": "politics"}


def buy(c, shares=100.0, price=0.40, fees=0.5, market=MARKET, side="YES", now=NOW) -> int:
    fill = Fill(shares, price, fees, shares * price + fees, "budget")
    return ledger.record_buy(c, market, side, fill, model="test", probability=0.5, reason="edge", now=now)


def position(c, position_id: int):
    return c.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()


def test_new_account_starts_with_the_configured_balance_and_keeps_it():
    c = ledger.open_ledger(1_000.0)
    assert ledger.cash(c) == 1_000.0 and ledger.stats(c, NOW)["equity"] == 1_000.0
    c.close()
    again = ledger.open_ledger(5_000.0)  # changing the setting later does not rewrite history
    assert ledger.starting_balance(again) == 1_000.0


def test_buy_takes_the_full_cost_including_the_fee_out_of_cash():
    c = ledger.open_ledger(1_000.0)
    pid = buy(c)
    assert ledger.cash(c) == pytest.approx(1_000 - 40.5)
    p = position(c, pid)
    assert (p["shares"], p["cost"], p["entry_price"], p["status"], p["side"]) == (100.0, 40.5, 0.40, "OPEN", "YES")
    fill = c.execute("SELECT * FROM fills").fetchone()
    assert (fill["action"], fill["dollars"], fill["reason"]) == ("BUY", 40.5, "edge")


def test_selling_everything_closes_the_position_with_its_profit():
    c = ledger.open_ledger(1_000.0)
    pid = buy(c)
    ledger.record_sell(c, position(c, pid), Fill(100.0, 0.50, 0.25, 49.75, "shares"), "edge gone", NOW)
    p = position(c, pid)
    assert (p["status"], p["shares"], p["exit_reason"]) == ("CLOSED", 0, "edge gone")
    assert p["pnl"] == pytest.approx(49.75 - 40.5) and ledger.cash(c) == pytest.approx(1_000 + 9.25)


def test_thin_book_leaves_the_rest_of_the_position_open():
    c = ledger.open_ledger(1_000.0)
    pid = buy(c)
    ledger.record_sell(c, position(c, pid), Fill(30.0, 0.50, 0.0, 15.0, "depth"), "edge gone", NOW)
    p = position(c, pid)
    assert (p["status"], p["shares"], p["proceeds"]) == ("OPEN", 70.0, 15.0)
    ledger.record_sell(c, p, Fill(70.0, 0.45, 0.0, 31.5, "shares"), "edge gone", NOW)
    assert position(c, pid)["pnl"] == pytest.approx(15 + 31.5 - 40.5)


def test_settlement_pays_a_dollar_a_share_to_the_winner_and_nothing_to_the_loser():
    c = ledger.open_ledger(1_000.0)
    winner, loser = buy(c), buy(c, market={**MARKET, "market_id": "m2"})
    ledger.record_settlement(c, position(c, winner), 1.0, "in its favour", NOW)
    ledger.record_settlement(c, position(c, loser), 0.0, "against it", NOW)
    assert position(c, winner)["pnl"] == pytest.approx(100 - 40.5) and position(c, loser)["pnl"] == pytest.approx(-40.5)
    assert ledger.cash(c) == pytest.approx(1_000 - 81 + 100)
    s = ledger.stats(c, NOW)
    assert (s["closed_positions"], s["wins"], s["win_rate"]) == (2, 1, 0.5)


def test_equity_is_cash_plus_what_the_positions_would_sell_for():
    c = ledger.open_ledger(1_000.0)
    pid = buy(c)
    ledger.mark(c, pid, 0.38)  # the bid fell: the position is now worth less than it cost
    assert ledger.record_equity(c, NOW) == pytest.approx(959.5 + 38.0)
    s = ledger.stats(c, NOW)
    assert s["pnl_total"] == pytest.approx(-2.5) and s["open_positions"] == 1 and s["win_rate"] is None


def test_pnl_today_is_measured_from_the_last_value_of_the_day_before():
    c = ledger.open_ledger(1_000.0)
    pid = buy(c, now=NOW - timedelta(days=1))
    ledger.mark(c, pid, 0.50)
    ledger.record_equity(c, NOW - timedelta(days=1))  # yesterday closed at +9.5
    ledger.mark(c, pid, 0.45)
    ledger.record_equity(c, NOW)  # today it gave 5 back
    s = ledger.stats(c, NOW)
    assert s["pnl_total"] == pytest.approx(4.5) and s["pnl_today"] == pytest.approx(-5.0)
    assert s["pnl_history"] == [9.5, 4.5]


def test_max_drawdown_is_the_worst_fall_from_a_peak():
    assert ledger.max_drawdown([1000, 1100, 990, 1200, 1140]) == pytest.approx(0.10)  # 1100 down to 990
    assert ledger.max_drawdown([1000, 1010, 1020]) == 0
    assert ledger.max_drawdown([]) == 0


def test_stats_count_fees_and_todays_trades_but_not_settlements():
    c = ledger.open_ledger(1_000.0)
    pid = buy(c, fees=0.5)
    buy(c, fees=0.25, market={**MARKET, "market_id": "old"}, now=NOW - timedelta(days=2))
    ledger.record_settlement(c, position(c, pid), 1.0, "in its favour", NOW)
    s = ledger.stats(c, NOW)
    assert s["fees_paid"] == pytest.approx(0.75) and s["trades_today"] == 1


def test_runs_are_recorded():
    c = ledger.open_ledger(1_000.0)
    assert ledger.last_run(c) is None
    ledger.finish_run(c, ledger.start_run(c, NOW), "success", summary="opened 2")
    ledger.finish_run(c, ledger.start_run(c, NOW), "failed", error="ValueError: boom")
    last = ledger.last_run(c)
    assert (last["status"], last["error"]) == ("failed", "ValueError: boom")


def test_recent_fills_come_oldest_first():
    c = ledger.open_ledger(1_000.0)
    for n in range(5):
        buy(c, market={**MARKET, "market_id": f"m{n}"})
    assert [f["market_id"] for f in ledger.recent_fills(c, limit=3)] == ["m2", "m3", "m4"]
