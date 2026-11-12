"""Hourly cycle: snapshot Polymarket, refresh held markets, record resolutions, paper trade."""

from __future__ import annotations

import logging

from polybot.fetch import fetch_active_markets, fetch_markets_by_id
from polybot.jobs import run_job
from polybot.paper_trader import run as run_trader
from polybot.portfolio import open_market_ids
from polybot.resolutions import record_resolutions, sweep
from polybot.storage import counts, save_snapshot

ACTIVE_MARKET_LIMIT = 500
LOCK_WAIT_SECONDS = 600  # the agent's cycle takes a few minutes; wait for it instead of skipping the hour

log = logging.getLogger("polybot.pipeline")


def refresh_held_markets(fetched_ids: set[str]) -> int:
    """Snapshot every market we hold a position in that the active fetch missed.

    Closed and resolved markets are not in the active listing, so without this
    step a position's market would never be seen resolving and the position
    would stay open forever.
    """
    missing = sorted(open_market_ids() - fetched_ids)
    if not missing:
        return 0
    markets = fetch_markets_by_id(missing)
    record_resolutions(markets, checked_ids=missing)
    return save_snapshot(markets)


def run_once() -> str:
    """One full cycle. Returns a one-line summary for the log and the job_runs table."""
    markets = fetch_active_markets(limit=ACTIVE_MARKET_LIMIT)
    saved = save_snapshot(markets)
    refreshed = refresh_held_markets({str(m.get("id")) for m in markets})
    stats = counts()
    log.info(
        "Saved %d snapshots (+%d held markets refreshed). DB totals: %d markets, %d snapshots.",
        saved,
        refreshed,
        stats["markets"],
        stats["snapshots"],
    )

    swept = sweep()
    log.info("Checked %d ended or dropped markets, %d newly resolved.", swept["checked"], swept["resolved"])

    result = run_trader()
    s = result["summary"]
    log.info(
        "Paper trader: opened %d, closed %d. Bankroll $%.2f, %d open, realized P&L $%.2f.",
        result["opened"],
        result["closed"],
        s["bankroll"],
        s["open_positions"],
        s["realized_pnl"],
    )
    return (
        f"{saved + refreshed} snapshots, {swept['resolved']} newly resolved, "
        f"opened {result['opened']}, closed {result['closed']}, {s['open_positions']} positions open"
    )


def main() -> int:
    """Entry point for the hourly scheduled task. Returns the process exit code."""
    return run_job("pipeline", run_once, wait_seconds=LOCK_WAIT_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
