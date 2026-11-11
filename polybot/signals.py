"""Signal generation for PolyBot.

Each signal function returns candidate trades with a `strategy` field naming the
signal that produced it and an `entry_price` the paper trader fills at. The
combined entry point deduplicates on market_id so no market is bet twice in one
cycle.

Traded signals:
  - longshot : fade overpriced longshots by buying NO when YES <= 0.15
  - momentum : follow markets whose YES price moved 10+ points in ~24h

Watchlist only (not traded yet, see `mispricing_candidates`):
  - odds_mismatch : neg-risk events whose outcome prices don't sum to 1
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from polybot.db import connect
from polybot.pricing import entry_price

# Ignore markets whose newest snapshot is older than this. Markets that drop
# out of the fetched set would otherwise be traded on hours-old prices.
MAX_SNAPSHOT_AGE_HOURS = 3


def _latest_snapshots() -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_SNAPSHOT_AGE_HOURS)).isoformat()
    with connect() as c:
        return c.execute("""
            SELECT m.id AS market_id, m.question, m.end_date, m.category, m.slug,
                   m.event_id, m.neg_risk,
                   s.yes_price, s.no_price, s.best_bid, s.best_ask, s.volume, s.captured_at
            FROM markets m
            JOIN snapshots s ON s.market_id = m.id
            WHERE s.id = (SELECT MAX(id) FROM snapshots WHERE market_id = m.id)
              AND s.captured_at >= ?
        """, (cutoff,)).fetchall()


def _parse_end(end_str: str | None) -> datetime | None:
    if not end_str:
        return None
    try:
        return datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _valid_time_window(end_str: str | None, min_days: int, max_days: int) -> bool:
    end_dt = _parse_end(end_str)
    if not end_dt:
        return False
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=min_days)) <= end_dt <= (now + timedelta(days=max_days))


def _signal(row, strategy: str, side: str, reason: str) -> dict | None:
    price = entry_price(side, row["best_bid"], row["best_ask"])
    if price is None:
        return None
    return {
        "strategy": strategy,
        "market_id": row["market_id"],
        "question": row["question"],
        "yes_price": row["yes_price"],
        "no_price": row["no_price"],
        "volume": row["volume"],
        "end_date": row["end_date"],
        "side": side,
        "entry_price": price,
        "reason": reason,
    }


# ============================================================================
# SIGNAL 1 — Longshot fade
# ============================================================================
# The favorite-longshot bias: low-probability outcomes tend to trade above
# their true win rate. If the bias exists here, the edge is selling the
# longshot, i.e. buying NO.

LONGSHOT_MAX_YES = 0.15
LONGSHOT_MIN_VOL = 500_000
LONGSHOT_MIN_DAYS = 7
LONGSHOT_MAX_DAYS = 1200


def longshot_signals(exclude: set[str]) -> list[dict]:
    signals = []
    for r in _latest_snapshots():
        if r["market_id"] in exclude:
            continue
        yes, vol = r["yes_price"], r["volume"]
        if yes is None or vol is None:
            continue
        if not (0.01 < yes <= LONGSHOT_MAX_YES):
            continue
        if vol < LONGSHOT_MIN_VOL:
            continue
        if not _valid_time_window(r["end_date"], LONGSHOT_MIN_DAYS, LONGSHOT_MAX_DAYS):
            continue
        sig = _signal(r, "longshot", "NO",
                      f"Longshot fade: YES={yes:.3f}, vol=${vol:,.0f}, buying NO")
        if sig:
            signals.append(sig)
    return signals


# ============================================================================
# SIGNAL 2 — 24-hour momentum
# ============================================================================

MOMENTUM_MIN_MOVE = 0.10   # 10 percentage points
MOMENTUM_MIN_VOL = 500_000
MOMENTUM_MIN_DAYS = 3
MOMENTUM_MAX_DAYS = 1200
MOMENTUM_MIN_YES = 0.15    # skip markets already near 0 or 1
MOMENTUM_MAX_YES = 0.85


def momentum_signals(exclude: set[str]) -> list[dict]:
    """Compare each market's latest snapshot to its newest snapshot >=20h older."""
    signals = []
    with connect() as c:
        for r in _latest_snapshots():
            if r["market_id"] in exclude or r["yes_price"] is None:
                continue
            prior = c.execute("""
                SELECT yes_price FROM snapshots
                WHERE market_id = ? AND datetime(captured_at) <= datetime(?, '-20 hours')
                ORDER BY id DESC LIMIT 1
            """, (r["market_id"], r["captured_at"])).fetchone()
            if not prior or prior["yes_price"] is None:
                continue

            move = r["yes_price"] - prior["yes_price"]
            if abs(move) < MOMENTUM_MIN_MOVE:
                continue
            if not (MOMENTUM_MIN_YES <= r["yes_price"] <= MOMENTUM_MAX_YES):
                continue
            if not (r["volume"] and r["volume"] >= MOMENTUM_MIN_VOL):
                continue
            if not _valid_time_window(r["end_date"], MOMENTUM_MIN_DAYS, MOMENTUM_MAX_DAYS):
                continue

            side = "YES" if move > 0 else "NO"
            sig = _signal(r, "momentum", side, f"Momentum: 24h move {move:+.1%}, entering {side}")
            if sig:
                signals.append(sig)
    return signals


