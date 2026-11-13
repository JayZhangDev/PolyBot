"""Export PolyBot state from polybot.db to docs/data.json for the static dashboard.

Usage:  python scripts/export_dashboard.py [--out docs/data.json] [--pnl-out paper_pnl.txt]

Reads the database read-only and writes only aggregate paper-trading data
(no API keys, no raw market JSON), so the output is safe to publish. Also writes
paper_pnl.txt (git-ignored): the v2 agent's paper P&L as one number, for a local
desktop wallpaper to read.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PNL_FILE = ROOT / "paper_pnl.txt"
sys.path.insert(0, str(ROOT))

from polybot.agent import export_state as agent_state  # noqa: E402
from polybot.calibration import HORIZONS_DAYS, report  # noqa: E402
from polybot.db import db_path  # noqa: E402
from polybot.jobs import LockTimeout, job_lock  # noqa: E402
from polybot.logsetup import setup_logging  # noqa: E402

LOCK_WAIT_SECONDS = 120
log = logging.getLogger("polybot.export")


def _rows(c: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def open_positions(c: sqlite3.Connection) -> list[dict]:
    out = []
    for p in _rows(c, "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at DESC"):
        snap = c.execute(
            "SELECT yes_price, no_price, captured_at FROM snapshots WHERE market_id = ? ORDER BY id DESC LIMIT 1",
            (p["market_id"],),
        ).fetchone()
        mark = None
        if snap:
            yes, no = snap["yes_price"], snap["no_price"]
            if p["side"] == "YES":
                mark = yes
            else:
                mark = no if no is not None else (1 - yes if yes is not None else None)
        value = p["shares"] * mark if mark is not None else None
        out.append(
            {
                "market": p["question"],
                "side": p["side"],
                "entry_price": p["entry_price"],
                "current_price": mark,
                "shares": p["shares"],
                "stake": p["stake"],
                "unrealized_pnl": value - p["stake"] if value is not None else None,
                "opened_at": p["opened_at"],
                "reason": p["opened_reason"],
            }
        )
    return out


def resolved(c: sqlite3.Connection) -> list[dict]:
    """Resolved markets, with the paper trade (if any) and whether it was correct."""
    out = []
    for r in _rows(
        c,
        """
        SELECT r.market_id, r.outcome, r.resolved_at, m.question
        FROM resolutions r JOIN markets m ON m.id = r.market_id
        WHERE r.outcome IS NOT NULL
        ORDER BY r.resolved_at DESC LIMIT 25""",
    ):
        pos = c.execute(
            "SELECT side, entry_price, pnl FROM positions WHERE market_id = ? ORDER BY id DESC LIMIT 1",
            (r["market_id"],),
        ).fetchone()
        correct = None
        if pos and r["outcome"] in ("YES", "NO"):
            correct = pos["side"] == r["outcome"]
        out.append(
            {
                "market": r["question"],
                "outcome": r["outcome"],
                "resolved_at": r["resolved_at"],
                "side": pos["side"] if pos else None,
                "entry_price": pos["entry_price"] if pos else None,
                "pnl": pos["pnl"] if pos else None,
                "correct": correct,
            }
        )
    return out


def last_run(c: sqlite3.Connection) -> dict:
    """Last finished pipeline run, from the job_runs table; else the newest snapshot."""
    has_runs = c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'job_runs'").fetchone()
    if has_runs:
        row = c.execute(
            "SELECT started_at, status FROM job_runs "
            "WHERE job = 'pipeline' AND status != 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return {"at": row["started_at"], "status": row["status"]}
    row = c.execute("SELECT MAX(captured_at) FROM snapshots").fetchone()
    return {"at": row[0], "status": "unknown"}


def write_paper_pnl(agent: dict, path: Path = PNL_FILE) -> float | None:
    """Write the v2 agent's paper P&L (equity minus its starting bankroll) as one number on one line.

    Written to a temp file and renamed, so a reader never sees half a number. Nothing is
    written until the agent has run once.
    """
    summary = agent.get("summary")
    if not summary:
        return None
    pnl = round(summary["equity"] - summary["starting_bankroll"], 2) + 0.0  # + 0.0 turns -0.0 into 0.0
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{pnl:.2f}\n", encoding="utf-8")
    os.replace(tmp, path)
    return pnl


def main() -> int:
    """Export under the job lock, so the files are never built from a half-written database."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "data.json"))
    ap.add_argument("--pnl-out", default=str(PNL_FILE))
    args = ap.parse_args()
    setup_logging()
    try:
        with job_lock("export", LOCK_WAIT_SECONDS):
            export(Path(args.out), Path(args.pnl_out))
    except LockTimeout as exc:
        log.warning("Dashboard export skipped: %s", exc)
    except Exception:
        log.exception("Dashboard export failed")
        return 1
    return 0


def export(out: Path, pnl_out: Path) -> None:
    """Read the database (read-only) and write docs/data.json and paper_pnl.txt."""
    c = sqlite3.connect(f"file:{db_path().as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    opens = open_positions(c)
    bankroll = c.execute("SELECT balance_after FROM bankroll_events ORDER BY id DESC LIMIT 1").fetchone()
    initial = c.execute("SELECT delta FROM bankroll_events WHERE reason = 'INITIAL_DEPOSIT' LIMIT 1").fetchone()
    cash = bankroll[0] if bankroll else 0.0
    realized = c.execute("SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status != 'OPEN'").fetchone()[0]
    unrealized = sum(p["unrealized_pnl"] or 0 for p in opens)
    market_value = sum(p["stake"] + (p["unrealized_pnl"] or 0) for p in opens)
    closed = _rows(c, "SELECT status FROM positions WHERE status != 'OPEN'")
    wins = sum(1 for p in closed if p["status"] == "CLOSED_WIN")

    summary = {
        "cash": cash,
        "equity": cash + market_value,
        "starting_bankroll": initial[0] if initial else None,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "open_count": len(opens),
        "closed_count": len(closed),
        "wins": wins,
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

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d open, %d resolved)", out, len(opens), len(data["resolved"]))
    pnl = write_paper_pnl(data["agent"], pnl_out)
    if pnl is not None:
        log.info("Wrote %s (v2 agent paper P&L %+.2f)", pnl_out, pnl)


if __name__ == "__main__":
    raise SystemExit(main())
