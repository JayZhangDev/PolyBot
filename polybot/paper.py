"""The PAPER trading job: every few minutes, let a model and a rule trade a fake-money account.

One cycle:

1. **Settle.** Any held market that has resolved pays out: $1 a share to the
   winning side, nothing to the losing one.
2. **Exit.** For each open position, ask the strategy whether the edge is gone
   and, if so, sell into the recorded bids.
3. **Enter.** Ask the model for a probability on every streamed market, ask the
   strategy whether that beats the real cost, and buy the best few into the
   recorded asks: never more than the book held, never below Polymarket's
   minimum order, and with the fee added.
4. **Value** the account and write the status file.

Everything is simulated against the order books the streamer recorded. Steps 2
and 3 are skipped unless the streamer is connected right now, because a stale
book would mean trading at prices that no longer exist.

This job is deliberately separate from the rest: it reads files, keeps its own
small database (``data/paper.db``) and its own log, and has no code that could
send an order anywhere.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from polybot import ledger, rawstore, statusfile, tracker, warehouse
from polybot.config import Settings, load_settings
from polybot.fetch import fetch_markets_by_id
from polybot.fills import Book, simulate_buy, simulate_sell
from polybot.http import FetchError
from polybot.logsetup import setup_logging
from polybot.recorder import DEPTH
from polybot.strategy import EdgeThreshold, Strategy
from polybot.timeutil import parse_ts

LOG_NAME = "paper.log"
log = logging.getLogger("polybot.paper")


# ---------------------------------------------------------------------------
# Inputs: the recorded books and the streamed markets
# ---------------------------------------------------------------------------
def _level_columns(side: str) -> str:
    return ", ".join(f"{side}_px_{n}, {side}_sz_{n}" for n in range(1, DEPTH + 1))


def load_books(now: datetime) -> dict[str, tuple[Book, float | None]]:
    """The newest recorded book per market, plus its midpoint a day earlier (None if unknown).

    Returns ``{market_id: (book, midpoint 24 hours ago)}``.
    """
    since = (now - timedelta(days=2)).date().isoformat()
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    con = warehouse.connect()
    try:
        rows = con.execute(f"""
            WITH recent AS (SELECT * FROM book_top WHERE date >= DATE '{since}'),
            latest AS (
                SELECT * FROM recent QUALIFY row_number() OVER (PARTITION BY market_id ORDER BY ts DESC) = 1
            ), earlier AS (
                SELECT market_id, arg_max((best_bid + best_ask) / 2, ts) AS mid_before
                FROM recent
                WHERE ts <= TIMESTAMP '{day_ago}' AND best_bid IS NOT NULL AND best_ask IS NOT NULL
                GROUP BY market_id
            )
            SELECT l.market_id, e.mid_before, {_level_columns("bid")}, {_level_columns("ask")}
            FROM latest l LEFT JOIN earlier e USING (market_id)
        """).fetchall()
    finally:
        con.close()
    books = {}
    for row in rows:
        numbers = row[2:]
        bids = _levels(numbers[: 2 * DEPTH])
        asks = _levels(numbers[2 * DEPTH :])
        books[row[0]] = (Book(bids, asks), row[1])
    return books


def _levels(flat: tuple) -> tuple[tuple[float, float], ...]:
    """``(px1, sz1, px2, sz2, ...)`` with blanks dropped, as ``((px1, sz1), ...)``."""
    pairs = zip(flat[0::2], flat[1::2], strict=True)
    return tuple((float(p), float(s)) for p, s in pairs if p is not None and s is not None and s > 0)


def load_markets() -> list[dict]:
    """The markets being streamed, from the file the discovery job writes."""
    data = rawstore.read_json(rawstore.selection_path()) or {}
    return [m for m in data.get("markets") or [] if isinstance(m, dict) and m.get("market_id")]


def market_view(market: dict, book: Book, mid_before: float | None, now: datetime) -> dict:
    """What the model is shown for one market right now (the same keys the calibration tracker uses)."""
    end = parse_ts(market.get("end_date"))
    return {
        "market_id": market["market_id"],
        "question": market.get("question", ""),
        "category": market.get("category", "other"),
        "price": book.mid,
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "volume": market.get("volume"),
        "liquidity": market.get("liquidity"),
        "days_to_end": None if end is None else (end - now).total_seconds() / 86400,
        "change_24h": None if mid_before is None or book.mid is None else book.mid - mid_before,
    }


def find_model(name: str) -> tracker.Model:
    for model in tracker.load_models():
        if model.name == name:
            return model
    raise LookupError(f"no model named '{name}' in {tracker.models_dir()} (set paper.model in config.yaml)")


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
def payout_per_share(market: dict, side: str) -> float | None:
    """What one share pays once ``market`` has closed, or None if it is still open.

    Polymarket reports final prices of 1 and 0 for a normal result, and 0.5 each
    when a market is resolved as a draw.
    """
    if not market.get("closed"):
        return None
    prices = _final_prices(market)
    if prices is None:
        return None
    return prices[0] if side == "YES" else prices[1]


def _final_prices(market: dict) -> tuple[float, float] | None:
    """The two outcome prices of a market as numbers, or None if they cannot be read."""
    raw = market.get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        return float(prices[0]), float(prices[1])
    except (TypeError, ValueError, IndexError):
        return None


def settle(c, lookup: Callable[[list[str]], list[dict]], now: datetime) -> int:
    """Pay out positions whose market has resolved. Returns how many were settled."""
    held = ledger.open_positions(c)
    if not held:
        return 0
    try:
        found = {str(m.get("id")): m for m in lookup(sorted({p["market_id"] for p in held}))}
    except FetchError as exc:
        log.warning("Could not check held markets for resolution this time: %s", exc)
        return 0
    settled = 0
    for position in held:
        market = found.get(position["market_id"])
        payout = payout_per_share(market, position["side"]) if market else None
        if payout is None:
            continue
        outcome = "in its favour" if payout >= 0.99 else "against it" if payout <= 0.01 else f"at {payout:.2f}"
        ledger.record_settlement(c, position, payout, outcome, now)
        log.info(
            "PAPER settled %s %s: %.1f shares paid $%.2f.",
            position["side"],
            position["question"][:60],
            position["shares"],
            position["shares"] * payout,
        )
        settled += 1
    return settled


def exits(
    c,
    books: dict,
    markets: dict[str, dict],
    model: tracker.Model,
    strategy: Strategy,
    settings: Settings,
    now: datetime,
) -> int:
    """Sell positions the strategy no longer wants. Returns how many sales were made."""
    sold = 0
    for position in ledger.open_positions(c):
        entry = books.get(position["market_id"])
        market = markets.get(position["market_id"])
        if entry is None or market is None:
            continue  # not being streamed any more: nothing fresh to trade against
        book, mid_before = entry
        probability = _probability(model, market_view(market, book, mid_before, now))
        if probability is None:
            continue
        rate = settings.fee_rate(market.get("category", "other"), market.get("fee_rate"))
        order = strategy.decide(probability, book, rate, position["side"])
        if order is None:
            continue
        fill = simulate_sell(book.sell_levels(position["side"]), position["shares"], rate)
        if fill.shares <= 0:
            continue
        ledger.record_sell(c, position, fill, order.reason, now)
        log.info("PAPER sold %.1f %s at %.3f: %s", fill.shares, position["side"], fill.avg_price, order.reason)
        sold += 1
    return sold


def _probability(model: tracker.Model, view: dict) -> float | None:
    """The model's answer for a live market, or None if it passed, failed or made no sense."""
    if view["price"] is None:
        return None
    try:
        value = model.predict(view)
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return float(value)


