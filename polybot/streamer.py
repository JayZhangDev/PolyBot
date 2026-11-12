"""Live market data from Polymarket's websocket, recorded in small batches.

A websocket is a connection that stays open so the server can push each change
the moment it happens, instead of us asking again and again. This program keeps
one open to Polymarket's public market feed, subscribes to the markets chosen by
``config.yaml``, and every couple of seconds appends what arrived to the day's
files (see ``polybot.rawstore``). It only listens; it cannot place orders.

Staying connected is most of the work:

* **Heartbeat.** We send the text ``PING`` every 10 seconds and the server
  answers ``PONG``. If nothing at all arrives for 30 seconds the connection is
  dead even though it still looks open, so we drop it and start again.
* **Reconnect with backoff.** After a drop we wait 1s, then 2s, 4s ... up to a
  minute before trying again, so a server that is down is not hammered.
* **Resubscribe.** A new connection knows nothing about the old one, so the
  full list of tokens is sent again and fresh book snapshots arrive.
* **Every disconnect is logged** with how long the connection lasted and why.
* **Reading never waits for the disk.** One task reads the socket and does
  nothing else; a second task does the writing. A slow drive then delays a
  batch by a moment instead of making the server think we stopped listening.

The hourly pipeline keeps taking its own snapshots over plain HTTP, so there is
still data if this program is not running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from polybot import rawstore
from polybot.config import Settings, load_settings
from polybot.logsetup import setup_logging
from polybot.recorder import BOOK_COLUMNS, TRADE_COLUMNS, Recorder, StreamMarket, parse_frame

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_SECONDS = 10  # how often we send PING (what Polymarket's docs ask for)
SILENCE_LIMIT_SECONDS = 30  # nothing received for this long means the connection is dead
RECEIVE_WAIT_SECONDS = 1.0  # longest we wait for a message before checking the PING and silence timers
RECEIVE_QUEUE = 4096  # messages the library may hold for us before it stops reading the socket
CHORE_SECONDS = 0.5  # how often the disk task looks for work
RECONNECT_BASE_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0
STABLE_SECONDS = 60  # a connection that lasted this long resets the backoff
SELECTION_CHECK_SECONDS = 30
STATUS_SECONDS = 10
SUMMARY_SECONDS = 300  # one "still alive" line in the log every five minutes
STATUS_FRESH_SECONDS = 45  # a status file newer than this means another streamer is running
LOG_NAME = "stream.log"  # its own file: this program runs for days and must not share a rotating log

log = logging.getLogger("polybot.streamer")


class DeadConnection(ConnectionError):
    """The socket looked open but nothing arrived, not even a PONG."""


def backoff_seconds(attempt: int) -> float:
    """Wait before reconnect number ``attempt`` in a row: 1s, 2s, 4s ... capped, plus up to 25% jitter."""
    delay = min(RECONNECT_MAX_SECONDS, RECONNECT_BASE_SECONDS * 2 ** (attempt - 1))
    return delay * (1 + 0.25 * random.random())


def load_selection(path: Path | None = None) -> list[StreamMarket]:
    """Read the markets to stream from the file the discovery job writes. Empty if it is missing."""
    data = rawstore.read_json(path or rawstore.selection_path())
    if not data:
        return []
    markets = []
    for m in data.get("markets") or []:
        try:
            markets.append(
                StreamMarket(
                    str(m["market_id"]),
                    str(m.get("question", "")),
                    str(m.get("category", "")),
                    str(m["yes_token"]),
                    str(m["no_token"]),
                )
            )
        except (KeyError, TypeError):
            continue
    return markets


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_ms() -> int:
    """The current time in the feed's own unit: milliseconds since 1970."""
    return int(time.time() * 1000)


def _connect(url: str):
    """Open the websocket. Imported here so everything else in this module works without the library."""
    import websockets

    # ping_interval adds the protocol's own keepalive on top of the text PING the server expects.
    # max_queue is how many received messages may wait for us; the default of 16 is a fraction of a second
    # at this feed's rate, so a short pause on our side would stall the connection.
    return websockets.connect(
        url, ping_interval=20, ping_timeout=20, max_size=None, max_queue=RECEIVE_QUEUE, open_timeout=20
    )


