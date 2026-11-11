"""PolyBot v2: an autonomous, paper-only prediction market agent.

The v1 pipeline (``pipeline.py``) snapshots markets and paper-trades fixed rules once an hour.
The agent is a second, independent loop that runs every 10 minutes and makes its own
probability-based decisions. It shares the market, snapshot and resolution tables with the
pipeline but keeps its own positions and bankroll, so the two never interfere.

Each cycle:

1. **Scan**: pull active markets, keep those with > $10k liquidity and > 7 days left.
2. **Estimate**: for each market build an ensemble probability from
   (a) a category-calibrated base rate, (b) a recency (momentum) signal and
   (c) a calibration-prior weight that says how far to trust the model in that category.
3. **Filter**: keep markets where ``abs(estimated - market) > MIN_EDGE`` (1.5% in calibration mode), ranked by edge.
4. **Size**: half-Kelly, capped at 6% of bankroll and $50 per market (minimum $2), 25 open positions max.
5. **Execute (paper)**: open a position or skip, and log the reason either way.
6. **Learn**: when markets resolve, settle positions, refit per-category calibration priors and
   log accuracy metrics.

Everything is logged to SQLite (``agent_*`` tables) so each decision can be audited afterwards.
No orders are ever sent anywhere.

Honest caveat: the estimator only has price history and past resolutions to work with. It
shrinks toward the market price, so large edges are rare, and a cycle with zero opportunities is
the expected outcome in an efficient market, not a bug.
"""
from __future__ import annotations

import argparse
import logging
import math
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from polybot.calibration import forecasts
from polybot.db import connect
from polybot.fetch import fetch_active_markets, fetch_markets_by_id
from polybot.jobs import INTERRUPTED, run_job
from polybot.pricing import entry_price
from polybot.resolutions import record_resolutions
from polybot.storage import _parse_yes_no_prices, init_db, save_snapshot
from polybot.timeutil import parse_ts

