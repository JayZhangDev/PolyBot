"""Hourly cycle: snapshot Polymarket, refresh held markets, record resolutions, paper trade."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from polybot.fetch import fetch_active_markets, fetch_markets_by_id
from polybot.paper_trader import run as run_trader
from polybot.portfolio import open_market_ids
from polybot.resolutions import record_resolutions, sweep
from polybot.storage import counts, save_snapshot

ACTIVE_MARKET_LIMIT = 500


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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"\n=== PolyBot pipeline — {datetime.now(timezone.utc).isoformat()} ===\n")

    print("→ Fetching Polymarket markets...")
    markets = fetch_active_markets(limit=ACTIVE_MARKET_LIMIT)
    saved = save_snapshot(markets)
    refreshed = refresh_held_markets({str(m.get("id")) for m in markets})
    stats = counts()
    print(f"  Saved {saved} snapshots (+{refreshed} held markets refreshed). "
          f"DB totals: {stats['markets']} markets, {stats['snapshots']} snapshots.")

    print("\n→ Checking for resolved markets...")
    swept = sweep()
    print(f"  Checked {swept['checked']} ended or dropped markets, {swept['resolved']} newly resolved.")

    print("\n→ Running paper-trader...")
    result = run_trader()
    s = result["summary"]
    print(f"  Opened: {result['opened']}   Closed: {result['closed']}")
    print(f"  Bankroll: ${s['bankroll']:.2f}   Open: {s['open_positions']}   "
          f"Realized P&L: ${s['realized_pnl']:.2f}")
    print("\n=== Pipeline complete ===\n")


if __name__ == "__main__":
    main()
