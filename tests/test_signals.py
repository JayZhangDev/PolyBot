import pytest
from conftest import make_market

from polybot.db import connect
from polybot.signals import find_signals, longshot_signals, mispricing_candidates
from polybot.storage import save_snapshot


def test_longshot_fades_by_buying_no_at_executable_price():
    save_snapshot([make_market("1", yes=0.08, bid=0.07, ask=0.09)])
    [sig] = longshot_signals(exclude=set())
    assert sig["side"] == "NO"
    assert sig["entry_price"] == pytest.approx(0.93)


def test_longshot_respects_volume_and_time_filters():
    save_snapshot(
        [
            make_market("thin", yes=0.08, volume=10_000),
            make_market("soon", yes=0.08, days_out=2),
            make_market("fair", yes=0.40),
        ]
    )
    assert longshot_signals(exclude=set()) == []


def test_stale_snapshots_are_ignored():
    save_snapshot([make_market("1", yes=0.08)])
    with connect() as c:
        c.execute("UPDATE snapshots SET captured_at = '2020-01-01T00:00:00+00:00'")
    assert find_signals() == []


def test_excluded_markets_are_skipped():
    save_snapshot([make_market("1", yes=0.08)])
    assert find_signals(exclude_market_ids={"1"}) == []


def test_mismatch_is_watchlist_only():
    legs = [make_market(str(i), yes=0.25, bid=0.24, ask=0.26, event_id="e", neg_risk=True) for i in range(3)]
    save_snapshot(legs)
    assert all(s["strategy"] != "odds_mismatch" for s in find_signals())


def test_yes_basket_edge_uses_asks():
    # Three exclusive outcomes with asks summing to 0.90: buying all YES pays 1.
    legs = [make_market(str(i), yes=0.29, bid=0.28, ask=0.30, event_id="e", neg_risk=True) for i in range(3)]
    save_snapshot(legs)
    [cand] = mispricing_candidates()
    assert cand["yes_basket_edge"] == pytest.approx(0.10)
    assert cand["no_basket_edge"] < 0


def test_no_basket_edge_uses_bids():
    # Bids sum to 1.20: buying NO on all three costs 3 - 1.20 = 1.80 and pays 2.
    legs = [make_market(str(i), yes=0.41, bid=0.40, ask=0.42, event_id="e", neg_risk=True) for i in range(3)]
    save_snapshot(legs)
    [cand] = mispricing_candidates()
    assert cand["no_basket_edge"] == pytest.approx(0.20)


def test_mismatch_ignores_events_that_are_not_neg_risk():
    legs = [make_market(str(i), yes=0.29, bid=0.28, ask=0.30, event_id="e", neg_risk=False) for i in range(3)]
    save_snapshot(legs)
    assert mispricing_candidates() == []