class Streamer:
    """Owns the connection loop. The pieces that touch the outside world can be swapped for tests."""

    def __init__(
        self,
        settings: Settings,
        *,
        connect: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.recorder = Recorder()
        self.stop = asyncio.Event()
        self._connect = connect or (lambda: _connect(WS_URL))
        self._clock = clock
        self._sleep = sleep
        self._selection_mtime: float | None = None
        self._write_lock = threading.Lock()
        self.state = "starting"
        self.connected_since: str | None = None
        self.last_message_at: str | None = None
        self.last_disconnect: dict | None = None
        self.messages_total = 0
        self.day = _utc_now().date().isoformat()
        self.trades_today = 0
        self.book_rows_today = 0
        self.reconnects_today = 0
        self._last_flush = self._last_status = self._last_summary = self._last_selection_check = clock()

    # ----------------------------- counters and status -----------------------------
    def restore_today(self) -> None:
        """Pick up today's counts after a restart, so "today" really means the whole UTC day."""
        self.trades_today = rawstore.count_rows("trades", self.day)
        self.book_rows_today = rawstore.count_rows("book_top", self.day)
        previous = rawstore.read_json(rawstore.status_path()) or {}
        if previous.get("day") == self.day:
            self.reconnects_today = int(previous.get("reconnects_today") or 0)

    def _roll_day(self) -> None:
        today = _utc_now().date().isoformat()
        if today != self.day:
            log.info(
                "New UTC day %s. Yesterday: %d trades, %d book rows, %d reconnects.",
                today,
                self.trades_today,
                self.book_rows_today,
                self.reconnects_today,
            )
            self.day, self.trades_today, self.book_rows_today, self.reconnects_today = today, 0, 0, 0

    def status(self) -> dict:
        """What the streamer is doing right now, for ``polybot health`` and the status page."""
        return {
            "updated_at": _utc_now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "state": self.state,
            "connected_since": self.connected_since,
            "last_message_at": self.last_message_at,
            "markets_streamed": len(self.recorder.markets),
            "tokens_subscribed": len(self.recorder.tokens),
            "messages_total": self.messages_total,
            "day": self.day,
            "trades_today": self.trades_today,
            "book_rows_today": self.book_rows_today,
            "reconnects_today": self.reconnects_today,
            "last_disconnect": self.last_disconnect,
        }

    def _write_status(self) -> None:
        rawstore.write_json_atomic(rawstore.status_path(), self.status())

    # ----------------------------- saving -----------------------------
    def _save(self, book_rows: list[tuple], trades: list[tuple]) -> None:
        """Append one batch to the day files. Runs in a worker thread so the socket keeps being read.

        The lock makes sure two batches are never written at once (a write that was
        still running when a connection dropped must finish before the next begins),
        or their lines could end up interleaved in the file.
        """
        with self._write_lock:
            if book_rows:
                rawstore.append_rows("book_top", BOOK_COLUMNS, book_rows)
            if trades:
                rawstore.append_rows("trades", TRADE_COLUMNS, trades)
            self.book_rows_today += len(book_rows)
            self.trades_today += len(trades)

    async def flush(self) -> None:
        """Write everything collected since the last flush as one batch.

        Once rows are taken out of the recorder they exist only in this batch,
        so the write is shielded: if the connection drops and this task is
        cancelled at that very moment, the batch is still written.
        """
        self._roll_day()
        book_rows, trades = self.recorder.drain()
        if book_rows or trades:
            await asyncio.shield(asyncio.to_thread(self._save, book_rows, trades))

    # ----------------------------- selection -----------------------------
    def _selection_changed(self) -> bool:
        """True when the discovery job has written a newer selection file."""
        try:
            mtime = rawstore.selection_path().stat().st_mtime
        except OSError:
            return False
        changed = mtime != self._selection_mtime
        self._selection_mtime = mtime
        return changed

    def _apply_selection(self) -> tuple[list[str], list[str]]:
        """Load the selection file into the recorder, capped at the configured maximum."""
        markets = load_selection()[: self.settings.max_stream_markets]
        return self.recorder.set_selection(markets)

    async def _refresh_subscriptions(self, ws) -> None:
        """Subscribe to newly selected tokens and drop removed ones, without reconnecting."""
        if not await asyncio.to_thread(self._selection_changed):
            return
        markets = await asyncio.to_thread(load_selection)  # file reads happen off the event loop
        added, removed = self.recorder.set_selection(markets[: self.settings.max_stream_markets])
        if added:
            self.recorder.snapshot_floor_ms = _now_ms()
            await ws.send(json.dumps({"operation": "subscribe", "assets_ids": added}))
        if removed:
            await ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": removed}))
        if added or removed:
            log.info(
                "Selection changed: +%d tokens, -%d tokens, now %d markets.",
                len(added),
                len(removed),
                len(self.recorder.markets),
            )

    # ----------------------------- one connection -----------------------------
    async def _disk_chores(self, ws) -> None:
        """Everything that touches the disk, on its own task so the socket is never kept waiting.

        Writing a batch can stall for seconds when the drive is busy. If reading
        the socket had to wait for that, the server would see a client that had
        stopped listening and drop it ("slow consumer"). So this loop does the
        slow work, and ``_receive`` does nothing but read.
        """
        while True:
            await asyncio.sleep(CHORE_SECONDS)
            now = self._clock()
            if now - self._last_flush >= self.settings.flush_seconds:
                await self.flush()
                self._last_flush = now
            if now - self._last_selection_check >= SELECTION_CHECK_SECONDS:
                await self._refresh_subscriptions(ws)
                self._last_selection_check = now
            if now - self._last_status >= STATUS_SECONDS:
                await asyncio.to_thread(rawstore.write_json_atomic, rawstore.status_path(), self.status())
                self._last_status = now
            if now - self._last_summary >= SUMMARY_SECONDS:
                log.info(
                    "Streaming %d markets: %d messages so far, today %d trades and %d book rows, %d reconnects.",
                    len(self.recorder.markets),
                    self.messages_total,
                    self.trades_today,
                    self.book_rows_today,
                    self.reconnects_today,
                )
                self._last_summary = now

    async def _receive(self, ws, chores: asyncio.Task) -> None:
        """Read the socket until it fails or a stop is requested. Also sends PING and watches for silence."""
        last_ping = last_heard = self._clock()
        while not self.stop.is_set():
            if chores.done():  # the disk task never ends on its own, so this means it failed: say why
                chores.result()
                raise RuntimeError("the disk task ended unexpectedly")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECEIVE_WAIT_SECONDS)
            except asyncio.TimeoutError:
                raw = None
            now = self._clock()
            if raw is not None:
                last_heard = now
                self.last_message_at = _utc_now().isoformat(timespec="seconds")
                for event in parse_frame(raw):
                    self.recorder.handle(event)
                    self.messages_total += 1
            if now - last_ping >= PING_SECONDS:
                await ws.send("PING")
                last_ping = now
            if now - last_heard > SILENCE_LIMIT_SECONDS:
                raise DeadConnection(f"nothing received for {now - last_heard:.0f}s, not even a PONG")

    async def run_session(self, ws) -> None:
        """Subscribe on an open socket and record until it fails or a stop is requested."""
        self.recorder.reset_books()
        self.recorder.snapshot_floor_ms = _now_ms()
        await ws.send(json.dumps({"assets_ids": self.recorder.tokens, "type": "market"}))
        self.state, self.connected_since = "connected", _utc_now().isoformat(timespec="seconds")
        log.info(
            "Connected. Subscribed to %d tokens for %d markets.", len(self.recorder.tokens), len(self.recorder.markets)
        )
        chores = asyncio.create_task(self._disk_chores(ws))
        try:
            await self._receive(ws, chores)
        finally:
            chores.cancel()
            await asyncio.gather(chores, return_exceptions=True)

    # ----------------------------- the reconnect loop -----------------------------
    async def run(self) -> None:
        """Stay connected until asked to stop. Never raises for a network problem."""
        self.restore_today()
        attempt = 0
        while not self.stop.is_set():
            self._selection_changed()  # remember the file's time so the first check does not see a "change"
            self._apply_selection()
            if not self.recorder.markets:
                self.state = "waiting for a selection"
                self._write_status()
                log.info(
                    "No markets selected yet (run `polybot discover`). Checking again in %ds.", SELECTION_CHECK_SECONDS
                )
                await self._sleep(SELECTION_CHECK_SECONDS)
                continue
            started = self._clock()
            try:
                async with self._connect() as ws:
                    await self.run_session(ws)
                reason = "stop requested"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any network failure: note it and reconnect
                reason = f"{type(exc).__name__}: {exc}"
            lived = self._clock() - started
            await self.flush()  # keep whatever arrived before the drop
            self.connected_since = None
            if self.stop.is_set():
                break
            attempt = 1 if lived >= STABLE_SECONDS else attempt + 1
            delay = backoff_seconds(attempt)
            self.reconnects_today += 1
            self.state = "reconnecting"
            self.last_disconnect = {
                "at": _utc_now().isoformat(timespec="seconds"),
                "after_seconds": round(lived),
                "reason": reason[:200],
            }
            self._write_status()
            log.warning(
                "Disconnected after %.0fs: %s. Reconnect %d today, retrying in %.1fs.",
                lived,
                reason,
                self.reconnects_today,
                delay,
            )
            await self._sleep(delay)
        self.state = "stopped"
        self._write_status()
        log.info("Streamer stopped.")


