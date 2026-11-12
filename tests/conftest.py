from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """A saved, real Gamma API response from tests/fixtures (so tests need no network)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own empty database; the real polybot.db is never touched."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("POLYBOT_DB", str(path))
    monkeypatch.setenv("POLYBOT_LOG_DIR", str(tmp_path / "logs"))  # tests never write to the real log
    monkeypatch.setenv("POLYBOT_CONFIG", str(tmp_path / "config.yaml"))  # absent, so tests get the defaults
    monkeypatch.setenv("POLYBOT_DATA_DIR", str(tmp_path / "data"))  # live-data files stay out of the project
    yield path
    # Close any log file a test opened, so the temporary folder can be deleted afterwards.
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "polybot_handler", False)]:
        root.removeHandler(handler)
        handler.close()


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
