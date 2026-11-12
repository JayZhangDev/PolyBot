"""The streamer's connection loop, driven by a fake socket and a fake clock: no network, no waiting."""

from __future__ import annotations

import asyncio
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from polybot import rawstore, streamer
from polybot.config import Settings
from polybot.streamer import DeadConnection, Streamer, already_running, backoff_seconds, load_selection

NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)


class Clock:
    """Time that only moves when a test says so."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeSocket:
    """Plays back queued frames. A queued function is called (to move the clock, say), an exception is
    raised, and a float is a pause of that many seconds.

    When the queue is empty, ``recv`` waits like a silent connection would.
    """

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.sent: list[str] = []
        self.resume_at = 0.0

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        loop = asyncio.get_running_loop()
        while True:
            # The streamer gives up on a quiet socket after a moment and asks again, so a pause is kept as a
            # time to wait until rather than one long sleep that the retry would cut short.
            while loop.time() < self.resume_at:
                await asyncio.sleep(0.002)
            if not self.script:
                break
            item = self.script.pop(0)
            if isinstance(item, float):
                self.resume_at = loop.time() + item
            elif callable(item):
                item()
            elif isinstance(item, Exception):
                raise item
            else:
                return str(item)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class FakeConnection:
    """Stands in for ``websockets.connect(...)``: ``async with`` gives the socket, or fails to open."""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> object:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(streamer, "RECEIVE_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(streamer, "CHORE_SECONDS", 0.001)


def write_selection(*market_ids: str) -> None:
    markets = [
        {
            "market_id": m,
            "question": f"Will {m} happen?",
            "category": "politics",
            "yes_token": f"Y{m}",
            "no_token": f"N{m}",
        }
        for m in market_ids
    ]
    rawstore.write_json_atomic(rawstore.selection_path(), {"updated_at": "now", "markets": markets})


def book(token: str, bid: str = "0.42", ask: str = "0.44") -> str:
    return json.dumps(
        {
            "event_type": "book",
            "asset_id": token,
            "timestamp": str(NOW_MS),
            "bids": [{"price": bid, "size": "50"}],
            "asks": [{"price": ask, "size": "10"}],
        }
    )


def trade(token: str) -> str:
    return json.dumps(
        {
            "event_type": "last_trade_price",
            "asset_id": token,
            "price": "0.43",
            "size": "5",
            "side": "BUY",
            "timestamp": str(NOW_MS),
            "fee_rate_bps": "0",
            "transaction_hash": "0x1",
        }
    )


def rows(table: str) -> list[dict]:
    folder = rawstore.data_dir() / "raw" / table
    found: list[dict] = []
    for path in sorted(folder.glob("*.csv")) if folder.exists() else []:
        with path.open(newline="", encoding="utf-8") as f:
            found += list(csv.DictReader(f))
    return found


def make(
    clock: Clock, *connections: object, sleeps: list[float] | None = None, settings: Settings | None = None
) -> Streamer:
    queue = list(connections)

    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return Streamer(settings or Settings(), connect=lambda: FakeConnection(queue.pop(0)), clock=clock, sleep=fake_sleep)


def after(clock: Clock, seconds: float):
    """A script step that moves the clock forward."""
    return lambda: setattr(clock, "t", clock.t + seconds)


# --------------------------- one connection ---------------------------


def test_session_subscribes_to_both_tokens_of_every_market():
    async def scenario() -> FakeSocket:
        write_selection("1", "2")
        clock = Clock()
        s = make(clock)
        s._apply_selection()
        sock = FakeSocket(s.stop.set)
        await s.run_session(sock)
        return sock

    sock = asyncio.run(scenario())
    assert json.loads(sock.sent[0]) == {"assets_ids": ["Y1", "N1", "Y2", "N2"], "type": "market"}


def test_messages_are_written_in_a_batch_when_the_flush_time_comes():
    async def scenario() -> Streamer:
        write_selection("1")
        clock = Clock()
        s = make(clock)
        s._apply_selection()
        real_save = s._save

        def noting_save(book_rows, trades):  # note each batch as it is handed to the disk
            batches.append((len(book_rows), len(trades)))
            real_save(book_rows, trades)

        s._save = noting_save
        # Two messages arrive and nothing is written; 3 seconds later the disk task writes them as one batch.
        sock = FakeSocket(
            book("Y1"),
            trade("N1"),
            0.05,
            lambda: seen.append(list(batches)),
            after(clock, 3),
            0.05,
            lambda: seen.append(list(batches)),
            trade("Y1"),
            s.stop.set,
        )
        await s.run_session(sock)
        await s.flush()
        return s

    batches: list[tuple[int, int]] = []
    seen: list[list[tuple[int, int]]] = []
    s = asyncio.run(scenario())
    assert seen == [[], [(1, 1)]]  # nothing until the flush time, then one batch with both rows
    assert batches == [(1, 1), (0, 1)]
    assert [r["market_id"] for r in rows("book_top")] == ["1"] and rows("book_top")[0]["best_bid"] == "0.42"
    assert [(r["market_id"], r["outcome"]) for r in rows("trades")] == [("1", "1"), ("1", "0")]
    assert (s.trades_today, s.book_rows_today, s.messages_total, s.state) == (2, 1, 3, "connected")


def test_ping_is_sent_every_ten_seconds():
    async def scenario() -> FakeSocket:
        write_selection("1")
        clock = Clock()
        s = make(clock)
        s._apply_selection()
        sock = FakeSocket(after(clock, 11), "PONG", after(clock, 4), "PONG", after(clock, 11), "PONG", s.stop.set)
        await s.run_session(sock)
        return sock

    assert asyncio.run(scenario()).sent[1:] == ["PING", "PING"]  # at 11s and 26s, not at 15s


def test_silent_connection_is_declared_dead():
    async def scenario() -> None:
        write_selection("1")
        clock = Clock()
        s = make(clock)
        s._apply_selection()
        ticks = iter(range(100))

        def clock_runs_while_nothing_arrives() -> float:  # every look at the clock is 5 seconds later
            return next(ticks) * 5.0

        s._clock = clock_runs_while_nothing_arrives
        await s.run_session(FakeSocket())

    with pytest.raises(DeadConnection, match="nothing received"):
        asyncio.run(scenario())


def test_new_selection_subscribes_and_unsubscribes_without_reconnecting():
    async def scenario() -> FakeSocket:
        write_selection("1", "2")
        clock = Clock()
        s = make(clock)
        s._selection_changed()
        s._apply_selection()

        def discovery_picks_new_markets() -> None:
            write_selection("2", "3")
            os.utime(rawstore.selection_path(), (1, 1))  # a different file time, whatever the disk's resolution
            clock.t += 31

        sock = FakeSocket(discovery_picks_new_markets, "PONG", 0.05, s.stop.set)
        await s.run_session(sock)
        assert sorted(s.recorder.markets) == ["2", "3"]
        return sock

    sent = [json.loads(m) for m in asyncio.run(scenario()).sent if m != "PING"]
    assert sent[1:] == [
        {"operation": "subscribe", "assets_ids": ["N3", "Y3"]},
        {"operation": "unsubscribe", "assets_ids": ["N1", "Y1"]},
    ]


# --------------------------- reconnecting ---------------------------


def test_reconnects_resubscribes_and_logs_every_disconnect(caplog):
    async def scenario() -> tuple[Streamer, FakeSocket, FakeSocket, list[float]]:
        write_selection("1")
        clock = Clock()
        sleeps: list[float] = []
        first = FakeSocket(book("Y1"), after(clock, 120), ConnectionError("connection reset by peer"))
        second = FakeSocket(book("Y1", bid="0.40"))
        s = make(clock, first, second, sleeps=sleeps)
        second.script.append(s.stop.set)
        await s.run()
        return s, first, second, sleeps

    with caplog.at_level("WARNING"):
        s, first, second, sleeps = asyncio.run(scenario())
    assert json.loads(first.sent[0]) == json.loads(second.sent[0]) == {"assets_ids": ["Y1", "N1"], "type": "market"}
    assert s.reconnects_today == 1 and len(sleeps) == 1 and 1.0 <= sleeps[0] <= 1.25
    assert "Disconnected after 120s: ConnectionError: connection reset by peer" in caplog.text
    assert s.last_disconnect["after_seconds"] == 120 and s.state == "stopped"
    assert [r["best_bid"] for r in rows("book_top")] == ["0.42", "0.4"]  # data from before the drop was kept


def test_backoff_grows_while_connections_keep_failing_and_resets_after_a_good_one():
    async def scenario() -> list[float]:
        write_selection("1")
        clock = Clock()
        sleeps: list[float] = []
        good = FakeSocket(after(clock, 90), ConnectionError("dropped"))
        last = FakeSocket()
        s = make(clock, OSError("refused"), OSError("refused"), OSError("refused"), good, last, sleeps=sleeps)
        last.script.append(s.stop.set)
        await s.run()
        return sleeps

    sleeps = asyncio.run(scenario())
    assert [round(d) for d in (sleeps[0], sleeps[1] / 2, sleeps[2] / 4, sleeps[3])] == [1, 1, 1, 1]
    assert 4.0 <= sleeps[2] <= 5.0 and 1.0 <= sleeps[3] <= 1.25  # 1s, 2s, 4s, then back to 1s


def test_backoff_is_capped(monkeypatch):
    monkeypatch.setattr(streamer.random, "random", lambda: 0.0)
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert backoff_seconds(30) == streamer.RECONNECT_MAX_SECONDS


def test_waits_for_a_selection_instead_of_connecting_to_nothing():
    async def scenario() -> tuple[Streamer, list[float]]:
        sleeps: list[float] = []
        s = make(Clock(), sleeps=sleeps)

        async def stop_after_first_wait(seconds: float) -> None:
            sleeps.append(seconds)
            s.stop.set()

        s._sleep = stop_after_first_wait
        await s.run()
        return s, sleeps

    s, sleeps = asyncio.run(scenario())
    assert sleeps == [streamer.SELECTION_CHECK_SECONDS] and s.reconnects_today == 0


def test_selection_is_capped_at_the_configured_maximum():
    write_selection("1", "2", "3")
    s = make(Clock(), settings=Settings(max_stream_markets=2))
    s._apply_selection()
    assert sorted(s.recorder.markets) == ["1", "2"]


# --------------------------- counters, status, single instance ---------------------------


def test_restart_picks_up_todays_counts():
    today = datetime.now(timezone.utc).date().isoformat()
    rawstore.append_rows("trades", ("ts", "x"), [(f"{today}T00:00:01.000Z", 1), (f"{today}T00:00:02.000Z", 2)])
    rawstore.write_json_atomic(rawstore.status_path(), {"day": today, "reconnects_today": 4, "state": "stopped"})
    s = make(Clock())
    s.restore_today()
    assert (s.trades_today, s.book_rows_today, s.reconnects_today) == (2, 0, 4)


def test_counts_start_again_on_a_new_utc_day():
    async def scenario() -> Streamer:
        s = make(Clock())
        s.day, s.trades_today, s.book_rows_today, s.reconnects_today = "2000-01-01", 9, 9, 9
        await s.flush()
        return s

    s = asyncio.run(scenario())
    assert s.day == datetime.now(timezone.utc).date().isoformat()
    assert (s.trades_today, s.book_rows_today, s.reconnects_today) == (0, 0, 0)


def test_status_never_contains_tokens_or_anything_secret():
    write_selection("1")
    s = make(Clock())
    s._apply_selection()
    status = s.status()
    assert status["markets_streamed"] == 1 and status["tokens_subscribed"] == 2 and status["pid"] == os.getpid()
    assert "Y1" not in json.dumps(status)


def status_file(age_seconds: float, state: str = "connected", pid: int = 1) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(timespec="seconds")
    rawstore.write_json_atomic(rawstore.status_path(), {"updated_at": stamp, "state": state, "pid": pid})


def test_second_copy_refuses_to_start_while_one_is_running():
    assert already_running() is False  # no status file at all
    status_file(age_seconds=5)
    assert already_running() is True
    status_file(age_seconds=5, state="stopped")
    assert already_running() is False  # the other one said goodbye
    status_file(age_seconds=600)
    assert already_running() is False  # stale: the other one died without saying so
    status_file(age_seconds=5, pid=os.getpid())
    assert already_running() is False  # that is us


def test_main_exits_at_once_when_another_copy_is_running(monkeypatch):
    status_file(age_seconds=5)
    monkeypatch.setattr(streamer.asyncio, "run", lambda *_a, **_k: pytest.fail("must not start"))
    assert streamer.main() == 0


def test_selection_file_with_a_broken_entry_keeps_the_good_ones():
    rawstore.write_json_atomic(
        rawstore.selection_path(),
        {
            "markets": [
                {"market_id": 7, "question": "Seven?", "category": "crypto", "yes_token": "Y7", "no_token": "N7"},
                {"market_id": 8},
                "junk",
            ]
        },
    )
    (market,) = load_selection()
    assert (market.market_id, market.yes_token, market.no_token, market.category) == ("7", "Y7", "N7", "crypto")
    rawstore.selection_path().unlink()
    assert load_selection() == []


# --------------------------- reading never waits for the disk ---------------------------


def test_slow_disk_does_not_stop_the_socket_being_read():
    import threading
    import time as real_time

    async def scenario() -> tuple[list[tuple[int, bool]], Streamer]:
        write_selection("1")
        clock = Clock()
        s = make(clock)
        s._apply_selection()
        writing, finished = threading.Event(), threading.Event()
        real_save = s._save

        def slow_save(book_rows, trades):  # a drive that takes a third of a second to accept a batch
            writing.set()
            real_time.sleep(0.3)
            real_save(book_rows, trades)
            finished.set()

        s._save = slow_save
        seen: list[tuple[int, bool]] = []
        sock = FakeSocket(
            book("Y1"),
            after(clock, 3),
            0.05,  # the disk task starts its slow write here
            trade("Y1"),
            trade("N1"),
            trade("Y1"),
            lambda: seen.append((s.messages_total, writing.is_set() and not finished.is_set())),
            s.stop.set,
        )
        await s.run_session(sock)
        await s.flush()
        return seen, s

    seen, s = asyncio.run(scenario())
    assert seen == [(4, True)]  # three more messages were read while the write was still going
    assert len(rows("book_top")) == 1 and len(rows("trades")) == 3 and s.trades_today == 3


def test_disk_failure_ends_the_session_with_the_real_reason():
    async def scenario() -> None:
        write_selection("1")
        clock = Clock()
        s = make(clock)
        s._apply_selection()

        def broken_save(book_rows, trades):
            raise OSError("No space left on device")

        s._save = broken_save
        await s.run_session(FakeSocket(book("Y1"), after(clock, 3), 0.05, "PONG", 0.05))

    with pytest.raises(OSError, match="No space left"):
        asyncio.run(scenario())


def test_batch_is_not_lost_when_the_connection_drops_as_it_is_handed_to_the_disk():
    async def scenario() -> None:
        write_selection("1")
        clock = Clock()
        s = make(clock)
        s._apply_selection()
        s.recorder.handle(
            {
                "event_type": "last_trade_price",
                "asset_id": "Y1",
                "price": "0.5",
                "size": "1",
                "side": "BUY",
                "timestamp": str(NOW_MS),
            }
        )
        writer = asyncio.create_task(s.flush())
        await asyncio.sleep(0)  # the batch has left the recorder and is on its way to a worker thread
        writer.cancel()  # the session ends at exactly that moment
        await asyncio.gather(writer, return_exceptions=True)
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert len(rows("trades")) == 1
