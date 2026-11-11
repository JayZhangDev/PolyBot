from conftest import make_market

from polybot import paper_trader, pipeline
from polybot.portfolio import summary
from polybot.storage import save_snapshot


def test_resolution_is_detected_after_market_leaves_active_listing(monkeypatch):
    # Cycle 1: market is active, a position opens.
    save_snapshot([make_market("42", yes=0.10, bid=0.09, ask=0.11)])
    paper_trader.run()
    assert summary()["open_positions"] == 1

    # Cycle 2: market has resolved NO, so the active fetch no longer returns it.
    requested = []

    def fake_fetch_by_id(ids):
        requested.extend(ids)
        return [make_market("42", yes=0.0, closed=True)]

    monkeypatch.setattr(pipeline, "fetch_markets_by_id", fake_fetch_by_id)
    pipeline.refresh_held_markets(fetched_ids={"other"})
    paper_trader.run()

    assert requested == ["42"]
    s = summary()
    assert s["open_positions"] == 0 and s["wins"] == 1


def test_markets_already_fetched_are_not_refetched(monkeypatch):
    save_snapshot([make_market("42", yes=0.10)])
    paper_trader.run()
    monkeypatch.setattr(pipeline, "fetch_markets_by_id",
                        lambda ids: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert pipeline.refresh_held_markets(fetched_ids={"42"}) == 0