def entries(
    c,
    books: dict,
    markets: dict[str, dict],
    model: tracker.Model,
    strategy: Strategy,
    settings: Settings,
    now: datetime,
) -> int:
    """Buy the best opportunities the strategy finds. Returns how many positions were opened."""
    held = {p["market_id"] for p in ledger.open_positions(c)}
    candidates = []
    for market_id, market in markets.items():
        entry = books.get(market_id)
        if entry is None or market_id in held:
            continue
        book, mid_before = entry
        view = market_view(market, book, mid_before, now)
        days = view["days_to_end"]
        if days is None or not 0 < days <= settings.paper_max_days_to_end:
            continue
        probability = _probability(model, view)
        if probability is None:
            continue
        rate = settings.fee_rate(market.get("category", "other"), market.get("fee_rate"))
        order = strategy.decide(probability, book, rate, None)
        if order is not None and order.action == "BUY":
            candidates.append((order, market, book, rate, probability))
    candidates.sort(key=lambda item: -item[0].edge)

    opened = 0
    equity = ledger.record_equity(c, now)
    for order, market, book, rate, probability in candidates:
        if len(held) + opened >= settings.paper_max_positions:
            break
        budget = min(settings.paper_stake_fraction * equity, ledger.cash(c))
        fill = simulate_buy(book.buy_levels(order.side), budget, rate, max_unit_cost=order.max_unit_cost)
        if fill.shares < settings.paper_min_shares:
            continue  # too small to be a real order: not enough cash, depth or room under the price limit
        ledger.record_buy(
            c, market, order.side, fill, model=model.name, probability=probability, reason=order.reason, now=now
        )
        log.info(
            "PAPER bought %.1f %s at %.3f (fee $%.4f, stopped by %s): %s | %s",
            fill.shares,
            order.side,
            fill.avg_price,
            fill.fees,
            fill.limited_by,
            market.get("question", "")[:60],
            order.reason,
        )
        opened += 1
    return opened


