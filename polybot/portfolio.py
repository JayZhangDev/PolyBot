"""Bankroll + position sizing for paper-trader."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("polybot.db")

STARTING_BANKROLL = 1000.0  # paper USDC
BET_FRACTION = 0.01          # 2% of bankroll per bet
MAX_OPEN_POSITIONS = 10


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_portfolio_tables() -> None:
    with _connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            side TEXT NOT NULL,               -- 'YES' or 'NO'
            entry_price REAL NOT NULL,
            shares REAL NOT NULL,             -- how many binary shares we hold
            stake REAL NOT NULL,              -- $ paid: shares * entry_price
            opened_at TEXT NOT NULL,
            opened_reason TEXT,
            closed_at TEXT,
            close_price REAL,
            pnl REAL,
            close_reason TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN'  -- OPEN, CLOSED_WIN, CLOSED_LOSS, CLOSED_MANUAL
        );

        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);

        CREATE TABLE IF NOT EXISTS bankroll_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            delta REAL NOT NULL,
            balance_after REAL NOT NULL,
            reason TEXT
        );
        """)
        # Seed starting bankroll if empty
        row = c.execute("SELECT COUNT(*) FROM bankroll_events").fetchone()
        if row[0] == 0:
            c.execute(
                "INSERT INTO bankroll_events (at, delta, balance_after, reason) "
                "VALUES (datetime('now'), ?, ?, 'INITIAL_DEPOSIT')",
                (STARTING_BANKROLL, STARTING_BANKROLL),
            )


def get_bankroll() -> float:
    init_portfolio_tables()
    with _connect() as c:
        row = c.execute(
            "SELECT balance_after FROM bankroll_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return float(row["balance_after"]) if row else 0.0


def get_open_positions() -> list[sqlite3.Row]:
    init_portfolio_tables()
    with _connect() as c:
        return c.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at DESC"
        ).fetchall()


def open_market_ids() -> set[str]:
    return {p["market_id"] for p in get_open_positions()}


def can_open_new_position() -> bool:
    return len(get_open_positions()) < MAX_OPEN_POSITIONS


def calculate_stake(bankroll: float) -> float:
    return round(bankroll * BET_FRACTION, 2)


def record_bankroll_change(delta: float, reason: str) -> float:
    with _connect() as c:
        row = c.execute(
            "SELECT balance_after FROM bankroll_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        current = float(row["balance_after"]) if row else 0.0
        new_balance = current + delta
        c.execute(
            "INSERT INTO bankroll_events (at, delta, balance_after, reason) "
            "VALUES (datetime('now'), ?, ?, ?)",
            (delta, new_balance, reason),
        )
    return new_balance


def summary() -> dict:
    init_portfolio_tables()
    with _connect() as c:
        n_open = c.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'").fetchone()[0]
        n_closed = c.execute("SELECT COUNT(*) FROM positions WHERE status != 'OPEN'").fetchone()[0]
        n_wins = c.execute("SELECT COUNT(*) FROM positions WHERE status = 'CLOSED_WIN'").fetchone()[0]
        n_loss = c.execute("SELECT COUNT(*) FROM positions WHERE status = 'CLOSED_LOSS'").fetchone()[0]
        total_pnl = c.execute("SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status != 'OPEN'").fetchone()[0] or 0.0
    return {
        "bankroll": get_bankroll(),
        "open_positions": n_open,
        "closed_positions": n_closed,
        "wins": n_wins,
        "losses": n_loss,
        "realized_pnl": round(total_pnl, 2),
        "win_rate": (n_wins / n_closed) if n_closed else 0.0,
    }


if __name__ == "__main__":
    init_portfolio_tables()
    s = summary()
    print(f"\nBankroll:        ${s['bankroll']:.2f}")
    print(f"Open positions:  {s['open_positions']}")
    print(f"Closed:          {s['closed_positions']}  (W: {s['wins']}, L: {s['losses']})")
    print(f"Realized P&L:    ${s['realized_pnl']:.2f}")
    print(f"Win rate:        {s['win_rate']*100:.1f}%")
