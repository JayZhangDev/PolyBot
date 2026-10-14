from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own empty database; the real polybot.db is never touched."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("POLYBOT_DB", str(path))
    return path


def make_market(mid: str = "1", *, yes: float = 0.5, bid: float | None = None,
                ask: float | None = None, volume: float = 1_000_000, days_out: int = 30,
                closed: bool = False, event_id: str | None = None, neg_risk: bool = False,
                question: str | None = None) -> dict:
    """A Gamma API market payload with only the fields PolyBot reads."""
    end = datetime.now(timezone.utc) + timedelta(days=days_out)
    m = {
        "id": mid,
        "question": question or f"Will market {mid} happen?",
        "slug": f"market-{mid}",
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcomePrices": json.dumps([str(yes), str(round(1 - yes, 6))]),
        "volume": str(volume),
        "liquidity": "50000",
        "closed": closed,
    }
    if bid is not None:
        m["bestBid"] = bid
    if ask is not None:
        m["bestAsk"] = ask
    if event_id is not None:
        m["events"] = [{"id": event_id, "negRisk": neg_risk}]
    return m