def revalue(c, books: dict) -> None:
    """Update each open position's last sale price from its book, where one is available."""
    for position in ledger.open_positions(c):
        entry = books.get(position["market_id"])
        levels = entry[0].sell_levels(position["side"]) if entry else ()
        if levels:
            ledger.mark(c, position["id"], levels[0][0])


def run_cycle(
    settings: Settings,
    now: datetime | None = None,
    *,
    books: dict | None = None,
    markets: list[dict] | None = None,
    lookup: Callable[[list[str]], list[dict]] | None = None,
    stream: dict | None = None,
    model: tracker.Model | None = None,
    strategy: Strategy | None = None,
) -> dict:
    """Run one cycle and return a summary. The inputs can be handed in, which is how the tests run it."""
    now = now or datetime.now(timezone.utc)
    stream = rawstore.read_json(rawstore.status_path()) if stream is None else stream
    live = statusfile.stream_is_live(stream, now)
    c = ledger.open_ledger(settings.paper_starting_balance)
    try:
        summary = {"settled": settle(c, lookup or fetch_markets_by_id, now), "sold": 0, "opened": 0, "live": live}
        books = load_books(now) if books is None else books
        by_id = {m["market_id"]: m for m in (load_markets() if markets is None else markets)}
        revalue(c, books)
        if live:
            model = model or find_model(settings.paper_model)
            strategy = strategy or EdgeThreshold(settings.paper_min_edge, settings.paper_exit_edge)
            summary["sold"] = exits(c, books, by_id, model, strategy, settings, now)
            summary["opened"] = entries(c, books, by_id, model, strategy, settings, now)
            revalue(c, books)
        ledger.record_equity(c, now)
        account = ledger.stats(c, now)
        calibration = rawstore.read_json(rawstore.reports_dir() / "calibration.json")
        payload = statusfile.build(account, ledger.recent_fills(c), stream, calibration, now)
        statusfile.write(payload, statusfile.resolve_path(settings.status_path))
        return {**summary, **account}
    finally:
        c.close()


def summary_line(s: dict) -> str:
    state = "" if s["live"] else " (stream not connected: no trading this run)"
    return (
        f"PAPER equity ${s['equity']:.2f} (PnL {s['pnl_total']:+.2f}), {s['open_positions']} open, "
        f"opened {s['opened']}, sold {s['sold']}, settled {s['settled']}{state}"
    )


def main() -> int:
    """Entry point for the scheduled task: one cycle, recorded in the ledger's ``runs`` table."""
    setup_logging(name=LOG_NAME)
    settings = load_settings()
    c = ledger.open_ledger(settings.paper_starting_balance)
    run_id = ledger.start_run(c)
    try:
        line = summary_line(run_cycle(settings))
        ledger.finish_run(c, run_id, "success", summary=line)
        log.info(line)
        return 0
    except Exception as exc:  # one bad run must be recorded, never left hanging
        log.exception("PAPER run failed")
        ledger.finish_run(c, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        c.close()


def account_report() -> str:
    """The text ``polybot account`` prints."""
    settings = load_settings()
    c = ledger.open_ledger(settings.paper_starting_balance)
    try:
        s = ledger.stats(c)
        lines = [
            "PAPER account (fake money; no real orders are ever sent)",
            f"  Equity        : ${s['equity']:,.2f}  (started with ${s['starting_balance']:,.2f})",
            f"  PnL           : {s['pnl_total']:+,.2f} total, {s['pnl_today']:+,.2f} today (UTC)",
            f"  Cash          : ${s['cash']:,.2f}",
            f"  Positions     : {s['open_positions']} open, {s['closed_positions']} closed",
            f"  Win rate      : {'n/a' if s['win_rate'] is None else format(s['win_rate'], '.0%')}"
            f" ({s['wins']} of {s['closed_positions']} closed positions made money)",
            f"  Max drawdown  : {s['max_drawdown']:.1%}",
            f"  Fees paid     : ${s['fees_paid']:,.2f}",
            f"  Model, rule   : {settings.paper_model}, edge above {settings.paper_min_edge:.3f}",
            "",
            "Open positions:",
        ]
        for p in ledger.open_positions(c):
            value = p["shares"] * (p["last_price"] or 0)
            lines.append(
                f"  {p['side']:<3} {p['shares']:>8.1f} sh  paid ${p['cost']:>7.2f}  now ${value:>7.2f}  "
                f"{p['question'][:60]}"
            )
        if s["open_positions"] == 0:
            lines.append("  none")
        return "\n".join(lines)
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
