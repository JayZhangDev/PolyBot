"""Signal generation for PolyBot.

Each signal function returns a list of candidate trades with a `strategy` field
identifying which signal created it. Paper trader deduplicates on market_id
so the same market never gets bet twice in one cycle.

Current signals:
  - longshot_favorite_bias : buy YES on cheap markets ($500k+ vol, YES<0.15)
  - momentum_24h          : buy YES on markets whose price rose >10% in the last 24h
  - odds_mismatch          : find related-outcome markets whose YES prices don't sum to 1
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path("polybot.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_snapshots() -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute("""
            SELECT m.id AS market_id, m.question, m.end_date, m.category, m.slug,
                   s.yes_price, s.no_price, s.volume, s.captured_at
            FROM markets m
            JOIN snapshots s ON s.market_id = m.id
            WHERE s.id = (
                SELECT MAX(id) FROM snapshots WHERE market_id = m.id
            )
        """).fetchall()


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


# ============================================================================
# SIGNAL 1 — Longshot favorite bias
# ============================================================================

LONGSHOT_MAX_YES = 0.15
LONGSHOT_MIN_VOL = 500_000
LONGSHOT_MIN_DAYS = 7
LONGSHOT_MAX_DAYS = 1200


def longshot_signals(exclude: set[str]) -> list[dict]:
    signals = []
    for r in _latest_snapshots():
        if r["market_id"] in exclude:
            continue
        yes = r["yes_price"]
        vol = r["volume"]
        if yes is None or vol is None:
            continue
        if not (0.01 < yes <= LONGSHOT_MAX_YES):
            continue
        if vol < LONGSHOT_MIN_VOL:
            continue
        if not _valid_time_window(r["end_date"], LONGSHOT_MIN_DAYS, LONGSHOT_MAX_DAYS):
            continue
        signals.append({
            "strategy": "longshot",
            "market_id": r["market_id"],
            "question": r["question"],
            "yes_price": yes,
            "no_price": r["no_price"],
            "volume": vol,
            "end_date": r["end_date"],
            "side": "YES",
            "reason": f"Longshot: YES={yes:.3f}, vol=${vol:,.0f}",
        })
    return signals


# ============================================================================
# SIGNAL 2 — 24-hour momentum
# ============================================================================

MOMENTUM_MIN_MOVE = 0.10   # 10% price move
MOMENTUM_MIN_VOL = 500_000
MOMENTUM_MIN_DAYS = 3
MOMENTUM_MAX_DAYS = 1200
MOMENTUM_MIN_YES = 0.15    # skip already-tiny or already-huge markets
MOMENTUM_MAX_YES = 0.85


def momentum_signals(exclude: set[str]) -> list[dict]:
    """Compare each market's latest snapshot to its snapshot ~24h ago."""
    signals = []
    with _connect() as c:
        # For each market, get its latest snapshot and one from >=20h ago
        rows = c.execute("""
            SELECT m.id AS market_id, m.question, m.end_date, m.category
            FROM markets m
        """).fetchall()

    with _connect() as c:
        for m in rows:
            mid = m["market_id"]
            if mid in exclude:
                continue
            latest = c.execute("""
                SELECT yes_price, no_price, volume, captured_at
                FROM snapshots WHERE market_id = ?
                ORDER BY id DESC LIMIT 1
            """, (mid,)).fetchone()
            if not latest or latest["yes_price"] is None:
                continue

            # Look for a snapshot at least 20 hours older than latest
            prior = c.execute("""
                SELECT yes_price, captured_at
                FROM snapshots
                WHERE market_id = ?
                  AND datetime(captured_at) <= datetime(?, '-20 hours')
                ORDER BY id DESC LIMIT 1
            """, (mid, latest["captured_at"])).fetchone()
            if not prior or prior["yes_price"] is None:
                continue

            move = latest["yes_price"] - prior["yes_price"]
            if abs(move) < MOMENTUM_MIN_MOVE:
                continue
            if not (MOMENTUM_MIN_YES <= latest["yes_price"] <= MOMENTUM_MAX_YES):
                continue
            if not (latest["volume"] and latest["volume"] >= MOMENTUM_MIN_VOL):
                continue
            if not _valid_time_window(m["end_date"], MOMENTUM_MIN_DAYS, MOMENTUM_MAX_DAYS):
                continue

            side = "YES" if move > 0 else "NO"
            entry = latest["yes_price"] if side == "YES" else latest["no_price"]
            if entry is None:
                continue
            signals.append({
                "strategy": "momentum",
                "market_id": mid,
                "question": m["question"],
                "yes_price": latest["yes_price"],
                "no_price": latest["no_price"],
                "volume": latest["volume"],
                "end_date": m["end_date"],
                "side": side,
                "reason": f"Momentum: 24h move {move:+.1%}, entering {side}",
            })
    return signals


