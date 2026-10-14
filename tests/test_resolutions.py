from conftest import make_market

from polybot import resolutions
from polybot.db import connect
from polybot.storage import save_snapshot


def test_outcome_of_closed_markets():
    assert resolutions.outcome_of(make_market(yes=1.0, closed=True)) == "YES"
    assert resolutions.outcome_of(make_market(yes=0.0, closed=True)) == "NO"
    assert resolutions.outcome_of(make_market(yes=0.5, closed=True)) == "VOID"
    assert resolutions.outcome_of(make_market(yes=0.999, closed=False)) is None


def test_resolved_at_prefers_close_time():
    m = make_market(yes=1.0, closed=True)
    m["closedTime"] = "2026-03-01 12:00:00+00"
    assert resolutions.resolved_at_of(m) == "2026-03-01T12:00:00+00:00"
    del m["closedTime"]
    assert resolutions.resolved_at_of(m).startswith(m["endDate"][:10])


def test_ended_markets_are_checked_and_resolved(monkeypatch):
    save_snapshot([make_market("old", yes=0.3, days_out=-2), make_market("live", yes=0.3)])
    monkeypatch.setattr(resolutions, "fetch_markets_by_id",
                        lambda ids: [make_market(i, yes=1.0, closed=True, days_out=-2) for i in ids])
    assert resolutions.markets_to_check() == ["old"]
    assert resolutions.sweep() == {"checked": 1, "resolved": 1}
    assert resolutions.counts() == {"YES": 1}
    # Resolved markets are never re-fetched.
    assert resolutions.markets_to_check() == []


def test_unresolved_markets_wait_before_recheck(monkeypatch):
    save_snapshot([make_market("old", yes=0.3, days_out=-2)])
    monkeypatch.setattr(resolutions, "fetch_markets_by_id",
                        lambda ids: [make_market(i, yes=0.3, closed=False, days_out=-2) for i in ids])
    assert resolutions.sweep()["checked"] == 1
    assert resolutions.counts() == {"PENDING": 1}
    assert resolutions.markets_to_check() == []


def test_markets_dropped_from_listing_are_checked():
    save_snapshot([make_market("gone", yes=0.3)])
    with connect() as c:
        c.execute("UPDATE markets SET last_seen = '2020-01-01T00:00:00+00:00'")
    assert resolutions.markets_to_check() == ["gone"]


def test_failed_fetch_still_counts_as_checked(monkeypatch):
    save_snapshot([make_market("old", yes=0.3, days_out=-2)])
    monkeypatch.setattr(resolutions, "fetch_markets_by_id", lambda ids: [])
    resolutions.sweep()
    assert resolutions.markets_to_check() == []


def test_backfill_drains_all_candidates(monkeypatch):
    save_snapshot([make_market(str(i), yes=0.3, days_out=-2) for i in range(5)])
    monkeypatch.setattr(resolutions, "fetch_markets_by_id",
                        lambda ids: [make_market(i, yes=0.0, closed=True, days_out=-2) for i in ids])
    assert resolutions.backfill(batch=2) == {"checked": 5, "resolved": 5}