# ============================================================================
# WATCHLIST — Neg-risk odds mismatch (not traded)
# ============================================================================
# In a neg-risk event exactly one market resolves YES, so the YES prices should
# sum to about 1. A real arbitrage trades every leg at executable prices:
#   - buy YES on every leg:  cost = sum(ask_i),     payout = 1
#   - buy NO  on every leg:  cost = sum(1 - bid_i), payout = N - 1
# Betting a single leg (what v0.1 did) is a directional bet, not an arb.
# We only snapshot the top markets by volume, so an event's legs may be
# incomplete; candidates are a watchlist until v0.4 fetches whole events.

MISMATCH_MIN_LEGS = 3
MISMATCH_MIN_EDGE = 0.01


def mispricing_candidates() -> list[dict]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in _latest_snapshots():
        if r["event_id"] and r["neg_risk"] and r["yes_price"] is not None:
            groups.setdefault(r["event_id"], []).append(r)

    out = []
    for event_id, legs in groups.items():
        if len(legs) < MISMATCH_MIN_LEGS:
            continue
        n = len(legs)
        yes_cost = [entry_price("YES", r["best_bid"], r["best_ask"]) for r in legs]
        no_cost = [entry_price("NO", r["best_bid"], r["best_ask"]) for r in legs]
        mid_sum = sum(r["yes_price"] for r in legs)
        yes_edge = 1.0 - sum(yes_cost) if None not in yes_cost else None
        no_edge = (n - 1) - sum(no_cost) if None not in no_cost else None
        best = max((e for e in (yes_edge, no_edge) if e is not None), default=None)
        if best is None or best < MISMATCH_MIN_EDGE:
            continue
        out.append({
            "event_id": event_id,
            "legs_seen": n,
            "mid_sum": round(mid_sum, 4),
            "yes_basket_edge": round(yes_edge, 4) if yes_edge is not None else None,
            "no_basket_edge": round(no_edge, 4) if no_edge is not None else None,
            "example_question": legs[0]["question"],
        })
    return sorted(out, key=lambda d: -max(d["yes_basket_edge"] or 0, d["no_basket_edge"] or 0))


# ============================================================================
# COMBINED ENTRY POINT
# ============================================================================

STRATEGY_PRIORITY = ("momentum", "longshot")


def find_signals(exclude_market_ids: set[str] | None = None) -> list[dict]:
    """Run all traded signal families, deduped by market_id, in priority order."""
    exclude = exclude_market_ids or set()
    all_sigs: list[dict] = []
    seen: set[str] = set()
    for finder in (momentum_signals, longshot_signals):
        for s in finder(exclude):
            if s["market_id"] in seen:
                continue
            seen.add(s["market_id"])
            all_sigs.append(s)
    return all_sigs


if __name__ == "__main__":
    sigs = find_signals()
    print(f"\nFound {len(sigs)} signals:\n")
    for s in sigs:
        print(f"  [{s['strategy']:>9}]  [{s['side']} @ {s['entry_price']:.3f}]  vol=${s['volume']:>12,.0f}")
        print(f"               {s['question'][:80]}")
        print(f"               {s['reason']}\n")
    cands = mispricing_candidates()
    print(f"{len(cands)} neg-risk mispricing candidates (watchlist, not traded)")
    for c in cands[:10]:
        print(f"  event {c['event_id']}: {c['legs_seen']} legs, mid sum {c['mid_sum']}, "
              f"YES-basket edge {c['yes_basket_edge']}, NO-basket edge {c['no_basket_edge']}")
