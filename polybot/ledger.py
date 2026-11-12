"""The PAPER ledger: a fake-money account that records every simulated trade.

It lives in its own small database, ``data/paper.db``, separate from
everything else. Deleting that file resets the account. Nothing in here can
reach an exchange: it is bookkeeping only.

Four tables:

* ``positions``  what is (or was) held: market, side, shares, what it cost
* ``fills``      every simulated buy, sell and settlement, with the reason
* ``equity``     the account's value after each run, which gives the PnL curve
* ``runs``       when the paper job ran and whether it worked

Words used here:

* **Equity** is cash plus what the open positions could be sold for right now.
* **PnL** (profit and loss) is equity minus the starting balance.
* **Drawdown** is how far equity has fallen from its highest point so far;
  **max drawdown** is the worst such fall, as a share of that peak.
* **Win rate** is the share of closed positions that made money.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from polybot import rawstore
from polybot.fills import Fill

SHARES_DUST = 1e-6  # fewer shares than this left over counts as fully sold
HISTORY_POINTS = 200  # how many equity points the status file carries


def ledger_path() -> Path:
    return rawstore.data_dir() / "paper.db"


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def open_ledger(starting_balance: float) -> sqlite3.Connection:
    """Open the paper account, creating it with ``starting_balance`` the first time.

    The starting balance is stored when the account is created. Changing the
    setting later does not rewrite history; delete ``data/paper.db`` to start over.
    """
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS account (name TEXT PRIMARY KEY, value REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT NOT NULL,
        question TEXT NOT NULL,
        category TEXT,
        side TEXT NOT NULL,                 -- YES or NO
        shares REAL NOT NULL,               -- shares still held
        cost REAL NOT NULL,                 -- dollars paid in total, fees included
        entry_price REAL NOT NULL,          -- average price per share, before fees
        proceeds REAL NOT NULL DEFAULT 0,   -- dollars received so far from selling or settling
        last_price REAL,                    -- the price it could last be sold at
        model TEXT, model_probability REAL,
        opened_at TEXT NOT NULL, closed_at TEXT,
        status TEXT NOT NULL DEFAULT 'OPEN',-- OPEN or CLOSED
        exit_reason TEXT, pnl REAL
    );
    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
    CREATE TABLE IF NOT EXISTS fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, position_id INTEGER, market_id TEXT NOT NULL, question TEXT NOT NULL,
        action TEXT NOT NULL,               -- BUY, SELL or SETTLE
        side TEXT NOT NULL, shares REAL NOT NULL, avg_price REAL NOT NULL, fees REAL NOT NULL,
        dollars REAL NOT NULL,              -- paid (BUY) or received (SELL, SETTLE)
        limited_by TEXT, reason TEXT
    );
    CREATE TABLE IF NOT EXISTS equity (
        ts TEXT PRIMARY KEY, cash REAL NOT NULL, positions_value REAL NOT NULL, equity REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
        status TEXT NOT NULL, summary TEXT, error TEXT
    );
    """)
    if c.execute("SELECT 1 FROM account WHERE name = 'starting_balance'").fetchone() is None:
        c.execute(
            "INSERT INTO account VALUES ('starting_balance', ?), ('cash', ?)", (starting_balance, starting_balance)
        )
    c.commit()
    return c


def _account(c: sqlite3.Connection, name: str) -> float:
    return float(c.execute("SELECT value FROM account WHERE name = ?", (name,)).fetchone()[0])


def cash(c: sqlite3.Connection) -> float:
    return _account(c, "cash")


def starting_balance(c: sqlite3.Connection) -> float:
    return _account(c, "starting_balance")


def _add_cash(c: sqlite3.Connection, amount: float) -> None:
    c.execute("UPDATE account SET value = value + ? WHERE name = 'cash'", (amount,))


def open_positions(c: sqlite3.Connection) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id").fetchall()


def _fill_row(
    c: sqlite3.Connection,
    now: datetime | None,
    position_id: int,
    market_id: str,
    question: str,
    action: str,
    side: str,
    fill: Fill,
    reason: str,
) -> None:
    c.execute(
        """
        INSERT INTO fills (ts, position_id, market_id, question, action, side, shares, avg_price, fees, dollars,
                           limited_by, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            _stamp(now),
            position_id,
            market_id,
            question,
            action,
            side,
            fill.shares,
            fill.avg_price,
            fill.fees,
            fill.dollars,
            fill.limited_by,
            reason,
        ),
    )


def record_buy(
    c: sqlite3.Connection,
    market: dict,
    side: str,
    fill: Fill,
    *,
    model: str,
    probability: float,
    reason: str,
    now: datetime | None = None,
) -> int:
    """Open a position from a simulated buy and take its cost out of cash. Returns the position id."""
    cur = c.execute(
        """
        INSERT INTO positions (market_id, question, category, side, shares, cost, entry_price, last_price, model,
                               model_probability, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            market["market_id"],
            market["question"],
            market.get("category"),
            side,
            fill.shares,
            fill.dollars,
            fill.avg_price,
            fill.avg_price,
            model,
            probability,
            _stamp(now),
        ),
    )
    position_id = int(cur.lastrowid)
    _fill_row(c, now, position_id, market["market_id"], market["question"], "BUY", side, fill, reason)
    _add_cash(c, -fill.dollars)
    c.commit()
    return position_id