def already_running() -> bool:
    """True when another streamer wrote the status file moments ago (so we must not start a second one)."""
    previous = rawstore.read_json(rawstore.status_path())
    if not previous or previous.get("state") == "stopped":
        return False
    try:
        age = (_utc_now() - datetime.fromisoformat(previous["updated_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return age < STATUS_FRESH_SECONDS and previous.get("pid") != os.getpid()


def _stop_on_signals(streamer: Streamer) -> None:
    """Ctrl+C or a polite kill sets the stop flag, so the last batch is written before exiting."""
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                loop.add_signal_handler(getattr(signal, name), streamer.stop.set)
            except NotImplementedError:  # Windows event loops cannot hook signals; Ctrl+C still ends the run
                pass


async def _amain() -> None:
    streamer = Streamer(load_settings())
    _stop_on_signals(streamer)
    await streamer.run()


def main() -> int:
    """Entry point: ``python -m polybot stream``. Runs until stopped."""
    setup_logging(name=LOG_NAME)
    logging.getLogger("websockets").setLevel(logging.WARNING)  # the library's own chatter is not needed
    if already_running():
        log.info("Another streamer is already running; this one is exiting.")
        return 0
    log.info("Streamer starting (pid %d). Data goes to %s", os.getpid(), rawstore.data_dir() / "raw")
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Streamer interrupted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
