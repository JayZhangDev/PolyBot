"""Hourly cycle: fetch fresh Polymarket data, then run paper-trader."""
from __future__ import annotations

from datetime import datetime, timezone

from polybot.fetch import fetch_active_markets
from polybot.paper_trader import run as run_trader
from polybot.storage import counts, save_snapshot


def main() -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    print(f"\n=== PolyBot pipeline — {stamp} ===\n")

    print("→ Fetching Polymarket markets...")
    markets = fetch_active_markets(limit=500)
    saved = save_snapshot(markets)
    stats = counts()
    print(f"  Saved {saved} snapshots. DB totals: {stats['markets']} markets, {stats['snapshots']} snapshots.")

    print("\n→ Running paper-trader...")
    result = run_trader()
    s = result["summary"]
    print(f"  Opened: {result['opened']}   Closed: {result['closed']}")
    print(f"  Bankroll: ${s['bankroll']:.2f}   Open: {s['open_positions']}   Realized P&L: ${s['realized_pnl']:.2f}")
    print(f"\n=== Pipeline complete ===\n")


if __name__ == "__main__":
    main()