# ============================================================================
# SIGNAL 3 — Multi-outcome odds mismatch
# ============================================================================

MISMATCH_MIN_DEVIATION = 0.03   # markets summing to >1.03 or <0.97
MISMATCH_MIN_OUTCOMES = 3       # need at least 3 related markets to be worth attention
MISMATCH_MIN_VOL_EACH = 200_000
MISMATCH_MIN_DAYS = 7
MISMATCH_MAX_DAYS = 1200


def _group_key(question: str) -> str:
    """Cheap heuristic grouping: strip the candidate/name portion.
    'Will X win the 2028 Dem nom?' → '2028 dem nom' (roughly).
    Not perfect but good enough for common Polymarket patterns.
    """
    q = question.lower()
    # Strip the "Will X " prefix
    if q.startswith("will "):
        rest = q[5:]
        # Look for keywords that come AFTER the candidate name
        for anchor in [" win ", " be ", " become "]:
            idx = rest.find(anchor)
            if idx > 0:
                return rest[idx:].strip("?. ").strip()
    return q[:60]


def odds_mismatch_signals(exclude: set[str]) -> list[dict]:
    """Find groups of related markets whose YES prices don't sum to ~1.0."""
    rows = _latest_snapshots()
    # Group by heuristic key
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if r["yes_price"] is None or r["volume"] is None:
            continue
        if r["volume"] < MISMATCH_MIN_VOL_EACH:
            continue
        if not _valid_time_window(r["end_date"], MISMATCH_MIN_DAYS, MISMATCH_MAX_DAYS):
            continue
        key = _group_key(r["question"])
        groups.setdefault(key, []).append(r)

    signals = []
    for key, group in groups.items():
        # Sub-group by end_date so we don't merge unrelated duplicate listings
        by_end: dict[str, list] = {}
        for r in group:
            by_end.setdefault(r["end_date"] or "", []).append(r)
        for end_date, sub in by_end.items():
            if len(sub) < MISMATCH_MIN_OUTCOMES:
                continue
            total = sum(r["yes_price"] for r in sub)
            deviation = total - 1.0
            if abs(deviation) < MISMATCH_MIN_DEVIATION:
                continue
            if abs(deviation) > 0.5:
                continue

            if deviation > 0:
                target = max(sub, key=lambda r: r["yes_price"])
                side = "NO"
                entry = target["no_price"]
                reason = f"Group sums to {total:.3f} (>1); shorting priciest at YES={target['yes_price']:.3f}"
            else:
                target = min(sub, key=lambda r: r["yes_price"])
                side = "YES"
                entry = target["yes_price"]
                reason = f"Group sums to {total:.3f} (<1); longing cheapest at YES={target['yes_price']:.3f}"

            if target["market_id"] in exclude:
                continue
            if entry is None or entry <= 0.005:
                continue
            if target["yes_price"] < 0.005 or target["yes_price"] > 0.995:
                continue
            signals.append({
                "strategy": "odds_mismatch",
                "market_id": target["market_id"],
                "question": target["question"],
                "yes_price": target["yes_price"],
                "no_price": target["no_price"],
                "volume": target["volume"],
                "end_date": target["end_date"],
                "side": side,
                "reason": reason,
            })
    return signals


# ============================================================================
# COMBINED ENTRY POINT
# ============================================================================

def find_signals(exclude_market_ids: set[str] | None = None) -> list[dict]:
    """Run all signal families and return combined results, deduped by market_id."""
    exclude = exclude_market_ids or set()
    all_sigs = []
    seen: set[str] = set()

    for finder in (odds_mismatch_signals, momentum_signals, longshot_signals):
        for s in finder(exclude):
            if s["market_id"] in seen:
                continue
            seen.add(s["market_id"])
            all_sigs.append(s)

    return all_sigs


if __name__ == "__main__":
    sigs = find_signals()
    print(f"\nFound {len(sigs)} signals across all strategies:\n")
    for s in sigs:
        print(f"  [{s['strategy']:>14}]  [{s['side']} @ {s['yes_price'] if s['side']=='YES' else s['no_price']:.3f}]  vol=${s['volume']:>12,.0f}")
        print(f"                    {s['question'][:80]}")
        print(f"                    {s['reason']}\n")