def _reduce(
    c: sqlite3.Connection,
    position: sqlite3.Row,
    shares_gone: float,
    dollars_in: float,
    reason: str,
    now: datetime | None,
) -> None:
    """Take shares out of a position and bank what they brought; close it when none are left."""
    left = position["shares"] - shares_gone
    proceeds = position["proceeds"] + dollars_in
    if left <= SHARES_DUST:
        c.execute(
            """UPDATE positions SET shares = 0, proceeds = ?, status = 'CLOSED', closed_at = ?, exit_reason = ?,
                     pnl = ? WHERE id = ?""",
            (proceeds, _stamp(now), reason, proceeds - position["cost"], position["id"]),
        )
    else:
        c.execute("UPDATE positions SET shares = ?, proceeds = ? WHERE id = ?", (left, proceeds, position["id"]))
    _add_cash(c, dollars_in)


def record_sell(
    c: sqlite3.Connection, position: sqlite3.Row, fill: Fill, reason: str, now: datetime | None = None
) -> None:
    """Record a simulated sale. If the book was too thin to sell everything, the rest stays open."""
    _fill_row(
        c, now, position["id"], position["market_id"], position["question"], "SELL", position["side"], fill, reason
    )
    _reduce(c, position, fill.shares, fill.dollars, reason, now)
    c.commit()


def record_settlement(
    c: sqlite3.Connection, position: sqlite3.Row, payout_per_share: float, outcome: str, now: datetime | None = None
) -> None:
    """The market resolved: each share pays ``payout_per_share`` (1 for the winning side, 0 for the losing one)."""
    paid = position["shares"] * payout_per_share
    fill = Fill(position["shares"], payout_per_share, 0.0, paid, "shares")
    reason = f"market resolved {outcome}"
    _fill_row(
        c, now, position["id"], position["market_id"], position["question"], "SETTLE", position["side"], fill, reason
    )
    _reduce(c, position, position["shares"], paid, reason, now)
    c.commit()


def mark(c: sqlite3.Connection, position_id: int, price: float) -> None:
    """Remember the price a position could last be sold at (used when its book is not available)."""
    c.execute("UPDATE positions SET last_price = ? WHERE id = ?", (price, position_id))


def record_equity(c: sqlite3.Connection, now: datetime | None = None) -> float:
    """Value the account (cash plus open positions at their last sale price), store it and return it."""
    value = c.execute("SELECT COALESCE(SUM(shares * last_price), 0) FROM positions WHERE status = 'OPEN'").fetchone()[0]
    money = cash(c)
    c.execute("INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?)", (_stamp(now), money, value, money + value))
    c.commit()
    return money + value


def max_drawdown(equities: list[float]) -> float:
    """The worst fall from a peak, as a share of that peak (0.12 means the account once fell 12%)."""
    worst, peak = 0.0, float("-inf")
    for value in equities:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def stats(c: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The account at a glance: equity, PnL, positions, win rate and max drawdown."""
    now = now or datetime.now(timezone.utc)
    start = starting_balance(c)
    curve = c.execute("SELECT ts, equity FROM equity ORDER BY ts").fetchall()
    equities = [start, *[r["equity"] for r in curve]]
    today = now.date().isoformat()
    before_today = [r["equity"] for r in curve if r["ts"][:10] < today]
    day_start = before_today[-1] if before_today else start
    closed = c.execute("SELECT pnl FROM positions WHERE status = 'CLOSED'").fetchall()
    wins = sum(1 for r in closed if r["pnl"] > 0)
    return {
        "starting_balance": start,
        "cash": cash(c),
        "equity": equities[-1],
        "pnl_total": equities[-1] - start,
        "pnl_today": equities[-1] - day_start,
        "open_positions": c.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'").fetchone()[0],
        "closed_positions": len(closed),
        "wins": wins,
        "win_rate": wins / len(closed) if closed else None,
        "max_drawdown": max_drawdown(equities),
        "fees_paid": c.execute("SELECT COALESCE(SUM(fees), 0) FROM fills").fetchone()[0],
        "trades_today": c.execute(
            "SELECT COUNT(*) FROM fills WHERE action != 'SETTLE' AND ts >= ?", (today,)
        ).fetchone()[0],
        "pnl_history": [round(e - start, 2) for e in equities[1:]][-HISTORY_POINTS:],
    }


def recent_fills(c: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    """The newest fills, oldest first."""
    rows = c.execute("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return rows[::-1]


def start_run(c: sqlite3.Connection, now: datetime | None = None) -> int:
    cur = c.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (_stamp(now),))
    c.commit()
    return int(cur.lastrowid)


def finish_run(
    c: sqlite3.Connection, run_id: int, status: str, summary: str | None = None, error: str | None = None
) -> None:
    c.execute(
        "UPDATE runs SET finished_at = ?, status = ?, summary = ?, error = ? WHERE id = ?",
        (_stamp(), status, summary, error, run_id),
    )
    c.commit()


def last_run(c: sqlite3.Connection) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1").fetchone()
