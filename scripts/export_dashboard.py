"""Export PolyBot state from polybot.db to docs/data.json for the static dashboard.

Usage:  python scripts/export_dashboard.py [--out docs/data.json]

Reads the database read-only and writes only aggregate paper-trading data
(no API keys, no raw market JSON), so the output is safe to publish.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polybot.agent import export_state as agent_state  # noqa: E402
from polybot.calibration import HORIZONS_DAYS, report  # noqa: E402
from polybot.db import db_path  # noqa: E402


def _rows(c: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def open_positions(c: sqlite3.Connection) -> list[dict]:
    out = []
    for p in _rows(c, "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at DESC"):
        snap = c.execute(
            "SELECT yes_price, no_price, captured_at FROM snapshots "
            "WHERE market_id = ? ORDER BY id DESC LIMIT 1", (p["market_id"],)
        ).fetchone()
        mark = None
        if snap:
            yes, no = snap["yes_price"], snap["no_price"]
            if p["side"] == "YES":
                mark = yes
            else:
                mark = no if no is not None else (1 - yes if yes is not None else None)
        value = p["shares"] * mark if mark is not None else None
        out.append({
            "market": p["question"], "side": p["side"], "entry_price": p["entry_price"],
            "current_price": mark, "shares": p["shares"], "stake": p["stake"],
            "unrealized_pnl": value - p["stake"] if value is not None else None,
            "opened_at": p["opened_at"], "reason": p["opened_reason"],
        })
    return out


def resolved(c: sqlite3.Connection) -> list[dict]:
    """Resolved markets, with the paper trade (if any) and whether it was correct."""
    out = []
    for r in _rows(c, """
        SELECT r.market_id, r.outcome, r.resolved_at, m.question
        FROM resolutions r JOIN markets m ON m.id = r.market_id
        WHERE r.outcome IS NOT NULL
        ORDER BY r.resolved_at DESC LIMIT 25"""):
        pos = c.execute(
            "SELECT side, entry_price, pnl FROM positions WHERE market_id = ? "
            "ORDER BY id DESC LIMIT 1", (r["market_id"],)).fetchone()
        correct = None
        if pos and r["outcome"] in ("YES", "NO"):
            correct = pos["side"] == r["outcome"]
        out.append({
            "market": r["question"], "outcome": r["outcome"], "resolved_at": r["resolved_at"],
            "side": pos["side"] if pos else None,
            "entry_price": pos["entry_price"] if pos else None,
            "pnl": pos["pnl"] if pos else None, "correct": correct,
        })
    return out


def last_run(c: sqlite3.Connection) -> dict:
    """Last pipeline run: the newest block in pipeline.log, else the newest snapshot."""
    log = ROOT / "pipeline.log"
    if log.exists():
        blocks = re.split(r"(?m)^=== PolyBot pipeline . ", log.read_text(encoding="utf-8", errors="replace"))
        if len(blocks) > 1:
            last = blocks[-1]
            ts = last.split(" ===", 1)[0].strip()
            ok = "Pipeline complete" in last and not re.search(r"Traceback|Error", last)
            return {"at": ts, "status": "success" if ok else "failed"}
    row = c.execute("SELECT MAX(captured_at) FROM snapshots").fetchone()
    return {"at": row[0], "status": "unknown"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "data.json"))
    args = ap.parse_args()

    c = sqlite3.connect(f"file:{db_path().as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    opens = open_positions(c)
    bankroll = c.execute(
        "SELECT balance_after FROM bankroll_events ORDER BY id DESC LIMIT 1").fetchone()
    initial = c.execute(
        "SELECT delta FROM bankroll_events WHERE reason = 'INITIAL_DEPOSIT' LIMIT 1").fetchone()
    cash = bankroll[0] if bankroll else 0.0
    realized = c.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status != 'OPEN'").fetchone()[0]
    unrealized = sum(p["unrealized_pnl"] or 0 for p in opens)
    market_value = sum(p["stake"] + (p["unrealized_pnl"] or 0) for p in opens)
    closed = _rows(c, "SELECT status FROM positions WHERE status != 'OPEN'")
    wins = sum(1 for p in closed if p["status"] == "CLOSED_WIN")

    summary = {
        "cash": cash, "equity": cash + market_value,
        "starting_bankroll": initial[0] if initial else None,
        "realized_pnl": realized, "unrealized_pnl": unrealized,
        "open_count": len(opens), "closed_count": len(closed), "wins": wins,
        "markets_tracked": c.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
        "snapshots": c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
    }
    equity_curve = _rows(c, "SELECT at, balance_after FROM bankroll_events ORDER BY id")
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
        "equity_curve": equity_curve,
        "open_positions": opens,
        "resolved": resolved(c),
        "last_run": last_run(c),
        "agent": agent_state(c),
    }
    c.close()
    data["calibration"] = {str(h): report(h) for h in HORIZONS_DAYS}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({len(opens)} open, {len(data['resolved'])} resolved)")


if __name__ == "__main__":
    main()
