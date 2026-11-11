"""Polite HTTP GET with retries, shared by everything that talks to Polymarket.

Why this exists: Polymarket's servers sometimes answer "500 Internal Server
Error" or drop the connection for a moment. One hiccup used to end a whole run,
and that hour of data was lost. Now a failed request is tried again after a
short wait that doubles each time (1s, 2s, 4s, 8s). That pattern is called
exponential backoff: it rides out a brief outage without hammering a server
that is already struggling.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import requests

TIMEOUT = 30  # seconds to wait for one response
MAX_TRIES = 5  # the first attempt plus four retries
BASE_DELAY = 1.0  # seconds before the first retry; doubles after each failure
MAX_DELAY = 30.0  # never wait longer than this between two tries
# Statuses worth retrying: the server is busy or briefly broken. Anything else
# in the 400s means the request itself is wrong, so asking again cannot help.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

log = logging.getLogger(__name__)
_session: requests.Session | None = None


class FetchError(RuntimeError):
    """A request that failed for good: every retry was used up, or it can never succeed."""


class _Retryable(Exception):
    """A failure that might go away if we wait and ask again."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


def _shared_session() -> requests.Session:
    """One session for the whole run, so pages reuse a connection instead of reconnecting."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Seconds to wait before retry number ``attempt`` (1 is the first retry).

    The wait doubles each time, plus up to 25% of random "jitter" so two jobs
    that failed together do not both come back at the same instant. If the
    server said how long to wait (a Retry-After header), we wait at least that.
    """
    delay = min(MAX_DELAY, BASE_DELAY * 2 ** (attempt - 1))
    delay += delay * 0.25 * random.random()
    if retry_after is not None:
        delay = max(delay, min(retry_after, MAX_DELAY))
    return delay


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """The Retry-After header as a number of seconds, or None if absent or not a number."""
    try:
        return float(resp.headers.get("Retry-After"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _get_once(session: requests.Session, url: str, params: dict | None) -> object:
    """Make one attempt. Returns the parsed JSON, or raises _Retryable / FetchError."""
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:  # connection reset, DNS failure, timeout
        raise _Retryable(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code in RETRY_STATUSES:
        raise _Retryable(f"HTTP {resp.status_code}", _retry_after_seconds(resp))
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} for {url} params={params}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:  # a cut-off or non-JSON body
        raise _Retryable("response was not valid JSON") from exc


def get_json(
    url: str,
    params: dict | None = None,
    *,
    tries: int = MAX_TRIES,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """GET ``url`` and return its JSON, retrying temporary failures with backoff.

    Raises :class:`FetchError` when the request still fails after ``tries``
    attempts. ``session`` and ``sleep`` can be swapped out, which is how the
    tests run without a network and without really waiting.
    """
    session = session or _shared_session()
    for attempt in range(1, tries + 1):
        try:
            return _get_once(session, url, params)
        except _Retryable as problem:
            if attempt == tries:
                raise FetchError(f"{url} params={params} failed after {tries} tries: {problem}") from problem
            delay = backoff_delay(attempt, problem.retry_after)
            log.warning("%s on try %d of %d for %s; retrying in %.1fs", problem, attempt, tries, url, delay)
            sleep(delay)
    raise AssertionError("unreachable: the loop always returns or raises")