log = logging.getLogger("polybot.agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCAN_INTERVAL_MINUTES = 10
FETCH_LIMIT = 2000               # markets pulled per scan, highest volume first
MIN_LIQUIDITY = 10_000.0         # dollars
MIN_DAYS_LEFT = 7.0
# Calibration mode (paper trading only): raise to 8% once 50+ forecasts resolved and trust > 0.85
MIN_EDGE = 0.015                 # 1.5 probability points
KELLY_MULTIPLIER = 0.5           # half-Kelly
MAX_STAKE_FRACTION = 0.06        # never risk more than 6% of bankroll on one market
MAX_POSITIONS = 25               # diversify across more markets while calibrating
MIN_STAKE = 2.0                  # dollars; smaller bets are skipped, not rounded up
MAX_STAKE = 50.0                 # dollars; Kelly can balloon on low-edge, high-confidence markets
STARTING_BANKROLL = 1000.0       # paper dollars, separate from the v1 bankroll

PRIOR_GAMMA = 1.10               # favorite-longshot prior: actual odds are more extreme than prices
PRIOR_STRENGTH = 15.0            # pseudo-observations behind PRIOR_GAMMA when refitting
MOMENTUM_K = 0.5                 # continuation: expect half of the last 24h move to repeat
BASE_WEIGHT = 0.6                # weight of the calibrated base rate in the ensemble
RECENCY_WEIGHT = 0.4             # weight of the recency signal in the ensemble
MIN_EVIDENCE = 20                # resolved forecasts before accuracy can change a category's weight
NEUTRAL_TRUST = 0.7              # category trust before there is any evidence
MIN_TRUST, MAX_TRUST = 0.3, 1.0
SKIP_RELOG_HOURS = 6             # do not repeat the same skip for a market more often than this
SCAN_RETENTION_HOURS = 24
LOCK_WAIT_SECONDS = 180          # wait this long for the hourly pipeline; the task is killed after 9 minutes
STALE_RUN_MINUTES = 15           # a run still marked 'running' after this long was killed mid-cycle

CATEGORIES = ("politics", "crypto", "sports", "science", "other")
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "crypto": ("bitcoin", "btc", "ethereum", "eth ", "crypto", "solana", "xrp", "dogecoin", "token",
               "coinbase", "binance", "stablecoin", "nft", "airdrop"),
    "sports": ("nba", "nfl", "mlb", "nhl", "fifa", "world cup", "super bowl", "champions league",
               "premier league", "ufc", "tennis", "wimbledon", "f1", "grand prix", "olympic",
               "playoffs", "finals", "stanley cup", "la liga", "serie a", " vs "),
    "politics": ("election", "president", "trump", "biden", "harris", "senate", "congress", "democrat",
                 "republican", "prime minister", "parliament", "governor", "nominee", "ceasefire",
                 "tariff", "sanction", "war ", "putin", "zelensky", "israel", "ukraine", "china",
                 "supreme court", "impeach", "vote", "poll"),
    "science": ("nasa", "spacex", "starship", "climate", "temperature", "hurricane", "earthquake",
                "vaccine", "fda", "covid", "pandemic", "ai model", "openai", "gpt", "launch",
                "asteroid", "mars", "nobel", "quantum", "measles", "bird flu"),
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Params:
    """Tunable agent settings. Defaults are the documented production values."""

    min_edge: float = MIN_EDGE
    kelly_multiplier: float = KELLY_MULTIPLIER
    max_stake_fraction: float = MAX_STAKE_FRACTION
    max_positions: int = MAX_POSITIONS
    min_stake: float = MIN_STAKE
    max_stake: float = MAX_STAKE
    min_liquidity: float = MIN_LIQUIDITY
    min_days_left: float = MIN_DAYS_LEFT
    momentum_k: float = MOMENTUM_K


@dataclass
class MarketView:
    """The fields of one Polymarket market the agent cares about."""

    market_id: str
    question: str
    category: str
    yes_price: float             # midpoint
    best_bid: float | None
    best_ask: float | None
    liquidity: float
    volume: float
    days_left: float
    raw: dict = field(repr=False, default_factory=dict)


@dataclass
class Estimate:
    """Output of the probability estimator for one market."""

    market_id: str
    category: str
    market_prob: float
    estimated_prob: float
    edge: float                  # estimated_prob - market_prob
    base_rate_prob: float
    recency_prob: float
    delta_24h: float | None
    gamma: float
    trust: float


@dataclass
class Sizing:
    """Result of the sizing / rules check for one opportunity."""

    action: str                  # 'OPEN' or 'SKIP'
    reason: str
    side: str | None = None      # 'YES' or 'NO'
    entry_price: float | None = None
    win_prob: float | None = None
    exec_edge: float | None = None
    kelly_fraction: float = 0.0
    stake: float = 0.0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_agent_tables() -> None:
    """Create the agent_* tables (and the base tables) if they do not exist yet."""
    init_db()
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,            -- success | failed
            scanned INTEGER, eligible INTEGER, opportunities INTEGER,
            opened INTEGER, skipped INTEGER, settled INTEGER,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            run_id INTEGER,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            category TEXT,
            market_prob REAL NOT NULL,
            estimated_prob REAL NOT NULL,
            edge REAL NOT NULL,
            kelly_stake REAL NOT NULL,       -- dollars the sizing rules allowed (0 when skipped)
            action_taken TEXT NOT NULL,      -- OPEN | SKIP
            reason TEXT NOT NULL,
            side TEXT,
            entry_price REAL,
            components_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_dec_market ON agent_decisions(market_id);
        CREATE INDEX IF NOT EXISTS idx_agent_dec_time ON agent_decisions(timestamp);

        CREATE TABLE IF NOT EXISTS agent_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            category TEXT,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            shares REAL NOT NULL,
            stake REAL NOT NULL,
            market_prob REAL NOT NULL,
            estimated_prob REAL NOT NULL,
            edge REAL NOT NULL,
            opened_at TEXT NOT NULL,
            decision_id INTEGER,
            closed_at TEXT,
            close_price REAL,
            pnl REAL,
            status TEXT NOT NULL DEFAULT 'OPEN'  -- OPEN | CLOSED_WIN | CLOSED_LOSS | CLOSED_VOID
        );
        CREATE INDEX IF NOT EXISTS idx_agent_pos_status ON agent_positions(status);

        CREATE TABLE IF NOT EXISTS agent_bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            delta REAL NOT NULL,
            balance_after REAL NOT NULL,
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_scan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            scanned_at TEXT NOT NULL,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            category TEXT,
            market_prob REAL NOT NULL,
            estimated_prob REAL NOT NULL,
            edge REAL NOT NULL,
            liquidity REAL,
            days_left REAL,
            qualifies INTEGER NOT NULL      -- 1 when abs(edge) > the minimum edge
        );
        CREATE INDEX IF NOT EXISTS idx_agent_scan_run ON agent_scan(run_id);

        CREATE TABLE IF NOT EXISTS agent_priors (
            category TEXT PRIMARY KEY,
            gamma REAL NOT NULL,            -- calibration exponent: 1.0 = prices are calibrated
            n_obs INTEGER NOT NULL,
            trust REAL NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at TEXT NOT NULL,
            category TEXT NOT NULL,         -- a category name or 'ALL'
            n_resolved INTEGER NOT NULL,
            agent_brier REAL,
            market_brier REAL,
            brier_skill REAL,               -- 1 - agent/market; > 0 means the agent beat the market
            edge_capture REAL,              -- realized edge / predicted edge
            gamma REAL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_perf_cat ON agent_performance(category);
        """)
        if c.execute("SELECT COUNT(*) FROM agent_bankroll").fetchone()[0] == 0:
            c.execute("INSERT INTO agent_bankroll (at, delta, balance_after, reason) "
                      "VALUES (?, ?, ?, 'INITIAL_DEPOSIT')",
                      (_now_iso(), STARTING_BANKROLL, STARTING_BANKROLL))


def _now_iso(now: datetime | None = None) -> str:
    """Return ``now`` (default: current UTC time) as an ISO-8601 string."""
    return (now or datetime.now(timezone.utc)).isoformat()


# ---------------------------------------------------------------------------
# Probability maths
# ---------------------------------------------------------------------------
def clip_prob(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    """Clamp a probability into [lo, hi] so logits stay finite."""
    return max(lo, min(hi, p))


def logit(p: float) -> float:
    """Log-odds of a probability (clamped away from 0 and 1)."""
    p = clip_prob(p, 1e-6, 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    """Inverse of :func:`logit`."""
    return 1.0 / (1.0 + math.exp(-x))


def calibrated_prob(price: float, gamma: float) -> float:
    """Map a market price to a calibrated probability: ``sigmoid(gamma * logit(price))``.

    ``gamma > 1`` pushes probabilities away from 50% (favorites are underpriced and longshots
    overpriced, the favorite-longshot bias); ``gamma < 1`` pulls them toward 50%; ``gamma == 1``
    means prices are already calibrated.
    """
    return clip_prob(sigmoid(gamma * logit(price)))


def fit_gamma(prices: list[float], outcomes: list[int], prior_gamma: float = PRIOR_GAMMA,
              prior_strength: float = PRIOR_STRENGTH) -> float:
    """Fit the calibration exponent to resolved markets by regularised log-loss.

    Minimises ``sum(logloss) + prior_strength * (gamma - prior_gamma)^2`` on a grid, so a
    category with few observations stays near the prior and one with many moves toward what
    the data says.
    """
    if not prices:
        return prior_gamma
    best_g, best_loss = prior_gamma, float("inf")
    for i in range(41):  # 0.60 .. 1.60 in steps of 0.025
        g = 0.6 + 0.025 * i
        loss = prior_strength * (g - prior_gamma) ** 2
        for p, o in zip(prices, outcomes, strict=True):
            q = calibrated_prob(p, g)
            loss -= math.log(q if o else 1 - q)
        if loss < best_loss:
            best_g, best_loss = g, loss
    return round(best_g, 4)


def recency_prob(price: float, delta_24h: float | None, k: float = MOMENTUM_K) -> float:
    """Momentum signal: the price plus ``k`` times its last-24h move (continuation if k > 0).

    With no 24h history the signal is just the price, i.e. it contributes no edge.
    """
    if delta_24h is None:
        return clip_prob(price)
    return clip_prob(price + k * delta_24h)


def estimate_probability(view: MarketView, delta_24h: float | None, gamma: float, trust: float,
                         params: Params | None = None) -> Estimate:
    """Ensemble probability estimate for one market.

    ``base`` is the category-calibrated price and ``recency`` the momentum signal. They are
    blended ``BASE_WEIGHT : RECENCY_WEIGHT`` into a model probability, then shrunk toward the
    market price by ``trust`` (the calibration-prior weight for the category)::

        estimated = market + trust * (blend - market)

    ``edge = estimated - market``. A trust of 0 reproduces the market price exactly.
    """
    params = params or Params()
    p = clip_prob(view.yes_price)
    base = calibrated_prob(p, gamma)
    recent = recency_prob(p, delta_24h, params.momentum_k)
    blend = BASE_WEIGHT * base + RECENCY_WEIGHT * recent
    est = clip_prob(p + trust * (blend - p))
    return Estimate(view.market_id, view.category, p, est, est - p, base, recent, delta_24h, gamma, trust)


def kelly_fraction(win_prob: float, price: float) -> float:
    """Full-Kelly fraction of bankroll for buying a binary share at ``price``.

    A share costs ``price`` and pays 1 if it wins, so the net odds are ``b = (1 - price) / price``
    and the Kelly fraction ``(b*q - (1 - q)) / b`` simplifies to ``(q - price) / (1 - price)``,
    which is the edge divided by the payout-per-dollar-at-risk term ``(1 - price)``. Returns 0 when
    there is no edge or the inputs are invalid (price outside (0, 1)).
    """
    if not (0.0 < price < 1.0) or not (0.0 <= win_prob <= 1.0):
        return 0.0
    return max(0.0, (win_prob - price) / (1.0 - price))


def stake_for(win_prob: float, price: float, bankroll: float, params: Params | None = None) -> float:
    """Dollar stake: half-Kelly of bankroll, capped at ``max_stake_fraction`` of bankroll and at ``max_stake``.

    The minimum stake is enforced by :func:`decide` (a stake below ``min_stake`` is skipped, not rounded up).
    """
    params = params or Params()
    raw = params.kelly_multiplier * kelly_fraction(win_prob, price) * bankroll
    return round(min(raw, params.max_stake_fraction * bankroll, params.max_stake), 2)


# ---------------------------------------------------------------------------
# Market parsing, categories and filters
# ---------------------------------------------------------------------------
def categorize(question: str, hint: str | None = None) -> str:
    """Assign a market to one of politics / crypto / sports / science / other.

    Uses the API's own category when it names one of ours, otherwise keyword matching on the
    question (first matching category in the order crypto, sports, politics, science).
    """
    if hint:
        h = hint.strip().lower()
        for cat in CATEGORIES[:-1]:
            if cat in h:
                return cat
    text = f" {question.lower()} "
    for cat in ("crypto", "sports", "politics", "science"):
        if any(re.search(re.escape(kw), text) for kw in _KEYWORDS[cat]):
            return cat
    return "other"


def _float(value: object) -> float | None:
    """Parse a float from API strings, returning None for blanks and junk."""
    try:
        return float(value) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def to_market_view(m: dict, now: datetime | None = None) -> MarketView | None:
    """Convert a Gamma API market dict into a :class:`MarketView`, or None if it is unusable."""
    now = now or datetime.now(timezone.utc)
    mid = m.get("id")
    end = parse_ts(m.get("endDate"))
    yes, _no = _parse_yes_no_prices(m)
    if mid is None or end is None or yes is None or not (0.0 < yes < 1.0):
        return None
    question = m.get("question") or ""
    return MarketView(
        market_id=str(mid), question=question, category=categorize(question, m.get("category")),
        yes_price=yes, best_bid=_float(m.get("bestBid")), best_ask=_float(m.get("bestAsk")),
        liquidity=_float(m.get("liquidity")) or 0.0, volume=_float(m.get("volume")) or 0.0,
        days_left=(end - now).total_seconds() / 86400.0, raw=m,
    )


def is_eligible(view: MarketView, params: Params | None = None) -> bool:
    """True when the market has enough liquidity and enough time left to be worth scanning."""
    params = params or Params()
    return view.liquidity > params.min_liquidity and view.days_left > params.min_days_left


def recent_delta(market_id: str, now: datetime | None = None, hours: float = 24.0,
                 conn: sqlite3.Connection | None = None) -> float | None:
    """Change in YES price over the last ``hours`` hours, from stored snapshots.

    Compares the newest snapshot with the one closest to ``now - hours`` (within +/- 3 hours of
    that target). Returns None when there is no snapshot that old, which the estimator treats as
    "no recency signal".
    """
    now = now or datetime.now(timezone.utc)
    own = conn is None
    c = conn or connect()
    try:
        target = now - timedelta(hours=hours)
        lo, hi = (target - timedelta(hours=3)).isoformat(), (target + timedelta(hours=3)).isoformat()
        past = c.execute("""
            SELECT yes_price FROM snapshots
            WHERE market_id = ? AND captured_at BETWEEN ? AND ? AND yes_price IS NOT NULL
            ORDER BY ABS(julianday(captured_at) - julianday(?)) LIMIT 1
        """, (market_id, lo, hi, target.isoformat())).fetchone()
        latest = c.execute("""
            SELECT yes_price FROM snapshots WHERE market_id = ? AND yes_price IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """, (market_id,)).fetchone()
    finally:
        if own:
            c.close()
    if not past or not latest:
        return None
    return latest["yes_price"] - past["yes_price"]


# ---------------------------------------------------------------------------
# Priors and bankroll
# ---------------------------------------------------------------------------
def load_priors(conn: sqlite3.Connection | None = None) -> dict[str, tuple[float, float]]:
    """Return ``{category: (gamma, trust)}``, using the documented defaults for unseen categories."""
    own = conn is None
    c = conn or connect()
    try:
        rows = c.execute("SELECT category, gamma, trust FROM agent_priors").fetchall()
    finally:
        if own:
            c.close()
    priors = {cat: (PRIOR_GAMMA, NEUTRAL_TRUST) for cat in CATEGORIES}
    priors.update({r["category"]: (r["gamma"], r["trust"]) for r in rows})
    return priors


def agent_cash(c: sqlite3.Connection) -> float:
    """Current uninvested paper cash."""
    row = c.execute("SELECT balance_after FROM agent_bankroll ORDER BY id DESC LIMIT 1").fetchone()
    return float(row["balance_after"]) if row else 0.0


def _bankroll_event(c: sqlite3.Connection, delta: float, reason: str, now: datetime | None = None) -> float:
    """Append a bankroll ledger entry and return the new cash balance."""
    new = agent_cash(c) + delta
    c.execute("INSERT INTO agent_bankroll (at, delta, balance_after, reason) VALUES (?, ?, ?, ?)",
              (_now_iso(now), delta, new, reason))
    return new


def bankroll_at_cost(c: sqlite3.Connection) -> float:
    """Cash plus the cost basis of open positions: the base for the percentage caps."""
    staked = c.execute("SELECT COALESCE(SUM(stake), 0) FROM agent_positions WHERE status = 'OPEN'").fetchone()[0]
    return agent_cash(c) + float(staked)


# ---------------------------------------------------------------------------
# Sizing rules
# ---------------------------------------------------------------------------
def decide(view: MarketView, est: Estimate, *, bankroll: float, cash: float, open_count: int,
           held_market_ids: set[str], params: Params | None = None) -> Sizing:
    """Apply the sizing and risk rules to one opportunity (``abs(edge) > min_edge`` already true).

    Order of checks: already holding the market, position limit, executable price, positive edge
    after the spread, minimum stake, cash. The stake is half-Kelly capped at 6% of bankroll.
    """
    params = params or Params()
    if view.market_id in held_market_ids:
        return Sizing("SKIP", "already_holding_market")
    if open_count >= params.max_positions:
        return Sizing("SKIP", f"max_positions_reached ({params.max_positions})")

    side = "YES" if est.edge > 0 else "NO"
    price = entry_price(side, view.best_bid, view.best_ask)
    if price is None:
        return Sizing("SKIP", "no_executable_price", side=side)
    win_prob = est.estimated_prob if side == "YES" else 1.0 - est.estimated_prob
    exec_edge = win_prob - price
    if exec_edge <= 0:
        return Sizing("SKIP", f"edge_consumed_by_spread (exec edge {exec_edge:+.3f})", side=side,
                      entry_price=price, win_prob=win_prob, exec_edge=exec_edge)

    kelly = kelly_fraction(win_prob, price)
    stake = stake_for(win_prob, price, bankroll, params)
    capped = params.kelly_multiplier * kelly * bankroll > min(params.max_stake_fraction * bankroll, params.max_stake)
    if stake < params.min_stake:
        return Sizing("SKIP", f"stake_below_minimum (${stake:.2f})", side=side, entry_price=price,
                      win_prob=win_prob, exec_edge=exec_edge, kelly_fraction=kelly)
    if stake > cash:
        return Sizing("SKIP", f"insufficient_cash (need ${stake:.2f}, have ${cash:.2f})", side=side,
                      entry_price=price, win_prob=win_prob, exec_edge=exec_edge, kelly_fraction=kelly)
    note = f"opened {side}; half-Kelly {kelly * params.kelly_multiplier:.1%} of bankroll"
    if capped:
        note += f", capped at ${stake:.2f}"
    return Sizing("OPEN", note, side=side, entry_price=price, win_prob=win_prob, exec_edge=exec_edge,
                  kelly_fraction=kelly, stake=stake)


# ---------------------------------------------------------------------------
# Logging and execution
# ---------------------------------------------------------------------------
def log_decision(c: sqlite3.Connection, run_id: int | None, view: MarketView, est: Estimate,
                 sizing: Sizing, now: datetime | None = None) -> int:
    """Insert one audit row into ``agent_decisions`` and return its id."""
    import json

    components = {"base_rate_prob": round(est.base_rate_prob, 4), "recency_prob": round(est.recency_prob, 4),
                  "delta_24h": None if est.delta_24h is None else round(est.delta_24h, 4),
                  "gamma": est.gamma, "trust": est.trust, "liquidity": view.liquidity,
                  "days_left": round(view.days_left, 1), "exec_edge": sizing.exec_edge,
                  "kelly_fraction": round(sizing.kelly_fraction, 4)}
    cur = c.execute("""
        INSERT INTO agent_decisions (timestamp, run_id, market_id, question, category, market_prob,
            estimated_prob, edge, kelly_stake, action_taken, reason, side, entry_price, components_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (_now_iso(now), run_id, view.market_id, view.question, view.category, est.market_prob,
          est.estimated_prob, est.edge, sizing.stake, sizing.action, sizing.reason, sizing.side,
          sizing.entry_price, json.dumps(components)))
    return int(cur.lastrowid)


def _recently_skipped(c: sqlite3.Connection, market_id: str, reason: str, now: datetime) -> bool:
    """True if the same skip reason was already logged for this market within SKIP_RELOG_HOURS."""
    cutoff = (now - timedelta(hours=SKIP_RELOG_HOURS)).isoformat()
    key = reason.split(" (")[0]
    row = c.execute("""
        SELECT 1 FROM agent_decisions
        WHERE market_id = ? AND action_taken = 'SKIP' AND reason LIKE ? AND timestamp >= ? LIMIT 1
    """, (market_id, key + "%", cutoff)).fetchone()
    return row is not None


def open_position(c: sqlite3.Connection, view: MarketView, est: Estimate, sizing: Sizing,
                  decision_id: int, now: datetime | None = None) -> int:
    """Open a paper position and debit the agent's cash. Returns the position id."""
    assert sizing.side and sizing.entry_price
    shares = round(sizing.stake / sizing.entry_price, 4)
    cur = c.execute("""
        INSERT INTO agent_positions (market_id, question, category, side, entry_price, shares, stake,
            market_prob, estimated_prob, edge, opened_at, decision_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (view.market_id, view.question, view.category, sizing.side, sizing.entry_price, shares,
          sizing.stake, est.market_prob, est.estimated_prob, est.edge, _now_iso(now), decision_id))
    pid = int(cur.lastrowid)
    _bankroll_event(c, -sizing.stake, f"AGENT OPEN #{pid}", now)
    return pid


def settle_positions(now: datetime | None = None, fetch: Callable[[list[str]], list[dict]] | None = None) -> int:
    """Refresh held markets, then close agent positions whose market has resolved.

    A winning share pays $1, a losing one $0, and a VOID market refunds the stake. Returns the
    number of positions closed.
    """
    fetch = fetch or fetch_markets_by_id
    with connect() as c:
        held = [r["market_id"] for r in c.execute(
            "SELECT DISTINCT market_id FROM agent_positions WHERE status = 'OPEN'")]
    if not held:
        return 0
    try:
        markets = fetch(held)
        record_resolutions(markets, checked_ids=held)
        save_snapshot(markets)
    except Exception as exc:  # network trouble must not block settlement from stored data
        log.warning("Could not refresh held markets: %s", exc)
    closed = 0
    with connect() as c:
        rows = c.execute("""
            SELECT p.*, r.outcome FROM agent_positions p
            JOIN resolutions r ON r.market_id = p.market_id
            WHERE p.status = 'OPEN' AND r.outcome IN ('YES', 'NO', 'VOID')
        """).fetchall()
        for p in rows:
            if p["outcome"] == "VOID":
                proceeds, status, close_price = p["stake"], "CLOSED_VOID", p["entry_price"]
            elif p["outcome"] == p["side"]:
                proceeds, status, close_price = p["shares"], "CLOSED_WIN", 1.0
            else:
                proceeds, status, close_price = 0.0, "CLOSED_LOSS", 0.0
            c.execute("UPDATE agent_positions SET status = ?, closed_at = ?, close_price = ?, pnl = ? WHERE id = ?",
                      (status, _now_iso(now), close_price, round(proceeds - p["stake"], 4), p["id"]))
            _bankroll_event(c, proceeds, f"AGENT CLOSE #{p['id']} {status}", now)
            closed += 1
    return closed


# ---------------------------------------------------------------------------
# Learning loop
# ---------------------------------------------------------------------------
def _brier(pairs: list[tuple[float, int]]) -> float | None:
    """Mean squared error of (probability, outcome) pairs, or None if empty."""
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def resolved_decisions(c: sqlite3.Connection) -> list[sqlite3.Row]:
    """First logged decision for every market that has since resolved YES or NO."""
    return c.execute("""
        SELECT d.* , r.outcome FROM agent_decisions d
        JOIN resolutions r ON r.market_id = d.market_id
        WHERE r.outcome IN ('YES', 'NO')
          AND d.id = (SELECT MIN(id) FROM agent_decisions WHERE market_id = d.market_id)
    """).fetchall()


def learn(now: datetime | None = None) -> dict[str, dict]:
    """Learning loop: refit per-category priors and log accuracy metrics.

    * **Priors.** For each category, refit the calibration exponent ``gamma`` on every resolved
      market PolyBot has a 7-day-ahead price for (the v1 calibration data), regularised toward
      the documented prior so thin categories barely move.
    * **Trust.** A category's trust (how far the estimate is allowed to depart from the market)
      starts neutral and, once ``MIN_EVIDENCE`` agent forecasts have resolved, follows the agent's
      Brier skill against the market in that category.
    * **Metrics.** Writes one ``agent_performance`` row per category (and ``ALL``) whenever the
      number of resolved forecasts changes: agent Brier, market Brier, skill and edge capture.
    """
    init_agent_tables()
    stamp = _now_iso(now)
    by_cat_fc: dict[str, list[tuple[float, int]]] = {c: [] for c in CATEGORIES}
    for f in forecasts(7):
        by_cat_fc[categorize(f.question)].append((f.price, f.outcome))

    result: dict[str, dict] = {}
    with connect() as c:
        decisions = resolved_decisions(c)
        by_cat_dec: dict[str, list[sqlite3.Row]] = {cat: [] for cat in CATEGORIES}
        for d in decisions:
            by_cat_dec[d["category"] if d["category"] in by_cat_dec else "other"].append(d)

        for cat in CATEGORIES:
            fc = by_cat_fc[cat]
            gamma = fit_gamma([p for p, _ in fc], [o for _, o in fc])
            decs = by_cat_dec[cat]
            stats = _decision_stats(decs)
            trust = NEUTRAL_TRUST
            if stats["n"] >= MIN_EVIDENCE and stats["skill"] is not None:
                trust = max(MIN_TRUST, min(MAX_TRUST, NEUTRAL_TRUST + stats["skill"]))
            c.execute("""
                INSERT INTO agent_priors (category, gamma, n_obs, trust, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET gamma = excluded.gamma, n_obs = excluded.n_obs,
                    trust = excluded.trust, updated_at = excluded.updated_at
            """, (cat, gamma, len(fc), trust, stamp))
            _log_performance(c, stamp, cat, stats, gamma)
            result[cat] = {"gamma": gamma, "trust": trust, "n_forecasts": len(fc), **stats}
        _log_performance(c, stamp, "ALL", _decision_stats(decisions), None)
        result["ALL"] = _decision_stats(decisions)
    return result


def _decision_stats(decs: list[sqlite3.Row]) -> dict:
    """Accuracy numbers for a list of resolved first-decisions."""
    pairs_agent = [(d["estimated_prob"], 1 if d["outcome"] == "YES" else 0) for d in decs]
    pairs_market = [(d["market_prob"], o) for (_, o), d in zip(pairs_agent, decs, strict=True)]
    agent_b, market_b = _brier(pairs_agent), _brier(pairs_market)
    skill = (1 - agent_b / market_b) if agent_b is not None and market_b else None
    signed = [(o - d["market_prob"]) * (1 if d["edge"] > 0 else -1)
              for (_, o), d in zip(pairs_agent, decs, strict=True)]
    predicted = [abs(d["edge"]) for d in decs]
    capture = (sum(signed) / sum(predicted)) if predicted and sum(predicted) > 0 else None
    return {"n": len(decs), "agent_brier": agent_b, "market_brier": market_b, "skill": skill, "edge_capture": capture}


def _log_performance(c: sqlite3.Connection, stamp: str, cat: str, stats: dict, gamma: float | None) -> None:
    """Insert an ``agent_performance`` row if the resolved count for ``cat`` changed since the last row."""
    last = c.execute("SELECT n_resolved FROM agent_performance WHERE category = ? ORDER BY id DESC LIMIT 1",
                     (cat,)).fetchone()
    if last is not None and last["n_resolved"] == stats["n"]:
        return
    if last is None and stats["n"] == 0:
        return
    c.execute("""
        INSERT INTO agent_performance (computed_at, category, n_resolved, agent_brier, market_brier,
                                       brier_skill, edge_capture, gamma) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (stamp, cat, stats["n"], stats["agent_brier"], stats["market_brier"], stats["skill"],
          stats["edge_capture"], gamma))


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
def scan(markets: list[dict], params: Params | None = None, now: datetime | None = None
         ) -> tuple[list[tuple[MarketView, Estimate]], int]:
    """Estimate every eligible market. Returns ``(pairs, n_parsed)`` sorted by ``abs(edge)`` descending."""
    params = params or Params()
    now = now or datetime.now(timezone.utc)
    pairs: list[tuple[MarketView, Estimate]] = []
    parsed = 0
    with connect() as c:
        priors = load_priors(c)
        for m in markets:
            view = to_market_view(m, now)
            if view is None:
                continue
            parsed += 1
            if not is_eligible(view, params):
                continue
            gamma, trust = priors.get(view.category, (PRIOR_GAMMA, NEUTRAL_TRUST))
            est = estimate_probability(view, recent_delta(view.market_id, now, conn=c), gamma, trust, params)
            pairs.append((view, est))
    pairs.sort(key=lambda p: abs(p[1].edge), reverse=True)
    return pairs, parsed


def run_cycle(markets: list[dict] | None = None, params: Params | None = None, now: datetime | None = None,
              fetch_by_id: Callable[[list[str]], list[dict]] | None = None) -> dict:
    """Run one full agent cycle and return a summary dict.

    ``markets`` and ``fetch_by_id`` can be injected (tests do) instead of calling the live API.
    """
    params = params or Params()
    now = now or datetime.now(timezone.utc)
    init_agent_tables()
    with connect() as c:
        _close_stale_runs(c, now)
        run_id = int(c.execute("INSERT INTO agent_runs (started_at, status) VALUES (?, 'running')",
                               (_now_iso(now),)).lastrowid)
    summary = {"run_id": run_id, "scanned": 0, "eligible": 0, "opportunities": 0, "opened": 0,
               "skipped": 0, "settled": 0}
    try:
        if markets is None:
            markets = fetch_active_markets(limit=FETCH_LIMIT)
        summary["settled"] = settle_positions(now, fetch_by_id)
        learn(now)
        pairs, parsed = scan(markets, params, now)
        summary["scanned"], summary["eligible"] = parsed, len(pairs)
        save_snapshot([v.raw for v, _ in pairs])  # 10-minute price history for the recency signal
        _execute(pairs, params, now, run_id, summary)
        _finish_run(run_id, "success", summary, None, now)
    except Exception as exc:
        _finish_run(run_id, "failed", summary, str(exc), now)
        raise  # run_job logs the traceback and records the failure
    return summary


def _close_stale_runs(c: sqlite3.Connection, now: datetime) -> None:
    """Mark runs that never finished as failed, so a killed cycle does not look busy forever."""
    cutoff = (now - timedelta(minutes=STALE_RUN_MINUTES)).isoformat()
    c.execute("UPDATE agent_runs SET status = 'failed', finished_at = ?, error = ? "
              "WHERE status = 'running' AND started_at < ?", (_now_iso(now), INTERRUPTED, cutoff))


def _execute(pairs: list[tuple[MarketView, Estimate]], params: Params, now: datetime, run_id: int,
             summary: dict) -> None:
    """Store the live scan feed, then size and (paper) execute every qualifying opportunity."""
    with connect() as c:
        cutoff = (now - timedelta(hours=SCAN_RETENTION_HOURS)).isoformat()
        c.execute("DELETE FROM agent_scan WHERE scanned_at < ?", (cutoff,))
        for view, est in pairs[:200]:
            c.execute("""
                INSERT INTO agent_scan (run_id, scanned_at, market_id, question, category, market_prob,
                    estimated_prob, edge, liquidity, days_left, qualifies) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_id, _now_iso(now), view.market_id, view.question, view.category, est.market_prob,
                  est.estimated_prob, est.edge, view.liquidity, view.days_left,
                  1 if abs(est.edge) > params.min_edge else 0))

        opportunities = [(v, e) for v, e in pairs if abs(e.edge) > params.min_edge]
        summary["opportunities"] = len(opportunities)
        held = {r["market_id"] for r in c.execute("SELECT market_id FROM agent_positions WHERE status = 'OPEN'")}
        open_count = len(held)
        for view, est in opportunities:  # already ranked by edge size
            sizing = decide(view, est, bankroll=bankroll_at_cost(c), cash=agent_cash(c), open_count=open_count,
                            held_market_ids=held, params=params)
            if sizing.action == "SKIP" and _recently_skipped(c, view.market_id, sizing.reason, now):
                continue
            decision_id = log_decision(c, run_id, view, est, sizing, now)
            if sizing.action == "OPEN":
                open_position(c, view, est, sizing, decision_id, now)
                held.add(view.market_id)
                open_count += 1
                summary["opened"] += 1
            else:
                summary["skipped"] += 1


def _finish_run(run_id: int, status: str, summary: dict, error: str | None, now: datetime) -> None:
    """Close out the ``agent_runs`` row for this cycle."""
    with connect() as c:
        c.execute("""
            UPDATE agent_runs SET finished_at = ?, status = ?, scanned = ?, eligible = ?, opportunities = ?,
                opened = ?, skipped = ?, settled = ?, error = ? WHERE id = ?
        """, (_now_iso(), status, summary["scanned"], summary["eligible"], summary["opportunities"],
              summary["opened"], summary["skipped"], summary["settled"], error, run_id))


# ---------------------------------------------------------------------------
# Dashboard export
# ---------------------------------------------------------------------------
def export_state(c: sqlite3.Connection, limit_decisions: int = 150) -> dict:
    """Agent state for the dashboard's Agent tab (read-only; aggregate data only).

    ``c`` must be a connection with ``row_factory = sqlite3.Row``. Returns an empty-but-valid
    structure when the agent has never run.
    """
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "agent_runs" not in tables:
        return {"enabled": False}

    def rows(sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

    last_run = rows("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1")
    scan_run = c.execute("SELECT MAX(run_id) FROM agent_scan").fetchone()[0]
    feed = rows("SELECT * FROM agent_scan WHERE run_id = ? ORDER BY ABS(edge) DESC LIMIT 40",
                (scan_run,)) if scan_run else []

    positions = []
    for p in rows("SELECT * FROM agent_positions WHERE status = 'OPEN' ORDER BY opened_at DESC"):
        snap = c.execute("SELECT yes_price FROM snapshots WHERE market_id = ? ORDER BY id DESC LIMIT 1",
                         (p["market_id"],)).fetchone()
        yes = snap["yes_price"] if snap else None
        mark = None if yes is None else (yes if p["side"] == "YES" else 1 - yes)
        p["current_price"] = mark
        p["unrealized_pnl"] = None if mark is None else p["shares"] * mark - p["stake"]
        positions.append(p)

    cash = agent_cash(c) if "agent_bankroll" in tables else 0.0
    open_value = sum((p["stake"] + (p["unrealized_pnl"] or 0.0)) for p in positions)
    closed = rows("SELECT * FROM agent_positions WHERE status != 'OPEN' ORDER BY closed_at DESC LIMIT 20")
    realized = c.execute("SELECT COALESCE(SUM(pnl), 0) FROM agent_positions WHERE status != 'OPEN'").fetchone()[0]
    perf = rows("""
        SELECT p.* FROM agent_performance p
        WHERE p.id = (SELECT MAX(id) FROM agent_performance WHERE category = p.category) ORDER BY p.category
    """)
    priors = rows("SELECT category, gamma, n_obs, trust FROM agent_priors ORDER BY category")
    return {
        "enabled": True,
        "config": {"min_edge": MIN_EDGE, "kelly_multiplier": KELLY_MULTIPLIER, "max_stake_fraction": MAX_STAKE_FRACTION,
                   "max_positions": MAX_POSITIONS, "min_stake": MIN_STAKE,
                   "max_stake": MAX_STAKE, "mode": "calibration", "min_liquidity": MIN_LIQUIDITY,
                   "min_days_left": MIN_DAYS_LEFT,
                   "interval_minutes": SCAN_INTERVAL_MINUTES},
        "last_run": last_run[0] if last_run else None,
        "summary": {"cash": cash, "equity": cash + open_value, "starting_bankroll": STARTING_BANKROLL,
                    "realized_pnl": realized, "open_count": len(positions),
                    "decisions": c.execute("SELECT COUNT(*) FROM agent_decisions").fetchone()[0],
                    "opened_total": c.execute("SELECT COUNT(*) FROM agent_positions").fetchone()[0]},
        "feed": feed,
        "positions": positions,
        "closed": closed,
        "decisions": rows("SELECT * FROM agent_decisions ORDER BY id DESC LIMIT ?", (limit_decisions,)),
        "performance": perf,
        "priors": priors,
        "equity_curve": rows("SELECT at, balance_after FROM agent_bankroll ORDER BY id"),
    }


def summary_line(s: dict, min_edge: float) -> str:
    """One line describing a finished cycle, for the log and the job_runs table."""
    return (f"parsed {s['scanned']} markets, {s['eligible']} eligible, "
            f"{s['opportunities']} opportunities (|edge| > {min_edge:.1%}), "
            f"opened {s['opened']}, skipped {s['skipped']}, settled {s['settled']}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: run one cycle (what the 10-minute scheduled task calls). Returns the exit code."""
    parser = argparse.ArgumentParser(description="PolyBot v2 autonomous paper-trading agent (one cycle).")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE, help="Edge threshold (default 0.015)")
    args = parser.parse_args(argv)
    params = Params(min_edge=args.min_edge)
    return run_job("agent", lambda: summary_line(run_cycle(params=params), params.min_edge),
                   wait_seconds=LOCK_WAIT_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Params", "MarketView", "Estimate", "Sizing", "run_cycle", "kelly_fraction", "stake_for",
           "estimate_probability", "export_state"]
