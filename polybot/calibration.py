"""How well do Polymarket prices predict outcomes?

For every resolved market, take the YES price PolyBot recorded some horizon
(1, 7 or 30 days) before the market resolved, and compare it with what happened.

Metrics:
  - Brier score: mean squared error of price vs outcome (0 is perfect;
    always saying 0.5 scores 0.25).
  - Brier skill score: improvement over always predicting the base rate
    (1 is perfect, 0 is no better than the base rate, negative is worse).
  - Reliability bins: within each price bucket, the average price vs how often
    YES actually happened. A calibrated market sits on the diagonal.

"Resolution time" is the earlier of the market's close time and the first
snapshot where its price was already pinned to its final value, so a market
that was effectively decided early isn't scored on post-decision prices.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timedelta

from polybot.db import connect
from polybot.storage import init_db
from polybot.timeutil import parse_ts

HORIZONS_DAYS = (1, 7, 30)
N_BINS = 10
PINNED_HI = 0.99
PINNED_LO = 0.01
LONGSHOT_MAX = 0.15  # matches the longshot signal threshold


@dataclass
class Forecast:
    market_id: str
    question: str
    price: float      # YES price at the horizon
    outcome: int      # 1 if YES resolved, 0 if NO


def brier(prices: list[float], outcomes: list[int]) -> float:
    if not prices:
        raise ValueError("no forecasts")
    return sum((p - o) ** 2 for p, o in zip(prices, outcomes, strict=True)) / len(prices)


def brier_skill(prices: list[float], outcomes: list[int]) -> float | None:
    """1 - Brier / Brier(base rate). None when every outcome is the same."""
    base = sum(outcomes) / len(outcomes)
    ref = brier([base] * len(outcomes), outcomes)
    if ref == 0:
        return None
    return 1 - brier(prices, outcomes) / ref


def reliability_bins(prices: list[float], outcomes: list[int], n_bins: int = N_BINS) -> list[dict]:
    """Equal-width price buckets with mean price, observed YES rate and count."""
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, o in zip(prices, outcomes, strict=True):
        buckets[min(int(p * n_bins), n_bins - 1)].append((p, o))
    out = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        out.append({
            "bin": f"{i / n_bins:.1f}–{(i + 1) / n_bins:.1f}",
            "mean_price": sum(p for p, _ in b) / len(b),
            "observed_rate": sum(o for _, o in b) / len(b),
            "count": len(b),
        })
    return out


def _effective_resolution(market_id: str, resolved_at: str, outcome: str, c) -> str:
    """Earlier of the close time and the first snapshot pinned at the final price."""
    pinned = (f"yes_price >= {PINNED_HI}" if outcome == "YES" else f"yes_price <= {PINNED_LO}")
    row = c.execute(f"""
        SELECT MIN(captured_at) AS t FROM snapshots
        WHERE market_id = ? AND {pinned}
    """, (market_id,)).fetchone()
    first_pinned = parse_ts(row["t"]) if row and row["t"] else None
    closed = parse_ts(resolved_at)
    candidates = [t for t in (first_pinned, closed) if t is not None]
    return min(candidates).isoformat()


def forecasts(horizon_days: int) -> list[Forecast]:
    """One forecast per resolved market that has a snapshot at least `horizon_days` before resolution."""
    init_db()
    out = []
    with connect() as c:
        resolved = c.execute("""
            SELECT r.market_id, r.outcome, r.resolved_at, m.question
            FROM resolutions r JOIN markets m ON m.id = r.market_id
            WHERE r.outcome IN ('YES', 'NO') AND r.resolved_at IS NOT NULL
        """).fetchall()
        for r in resolved:
            effective = parse_ts(_effective_resolution(r["market_id"], r["resolved_at"], r["outcome"], c))
            cutoff = (effective - timedelta(days=horizon_days)).isoformat()
            snap = c.execute("""
                SELECT yes_price FROM snapshots
                WHERE market_id = ? AND captured_at <= ? AND yes_price IS NOT NULL
                ORDER BY captured_at DESC LIMIT 1
            """, (r["market_id"], cutoff)).fetchone()
            if not snap:
                continue
            out.append(Forecast(r["market_id"], r["question"], snap["yes_price"],
                                1 if r["outcome"] == "YES" else 0))
    return out


def report(horizon_days: int) -> dict:
    """Calibration summary for one horizon; `n` is 0 when there is nothing to score yet."""
    fc = forecasts(horizon_days)
    if not fc:
        return {"horizon_days": horizon_days, "n": 0}
    prices = [f.price for f in fc]
    outcomes = [f.outcome for f in fc]
    longshots = [f for f in fc if f.price <= LONGSHOT_MAX]
    return {
        "horizon_days": horizon_days,
        "n": len(fc),
        "base_rate": sum(outcomes) / len(outcomes),
        "brier": brier(prices, outcomes),
        "brier_skill": brier_skill(prices, outcomes),
        "bins": reliability_bins(prices, outcomes),
        # Direct test of the longshot-fade hypothesis: are cheap contracts overpriced?
        "longshot": {
            "n": len(longshots),
            "mean_price": sum(f.price for f in longshots) / len(longshots),
            "observed_rate": sum(f.outcome for f in longshots) / len(longshots),
        } if longshots else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibration of Polymarket prices vs outcomes.")
    parser.add_argument("--horizon", type=int, choices=HORIZONS_DAYS, help="Only this horizon (days)")
    args = parser.parse_args()

    for h in ([args.horizon] if args.horizon else HORIZONS_DAYS):
        r = report(h)
        print(f"\n=== {h}-day horizon ===")
        if not r["n"]:
            print("  No resolved markets with a snapshot this far before resolution yet.")
            continue
        skill = f"{r['brier_skill']:+.3f}" if r["brier_skill"] is not None else "n/a"
        print(f"  Markets: {r['n']}   Base rate: {r['base_rate']:.1%}   "
              f"Brier: {r['brier']:.4f}   Skill vs base rate: {skill}")
        print(f"  {'bucket':>9}  {'mean price':>10}  {'YES rate':>8}  {'n':>5}")
        for b in r["bins"]:
            print(f"  {b['bin']:>9}  {b['mean_price']:>10.3f}  {b['observed_rate']:>8.3f}  {b['count']:>5}")
        if r["longshot"]:
            ls = r["longshot"]
            print(f"  Longshots (YES <= {LONGSHOT_MAX}): priced {ls['mean_price']:.3f}, "
                  f"won {ls['observed_rate']:.3f} of {ls['n']}")


if __name__ == "__main__":
    main()
