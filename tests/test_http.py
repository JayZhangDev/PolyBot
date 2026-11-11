"""Retry and backoff behaviour, tested with a fake session: no network and no real waiting."""

from __future__ import annotations

import pytest
import requests

from polybot import http
from polybot.http import FetchError, backoff_delay, get_json


class FakeResponse:
    def __init__(
        self, status: int = 200, body: object = None, headers: dict | None = None, bad_json: bool = False
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.text = "error text from the server"
        self._body = body
        self._bad_json = bad_json

    def json(self) -> object:
        if self._bad_json:
            raise ValueError("not JSON")
        return self._body


class FakeSession:
    """Hands back the queued outcomes one at a time; an exception in the queue is raised."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> FakeResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


@pytest.fixture
def waits():
    """Collects the delays get_json asked for, instead of actually sleeping."""
    return []


def test_success_on_first_try_does_not_wait(waits):
    session = FakeSession(FakeResponse(body=[{"id": "1"}]))
    assert get_json("u", session=session, sleep=waits.append) == [{"id": "1"}]
    assert session.calls == 1 and waits == []


def test_server_error_is_retried_until_it_works(waits):
    session = FakeSession(FakeResponse(500), FakeResponse(502), FakeResponse(body={"ok": True}))
    assert get_json("u", session=session, sleep=waits.append) == {"ok": True}
    assert session.calls == 3
    assert 1.0 <= waits[0] <= 1.25 and 2.0 <= waits[1] <= 2.5  # 1s then 2s, plus up to 25% jitter


def test_dropped_connection_is_retried(waits):
    session = FakeSession(requests.ConnectionError("Connection reset by peer"), FakeResponse(body=[]))
    assert get_json("u", session=session, sleep=waits.append) == []
    assert len(waits) == 1


def test_cut_off_json_is_retried(waits):
    session = FakeSession(FakeResponse(bad_json=True), FakeResponse(body=[1]))
    assert get_json("u", session=session, sleep=waits.append) == [1]


def test_gives_up_after_the_last_try(waits):
    session = FakeSession(*[FakeResponse(500)] * 5)
    with pytest.raises(FetchError, match="failed after 5 tries: HTTP 500"):
        get_json("u", session=session, sleep=waits.append)
    assert session.calls == 5 and len(waits) == 4


def test_a_wrong_request_is_not_retried(waits):
    session = FakeSession(FakeResponse(422))
    with pytest.raises(FetchError, match="HTTP 422"):
        get_json("u", session=session, sleep=waits.append)
    assert session.calls == 1 and waits == []


def test_retry_after_header_is_respected(waits):
    session = FakeSession(FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(body=[]))
    get_json("u", session=session, sleep=waits.append)
    assert waits[0] >= 7


def test_backoff_doubles_and_is_capped(monkeypatch):
    monkeypatch.setattr(http.random, "random", lambda: 0.0)  # no jitter, so the numbers are exact
    assert [backoff_delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert backoff_delay(20) == http.MAX_DELAY
    assert backoff_delay(1, retry_after=9999) == http.MAX_DELAY
