"""Tests for the v2 agent: Kelly sizing, probability estimator, risk rules, settlement and learning."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_market

from polybot import agent
from polybot.agent import (
    Params,
    categorize,
    decide,
    estimate_probability,
    fit_gamma,
    kelly_fraction,
    stake_for,
    to_market_view,
)
from polybot.db import connect
from polybot.storage import save_snapshot

NOW = datetime.now(timezone.utc)


def liquid(mid, yes, **kw):
    """A Gamma-style market with enough liquidity to pass the scan filter."""
    m = make_market(mid, yes=yes, **kw)
    m["liquidity"] = "80000"
    return m


def view(yes=0.5, **kw):
    """A MarketView built from a Gamma-style payload."""
    v = to_market_view(liquid("1", yes, **kw))
    assert v is not None
    return v


# --------------------------- Kelly calculator ---------------------------


def test_kelly_matches_textbook_formula():
    # Buy at 0.40 with a true 55% chance: b = 1.5, f* = (1.5*0.55 - 0.45)/1.5 = 0.25
    assert kelly_fraction(0.55, 0.40) == pytest.approx(0.25)


def test_kelly_equals_edge_over_one_minus_price():
    q, c = 0.7, 0.6
    assert kelly_fraction(q, c) == pytest.approx((q - c) / (1 - c))


@pytest.mark.parametrize("q,c", [(0.40, 0.40), (0.30, 0.40), (0.5, 0.0), (0.5, 1.0), (1.2, 0.5)])
def test_kelly_is_zero_without_edge_or_with_bad_inputs(q, c):
    assert kelly_fraction(q, c) == 0.0


def test_stake_is_half_kelly():
    # kelly = 0.05 / 0.6 = 0.0833; half-Kelly = 4.167% of a $1000 bankroll
    assert stake_for(0.45, 0.40, 1000.0) == pytest.approx(41.67, abs=0.01)


def test_stake_is_capped_at_six_percent_of_bankroll():
    assert stake_for(0.9, 0.3, 500.0) == pytest.approx(30.0)  # raw half-Kelly ~42% -> 6% of $500
    assert stake_for(0.9, 0.3, 800.0, Params(max_stake=1000)) == pytest.approx(48.0)


def test_stake_is_capped_at_fifty_dollars():
    assert stake_for(0.9, 0.3, 1000.0) == pytest.approx(50.0)  # 6% of $1000 is $60, the $50 cap binds
    assert stake_for(0.9, 0.3, 100_000.0) == pytest.approx(50.0)


def test_stake_below_minimum_is_skipped_not_rounded_up():
    v, e = opp(0.30, 0.45, ask=0.31, bid=0.29)
    s = decide(v, e, bankroll=30, cash=30, open_count=0, held_market_ids=set())  # half-Kelly ~ $1.4 < $2
    assert s.action == "SKIP" and s.reason.startswith("stake_below_minimum")


def test_stake_guards_are_two_and_fifty_dollars():
    assert (Params().min_stake, Params().max_stake) == (2.0, 50.0)


def test_stake_cap_follows_params():
    assert stake_for(0.9, 0.3, 1000.0, Params(max_stake_fraction=0.02)) == pytest.approx(20.0)


# --------------------------- Probability estimator ---------------------------


def test_calibrated_prob_identity_at_gamma_one():
    for p in (0.05, 0.3, 0.5, 0.9):
        assert agent.calibrated_prob(p, 1.0) == pytest.approx(p)


def test_favorite_longshot_prior_shrinks_longshots_and_lifts_favorites():
    assert agent.calibrated_prob(0.10, 1.1) < 0.10
    assert agent.calibrated_prob(0.90, 1.1) > 0.90
    assert agent.calibrated_prob(0.50, 1.1) == pytest.approx(0.5)


def test_estimate_edge_is_estimated_minus_market():
    est = estimate_probability(view(0.30), delta_24h=0.10, gamma=1.1, trust=1.0)
    assert est.edge == pytest.approx(est.estimated_prob - est.market_prob)
    assert est.market_prob == pytest.approx(0.30)


def test_zero_trust_reproduces_market_price():
    est = estimate_probability(view(0.30), delta_24h=0.20, gamma=1.3, trust=0.0)
    assert est.estimated_prob == pytest.approx(0.30)
    assert est.edge == pytest.approx(0.0)


def test_positive_momentum_raises_and_negative_lowers_estimate():
    up = estimate_probability(view(0.50), 0.2, 1.0, 1.0)
    down = estimate_probability(view(0.50), -0.2, 1.0, 1.0)
    flat = estimate_probability(view(0.50), 0.0, 1.0, 1.0)
    assert up.estimated_prob > flat.estimated_prob > down.estimated_prob


def test_missing_history_gives_no_recency_signal():
    est = estimate_probability(view(0.50), None, 1.0, 1.0)
    assert est.recency_prob == pytest.approx(0.50)
    assert est.edge == pytest.approx(0.0)


def test_higher_trust_means_larger_departure_from_market():
    low = estimate_probability(view(0.10), 0.05, 1.2, 0.3)
    high = estimate_probability(view(0.10), 0.05, 1.2, 1.0)
    assert abs(high.edge) > abs(low.edge)


def test_estimate_stays_a_valid_probability():
    est = estimate_probability(view(0.98), 0.5, 1.5, 1.0)
    assert 0.0 < est.estimated_prob < 1.0


def test_fit_gamma_returns_prior_without_data():
    assert fit_gamma([], []) == agent.PRIOR_GAMMA


def test_fit_gamma_moves_toward_data_when_there_is_a_lot():
    # Longshots that never win and favorites that always win: gamma should rise above the prior.
    prices = [0.10] * 60 + [0.90] * 60
    outcomes = [0] * 60 + [1] * 60
    assert fit_gamma(prices, outcomes) > agent.PRIOR_GAMMA


def test_fit_gamma_stays_near_prior_with_little_data():
    assert abs(fit_gamma([0.10, 0.90], [0, 1]) - agent.PRIOR_GAMMA) < 0.15


# --------------------------- Categories and filters ---------------------------


@pytest.mark.parametrize(
    "q,cat",
    [
        ("Will Bitcoin hit $150k in 2026?", "crypto"),
        ("Will the Lakers win the NBA Finals?", "sports"),
        ("Will Trump win the 2028 election?", "politics"),
        ("Will SpaceX launch Starship by June?", "science"),
        ("Will a new flavor of ice cream be announced?", "other"),
    ],
)
def test_categorize_by_keyword(q, cat):
    assert categorize(q) == cat


def test_categorize_prefers_api_hint():
    assert categorize("Anything at all", "US-current-affairs politics") == "politics"


def test_eligibility_needs_liquidity_and_time():
    assert agent.is_eligible(view(days_out=30))
    assert not agent.is_eligible(view(days_out=5))
    thin = view(days_out=30)
    thin.liquidity = 9_000
    assert not agent.is_eligible(thin)


# --------------------------- Risk rules ---------------------------


def opp(yes=0.30, est_prob=0.45, **kw):
    """A (view, estimate) pair with a hand-set estimate."""
    v = view(yes, **kw)
    e = estimate_probability(v, None, 1.0, 1.0)
    e.estimated_prob, e.edge = est_prob, est_prob - yes
    return v, e


def test_opens_yes_when_estimate_above_market():
    v, e = opp(0.30, 0.45, ask=0.31, bid=0.29)
    s = decide(v, e, bankroll=1000, cash=1000, open_count=0, held_market_ids=set())
    assert s.action == "OPEN" and s.side == "YES"
    assert s.entry_price == pytest.approx(0.31)
    assert 0 < s.stake <= 60


def test_opens_no_when_estimate_below_market():
    v, e = opp(0.70, 0.55, ask=0.71, bid=0.69)
    s = decide(v, e, bankroll=1000, cash=1000, open_count=0, held_market_ids=set())
    assert s.action == "OPEN" and s.side == "NO"
    assert s.entry_price == pytest.approx(0.31)  # 1 - best bid


def test_skips_at_position_limit():
    v, e = opp(ask=0.31, bid=0.29)
    s = decide(v, e, bankroll=1000, cash=1000, open_count=25, held_market_ids=set())
    assert s.action == "SKIP" and "max_positions" in s.reason
    assert decide(v, e, bankroll=1000, cash=1000, open_count=24, held_market_ids=set()).action == "OPEN"


def test_skips_already_held_market():
    v, e = opp()
    s = decide(v, e, bankroll=1000, cash=1000, open_count=1, held_market_ids={v.market_id})
    assert s.action == "SKIP" and s.reason == "already_holding_market"


def test_skips_when_spread_eats_the_edge():
    v, e = opp(0.30, 0.34, ask=0.40, bid=0.20)
    s = decide(v, e, bankroll=1000, cash=1000, open_count=0, held_market_ids=set())
    assert s.action == "SKIP" and "edge_consumed_by_spread" in s.reason


def test_skips_when_cash_is_short_and_when_stake_too_small():
    v, e = opp(0.30, 0.45, ask=0.31, bid=0.29)
    poor = decide(v, e, bankroll=1000, cash=5, open_count=0, held_market_ids=set())
    tiny = decide(v, e, bankroll=10, cash=10, open_count=0, held_market_ids=set())
    assert poor.reason.startswith("insufficient_cash")
    assert tiny.reason.startswith("stake_below_minimum")


# --------------------------- Full cycle, settlement, learning ---------------------------

LOW = Params(min_edge=0.001)  # lets the cycle trade on the small edges the estimator actually produces


def test_cycle_with_no_edge_opens_nothing_and_logs_run():
    summary = agent.run_cycle([liquid("1", 0.5), liquid("2", 0.2)], now=NOW)
    assert summary["eligible"] == 2 and summary["opportunities"] == 0 and summary["opened"] == 0
    with connect() as c:
        assert c.execute("SELECT status FROM agent_runs").fetchone()["status"] == "success"
        assert c.execute("SELECT COUNT(*) FROM agent_scan").fetchone()[0] == 2


def test_default_threshold_is_calibration_mode():
    # Paper-trading calibration phase; raise to 0.08 once 50+ forecasts resolved and trust > 0.85.
    assert Params().min_edge == 0.015
    assert Params().max_stake_fraction == 0.06 and Params().max_positions == 25


def test_cycle_opens_and_logs_decision_and_position():
    summary = agent.run_cycle([liquid("1", 0.10, ask=0.101, bid=0.099), liquid("2", 0.5)], params=LOW, now=NOW)
    assert summary["opened"] == 1
    with connect() as c:
        d = c.execute("SELECT * FROM agent_decisions WHERE action_taken = 'OPEN'").fetchone()
        assert d["market_id"] == "1" and d["kelly_stake"] > 0 and d["reason"]
        assert json.loads(d["components_json"])["gamma"]
        pos = c.execute("SELECT * FROM agent_positions").fetchone()
        assert pos["decision_id"] == d["id"] and pos["status"] == "OPEN"
        assert agent.agent_cash(c) == pytest.approx(agent.STARTING_BANKROLL - pos["stake"])


def test_skipped_opportunities_are_logged_with_a_reason():
    agent.run_cycle([liquid("1", 0.10, ask=0.101, bid=0.099)], params=Params(min_edge=0.001, max_positions=0), now=NOW)
    with connect() as c:
        d = c.execute("SELECT * FROM agent_decisions").fetchone()
    assert d["action_taken"] == "SKIP" and "max_positions" in d["reason"]


def test_repeat_skip_is_not_relogged_within_window():
    p = Params(min_edge=0.001, max_positions=0)
    for minutes in (0, 10, 20):
        agent.run_cycle([liquid("1", 0.10, ask=0.101, bid=0.099)], params=p, now=NOW + timedelta(minutes=minutes))
    with connect() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_decisions").fetchone()[0] == 1


def test_position_limit_of_twenty_five_is_enforced():
    markets = [liquid(str(i), 0.10, ask=0.101, bid=0.099) for i in range(30)]
    assert agent.run_cycle(markets, params=LOW, now=NOW)["opened"] == 25
    with connect() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_positions WHERE status = 'OPEN'").fetchone()[0] == 25


def test_stake_never_exceeds_six_percent_cap():
    agent.run_cycle([liquid("1", 0.10, ask=0.101, bid=0.099)], params=LOW, now=NOW)
    with connect() as c:
        stake = c.execute("SELECT stake FROM agent_positions").fetchone()["stake"]
    assert stake <= 0.06 * agent.STARTING_BANKROLL + 1e-9


def _opened_position():
    agent.run_cycle([liquid("1", 0.10, ask=0.101, bid=0.099)], params=LOW, now=NOW)
    with connect() as c:
        return c.execute("SELECT * FROM agent_positions").fetchone()


def test_settlement_pays_winners():
    pos = _opened_position()
    winner_yes = 1.0 if pos["side"] == "YES" else 0.0
    assert agent.settle_positions(NOW, fetch=lambda ids: [make_market("1", yes=winner_yes, closed=True)]) == 1
    with connect() as c:
        done = c.execute("SELECT * FROM agent_positions").fetchone()
        assert done["status"] == "CLOSED_WIN"
        assert done["pnl"] == pytest.approx(pos["shares"] - pos["stake"], abs=1e-3)
        assert agent.agent_cash(c) == pytest.approx(agent.STARTING_BANKROLL + done["pnl"], abs=1e-3)


def test_settlement_charges_losers_their_stake():
    pos = _opened_position()
    loser_yes = 0.0 if pos["side"] == "YES" else 1.0
    agent.settle_positions(NOW, fetch=lambda ids: [make_market("1", yes=loser_yes, closed=True)])
    with connect() as c:
        done = c.execute("SELECT * FROM agent_positions").fetchone()
    assert done["status"] == "CLOSED_LOSS" and done["pnl"] == pytest.approx(-pos["stake"])


def test_unresolved_position_stays_open():
    _opened_position()
    assert agent.settle_positions(NOW, fetch=lambda ids: [make_market("1", yes=0.12)]) == 0


def test_learning_loop_logs_accuracy_by_category():
    _opened_position()
    agent.settle_positions(NOW, fetch=lambda ids: [make_market("1", yes=0.0, closed=True)])
    result = agent.learn(NOW)
    assert result["ALL"]["n"] == 1
    assert result["ALL"]["agent_brier"] is not None and result["ALL"]["market_brier"] is not None
    with connect() as c:
        row = c.execute("SELECT n_resolved FROM agent_performance WHERE category = 'ALL'").fetchone()
        assert row["n_resolved"] == 1
        assert c.execute("SELECT COUNT(*) FROM agent_priors").fetchone()[0] == len(agent.CATEGORIES)


def test_learning_does_not_duplicate_rows_when_nothing_new_resolved():
    _opened_position()
    agent.settle_positions(NOW, fetch=lambda ids: [make_market("1", yes=0.0, closed=True)])
    agent.learn(NOW)
    agent.learn(NOW)
    with connect() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_performance WHERE category = 'ALL'").fetchone()[0] == 1


def test_recent_delta_uses_old_snapshot():
    save_snapshot([make_market("1", yes=0.40)])
    with connect() as c:
        c.execute("UPDATE snapshots SET captured_at = ?", ((NOW - timedelta(hours=24)).isoformat(),))
    save_snapshot([make_market("1", yes=0.55)])
    assert agent.recent_delta("1", NOW) == pytest.approx(0.15)


def test_recent_delta_none_without_history():
    save_snapshot([make_market("1", yes=0.40)])
    assert agent.recent_delta("1", NOW) is None


def test_export_state_shape():
    _opened_position()
    with connect() as c:
        state = agent.export_state(c)
    assert state["enabled"] and state["summary"]["open_count"] == 1
    assert state["feed"] and state["decisions"] and state["positions"][0]["current_price"] is not None


def test_export_state_before_first_run():
    from polybot.storage import init_db

    init_db()
    with connect() as c:
        assert agent.export_state(c) == {"enabled": False}


# --------------------------- Phase 1 hardening ---------------------------


def test_no_side_is_skipped_when_nobody_is_bidding():
    v, e = opp(0.30, 0.15, ask=0.31)  # the estimate says buy NO, but there is no bid to fill against
    s = decide(v, e, bankroll=1000, cash=1000, open_count=0, held_market_ids=set())
    assert s.action == "SKIP" and s.reason == "no_executable_price"


def test_run_killed_mid_cycle_is_marked_failed_by_the_next_cycle():
    agent.init_agent_tables()
    with connect() as c:
        c.execute(
            "INSERT INTO agent_runs (started_at, status) VALUES (?, 'running')",
            ((NOW - timedelta(hours=2)).isoformat(),),
        )
    agent.run_cycle([], now=NOW)
    with connect() as c:
        killed, latest = c.execute("SELECT status, error FROM agent_runs ORDER BY id").fetchall()
    assert killed["status"] == "failed" and "interrupted" in killed["error"]
    assert latest["status"] == "success"


def test_summary_line_shows_the_threshold_without_rounding_it_up():
    s = {"scanned": 2000, "eligible": 1500, "opportunities": 30, "opened": 0, "skipped": 1, "settled": 0}
    assert "|edge| > 1.5%" in agent.summary_line(s, 0.015)
