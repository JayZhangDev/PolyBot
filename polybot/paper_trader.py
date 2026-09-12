"""Paper-trading engine for PolyBot.

Every run:
  1. Look for new signals matching our strategy
  2. Open paper positions against them, respecting position limits and bankroll
  3. Check all open positions for market resolution
  4. Close resolved positions and update bankroll
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from polybot.portfolio import (
    calculate_stake,
    can_open_new_position,
    get_bankroll,
    get_open_positions,
    init_portfolio_tables,
    open_market_ids,
    record_bankroll_change,
    summary,
)
from polybot.signals import find_signals

DB_PATH = Path("polybot.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_snapshot(market_id: str) -> dict | None:
    with _connect() as c:
        row = c.execute("""
            SELECT s.yes_price, s.no_price, m.end_date, m.raw_json
            FROM snapshots s
            JOIN markets m ON m.id = s.market_id
            WHERE s.market_id = ?
            ORDER BY s.id DESC LIMIT 1
        """, (market_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def _open_position(signal: dict, bankroll: float) -> int | None:
    """Open a paper position and debit bankroll. Returns position id."""
    side = signal["side"]
    price = signal["yes_price"] if side == "YES" else signal["no_price"]
    if price is None or price <= 0:
        return None
    stake = calculate_stake(bankroll)
    if stake < 1.0 or stake > bankroll:
        return None
    shares = round(stake / price, 4)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as c:
        cur = c.execute("""
            INSERT INTO positions (market_id, question, side, entry_price, shares, stake, opened_at, opened_reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """, (signal["market_id"], signal["question"], side, price, shares, stake, now, signal["reason"]))
        pos_id = cur.lastrowid
    record_bankroll_change(-stake, f"OPEN #{pos_id}")
    return pos_id


def _market_is_resolved(market_id: str) -> tuple[bool, str | None]:
    """Return (resolved, winning_outcome).
    winning_outcome is 'YES' or 'NO' or None if unresolvable.
    We use two proxies:
      - Market's raw_json has 'closed': true and 'outcomePrices' shows 1.0/0.0
      - end_date has passed AND yes_price is at extreme (>=0.99 or <=0.01)
    """
    snap = _latest_snapshot(market_id)
    if not snap:
        return False, None

    raw = {}
    try:
        raw = json.loads(snap.get("raw_json") or "{}")
    except Exception:
        pass

    closed = bool(raw.get("closed"))
    yes = snap.get("yes_price")
    end = snap.get("end_date")

    end_dt = None
    if end:
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except Exception:
            pass

    if closed:
        if yes is not None and yes >= 0.99:
            return True, "YES"
        if yes is not None and yes <= 0.01:
            return True, "NO"
        return True, None  # closed but ambiguous

    if end_dt and end_dt < datetime.now(timezone.utc):
        if yes is not None and yes >= 0.99:
            return True, "YES"
        if yes is not None and yes <= 0.01:
            return True, "NO"

    return False, None


def _close_position(position: sqlite3.Row, winner: str | None, reason: str) -> None:
    """Close a position. Winner is the resolved side or None for manual/ambiguous."""
    now = datetime.now(timezone.utc).isoformat()
    side = position["side"]
    shares = position["shares"]
    stake = position["stake"]

    if winner == side:
        close_price = 1.0
        proceeds = shares * 1.0
        pnl = proceeds - stake
        status = "CLOSED_WIN"
    elif winner is None:
        # Ambiguous / manual close at current market price
        snap = _latest_snapshot(position["market_id"])
        if snap:
            mark = snap["yes_price"] if side == "YES" else snap["no_price"]
        else:
            mark = 0.0
        close_price = mark if mark is not None else 0.0
        proceeds = shares * close_price
        pnl = proceeds - stake
        status = "CLOSED_MANUAL"
    else:
        close_price = 0.0
        proceeds = 0.0
        pnl = -stake
        status = "CLOSED_LOSS"

    with _connect() as c:
        c.execute("""
            UPDATE positions
            SET closed_at = ?, close_price = ?, pnl = ?, close_reason = ?, status = ?
            WHERE id = ?
        """, (now, close_price, round(pnl, 4), reason, status, position["id"]))
    if proceeds > 0:
        record_bankroll_change(proceeds, f"CLOSE #{position['id']} {status}")


def run() -> dict:
    """One full paper-trader cycle. Returns summary."""
    init_portfolio_tables()

    # 1. Check open positions for resolution
    closed = 0
    for pos in get_open_positions():
        resolved, winner = _market_is_resolved(pos["market_id"])
        if resolved:
            _close_position(pos, winner, f"Resolved: winner={winner}")
            closed += 1

    # 2. Open new positions from signals — priority: odds_mismatch > momentum > longshot
    #    Cap concentration: max 3 new positions per strategy per cycle
    opened = 0
    excluded = open_market_ids()
    sigs = find_signals(exclude_market_ids=excluded)
    priority_order = {"odds_mismatch": 0, "momentum": 1, "longshot": 2}
    sigs.sort(key=lambda s: priority_order.get(s.get("strategy", ""), 3))

    per_strategy_count: dict[str, int] = {}
    for sig in sigs:
        if not can_open_new_position():
            break
        strat = sig.get("strategy", "?")
        if per_strategy_count.get(strat, 0) >= 3:
            continue
        bankroll = get_bankroll()
        pid = _open_position(sig, bankroll)
        if pid:
            opened += 1
            per_strategy_count[strat] = per_strategy_count.get(strat, 0) + 1

    return {"opened": opened, "closed": closed, "summary": summary()}


if __name__ == "__main__":
    result = run()
    s = result["summary"]
    print(f"\n→ Paper-trader run complete")
    print(f"  Opened this run: {result['opened']}")
    print(f"  Closed this run: {result['closed']}")
    print(f"\n  Bankroll:        ${s['bankroll']:.2f}")
    print(f"  Open positions:  {s['open_positions']}")
    print(f"  Closed:          {s['closed_positions']}  (W: {s['wins']}, L: {s['losses']})")
    print(f"  Realized P&L:    ${s['realized_pnl']:.2f}")
    print(f"  Win rate:        {s['win_rate']*100:.1f}%")
