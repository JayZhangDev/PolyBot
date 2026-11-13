"""The Kalshi comparison: one question, two markets, their prices side by side.

Every 10 minutes (the PolyBot-Kalshi task, or ``polybot kalshi``):

1. **Pairing**, about once a day. List every open Kalshi market (about 110 pages,
   one a second), pair them with the Polymarket catalog (:mod:`polybot.kalshi_match`)
   and apply the hand-made corrections in ``kalshi_pairs.yaml``. The pairs are saved
   in ``data/kalshi_pairs.json``. A readable list with the near misses, for checking
   by eye, goes to ``reports/kalshi_pairs.md``.
2. **Prices**, every run. Fetch every pair's current prices from both sites, a few
   seconds apart, and add one row per pair to ``data/raw/price_gaps/<day>.csv``.

A row holds YES prices on a 0 to 1 scale:

* ``pm_bid``, ``pm_ask``, ``k_bid``, ``k_ask``: the best bid and ask on Polymarket and Kalshi.
* ``pm_mid``, ``k_mid``: the midpoint, or the last trade when one side of the book is empty.
* ``gap``: ``pm_mid - k_mid``. Positive means Polymarket prices YES higher.
* ``crossing``: the most you could lock in per share by buying YES where it is cheaper (at
  the ask) and selling it where it is dearer (at the bid), **before fees**. Above zero
  means the two books overlap. Kalshi's fee formula is in a PDF that could not be read
  automatically when this was written, so no fee is taken off.

Research only: PolyBot has no Kalshi account, and nothing here places an order anywhere.
"""

from __future__ import annotations

import logging
import os
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from polybot import catalog, kalshi, rawstore, warehouse
from polybot.config import Settings, load_settings
from polybot.db import PROJECT_ROOT
from polybot.fetch import fetch_markets_by_id
from polybot.http import FetchError
from polybot.jobs import run_job
from polybot.kalshi import KalshiMarket
from polybot.kalshi_match import Matcher, NearMiss, Pair, Question

log = logging.getLogger("polybot.crossmarket")

TABLE = "price_gaps"
GAP_COLUMNS = (
    "ts",
    "polymarket_id",
    "kalshi_ticker",
    "pm_bid",
    "pm_ask",
    "pm_mid",
    "k_bid",
    "k_ask",
    "k_mid",
    "gap",
    "crossing",
)
NEAR_MISSES_KEPT = 300  # how many near misses the pairing file and the report keep
# The lock is only needed to record the run, after the prices are already saved. The agent can hold
# it for three minutes or more, so wait up to four: a pairing run (about three minutes of downloads)
# plus the wait still ends inside the task's nine-minute limit.
LOCK_WAIT_SECONDS = 240
BIG_GAP = 0.05  # five points: the report counts gaps at least this wide
# A crossing smaller than a point is mostly the two sites' different price steps on longshots
# (0.2 cents on one, a cent on the other), so the counts that matter start at one point.
BIG_CROSSING = 0.01


# ----------------------------- files -----------------------------
def pairs_path() -> Path:
    return rawstore.data_dir() / "kalshi_pairs.json"


def overrides_path() -> Path:
    """``kalshi_pairs.yaml`` in the project, or the file named by ``POLYBOT_KALSHI_PAIRS`` (tests use this)."""
    return Path(os.environ.get("POLYBOT_KALSHI_PAIRS", PROJECT_ROOT / "kalshi_pairs.yaml"))


def report_path() -> Path:
    return rawstore.reports_dir() / "kalshi_pairs.md"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(text: object) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# ----------------------------- hand-made corrections -----------------------------
@dataclass(frozen=True)
class Overrides:
    """The corrections in ``kalshi_pairs.yaml``: pairs to add and pairs to remove."""

    add: tuple[Pair, ...] = ()
    remove: frozenset[tuple[str, str]] = frozenset()


def _entry(raw: object, section: str, file_name: str) -> Pair:
    if not isinstance(raw, dict) or "polymarket" not in raw or "kalshi" not in raw:
        raise ValueError(f"each entry under '{section}:' in {file_name} needs 'polymarket:' and 'kalshi:', got {raw!r}")
    flip = raw.get("flip", False)
    if not isinstance(flip, bool):
        raise ValueError(f"'flip' in {file_name} must be true or false, got {flip!r}")
    return Pair(str(raw["polymarket"]).strip(), str(raw["kalshi"]).strip(), 1.0, "", "", flip=flip, source="added")


def load_overrides(path: Path | None = None) -> Overrides:
    """Read ``kalshi_pairs.yaml``. No file means no corrections; a malformed entry raises ValueError."""
    path = path or overrides_path()
    if not path.exists():
        return Overrides()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or set(raw) - {"add", "remove"}:
        raise ValueError(f"{path.name} may only contain an 'add:' list and a 'remove:' list")
    add = tuple(_entry(e, "add", path.name) for e in raw.get("add") or [])
    removed = (_entry(e, "remove", path.name) for e in raw.get("remove") or [])
    return Overrides(add, frozenset((p.polymarket_id, p.kalshi_ticker) for p in removed))


# ----------------------------- pairing -----------------------------
def catalog_questions(now: datetime | None = None) -> list[Question]:
    """The Polymarket catalog's yes/no markets, as the matcher reads them."""
    return [
        Question(m.market_id, m.question, m.end_date)
        for m in catalog.open_markets(now)
        if tuple(o.lower() for o in m.outcomes) == ("yes", "no")
    ]


def build_pairing(
    questions: Sequence[Question], kalshi_markets: Iterable[KalshiMarket], min_score: float
) -> tuple[list[Pair], list[NearMiss], dict]:
    """Run the matcher over every Kalshi market as it arrives. Returns pairs, near misses and counts."""
    matcher = Matcher(questions)
    listed = kept = 0
    for market in kalshi_markets:
        listed += 1
        kept += matcher.add(market)
    pairs, misses = matcher.pairs(min_score)
    stats = {
        "polymarket_questions": len(questions),
        "kalshi_markets": listed,
        "kalshi_compared": kept,
        "min_score": min_score,
    }
    return pairs, misses, stats


def save_pairing(pairs: Sequence[Pair], misses: Sequence[NearMiss], stats: dict, now: datetime) -> None:
    rawstore.write_json_atomic(
        pairs_path(),
        {
            "updated_at": now.isoformat(timespec="seconds"),
            **stats,
            "matched": [asdict(p) for p in pairs],
            "near_misses": [asdict(m) for m in misses[:NEAR_MISSES_KEPT]],
        },
    )


def load_pairing() -> dict | None:
    return rawstore.read_json(pairs_path())


def pairing_due(saved: dict | None, settings: Settings, now: datetime) -> bool:
    """Pair again when there is no pairing yet, it is older than ``refresh_hours``, or the minimum score changed."""
    if not saved or saved.get("min_score") != settings.kalshi_min_score:
        return True
    updated = _parse_time(saved.get("updated_at"))
    return updated is None or now - updated >= timedelta(hours=settings.kalshi_refresh_hours)


def current_pairs(saved: dict, overrides: Overrides, questions: dict[str, str]) -> list[Pair]:
    """The matcher's pairs, minus those removed by hand, plus those added by hand.

    A pair added by hand replaces anything the matcher paired either of its markets with.
    """
    pairs = [
        Pair(**p)
        for p in saved.get("matched") or []
        if (p["polymarket_id"], p["kalshi_ticker"]) not in overrides.remove
    ]
    for extra in overrides.add:
        pairs = [p for p in pairs if extra.polymarket_id != p.polymarket_id and extra.kalshi_ticker != p.kalshi_ticker]
        pairs.append(replace(extra, question=questions.get(extra.polymarket_id, "(not in the catalog now)")))
    return pairs


# ----------------------------- prices -----------------------------
@dataclass(frozen=True)
class Quote:
    """One side's YES prices now. ``mid`` falls back to the last trade when one side of the book is empty."""

    bid: float | None
    ask: float | None
    last: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last


def _number(value: object) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def polymarket_quote(m: dict) -> Quote | None:
    """Prices from a Gamma API market, or None once it no longer trades."""
    if m.get("closed") or m.get("acceptingOrders") is False:
        return None
    bid, ask = _number(m.get("bestBid")), _number(m.get("bestAsk"))
    return Quote(
        bid if bid is not None and bid > 0 else None,
        ask if ask is not None and ask < 1 else None,
        _number(m.get("lastTradePrice")),
    )


def kalshi_quote(m: KalshiMarket, flip: bool = False) -> Quote | None:
    """Prices from a Kalshi market, turned around when its YES means Polymarket's NO. None once it no longer trades."""
    if m.status and m.status != "active":
        return None
    if not flip:
        return Quote(m.yes_bid, m.yes_ask, m.last_price)

    def other_side(p: float | None) -> float | None:
        return None if p is None else 1 - p

    # Buying Kalshi's NO is selling its YES: the NO bid is one minus the YES ask, and so on.
    return Quote(other_side(m.yes_ask), other_side(m.yes_bid), other_side(m.last_price))


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def gap_row(ts: str, pair: Pair, pm: Quote, k: Quote) -> list:
    """One CSV row: both sides' prices, the gap between the midpoints and the before-fees crossing."""
    gap = pm.mid - k.mid if pm.mid is not None and k.mid is not None else None
    crossing = None
    if None not in (pm.bid, pm.ask, k.bid, k.ask):
        crossing = max(k.bid - pm.ask, pm.bid - k.ask)  # type: ignore[operator]
    values = (pm.bid, pm.ask, pm.mid, k.bid, k.ask, k.mid, gap, crossing)
    return [ts, pair.polymarket_id, pair.kalshi_ticker, *(_round(v) for v in values)]


def record_prices(
    pairs: Sequence[Pair],
    now: datetime,
    *,
    fetch_kalshi: Callable[[list[str]], dict[str, KalshiMarket]] = kalshi.fetch_markets,
    fetch_polymarket: Callable[[list[str]], list[dict]] = fetch_markets_by_id,
) -> dict:
    """Fetch both sides for every pair and append one row each. Returns counts for the summary."""
    ts = now.isoformat(timespec="seconds")
    k_markets = fetch_kalshi(sorted({p.kalshi_ticker for p in pairs}))
    pm_markets = {str(m.get("id")): m for m in fetch_polymarket(sorted({p.polymarket_id for p in pairs}))}
    rows, skipped = [], 0
    for pair in pairs:
        pm, k = pm_markets.get(pair.polymarket_id), k_markets.get(pair.kalshi_ticker)
        pm_quote = polymarket_quote(pm) if pm else None
        k_quote = kalshi_quote(k, pair.flip) if k else None
        if pm_quote is None or k_quote is None:
            skipped += 1  # one side is missing or has stopped trading
            continue
        rows.append(gap_row(ts, pair, pm_quote, k_quote))
    if rows:
        rawstore.append_rows(TABLE, GAP_COLUMNS, rows)
    gaps = [abs(r[9]) for r in rows if r[9] is not None]
    return {
        "rows": len(rows),
        "skipped": skipped,
        "median_gap": statistics.median(gaps) if gaps else None,
        "crossed": sum(1 for r in rows if r[10] is not None and r[10] >= BIG_CROSSING),
    }


# ----------------------------- the readable pair list -----------------------------
def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def write_report(saved: dict, overrides: Overrides, pairs: Sequence[Pair], now: datetime) -> Path:
    """``reports/kalshi_pairs.md``: every pair, weakest first, and the near misses, for checking by eye."""
    added = sum(1 for p in pairs if p.source == "added")
    lines = [
        "# Kalshi pairs",
        "",
        f"Paired {saved.get('updated_at', '?')} from {saved.get('polymarket_questions', 0):,} Polymarket questions "
        f"and {saved.get('kalshi_markets', 0):,} open Kalshi markets ({saved.get('kalshi_compared', 0):,} shared "
        f"enough rare words to be compared). {len(pairs)} pairs: a score of at least "
        f"{saved.get('min_score', 0):.2f}, {len(overrides.remove)} removed and {added} added by hand "
        "in `kalshi_pairs.yaml`. Written " + now.strftime("%Y-%m-%d %H:%M UTC") + ".",
        "",
        "The weakest matches come first, because they are the ones worth checking. To drop a wrong pair,",
        "copy its two IDs into the `remove:` list in `kalshi_pairs.yaml`.",
        "",
        "| Score | Polymarket question (ID) | Kalshi question (ticker) |",
        "|---|---|---|",
    ]
    for p in sorted(pairs, key=lambda p: (p.source != "added", p.score)):
        score = "added" if p.source == "added" else f"{p.score:.2f}"
        flip = " (flipped)" if p.flip else ""
        lines.append(
            f"| {score} | {_cell(p.question)} ({p.polymarket_id}) | "
            f"{_cell(p.kalshi_question) or '-'} ({p.kalshi_ticker}){flip} |"
        )
    lines += [
        "",
        "## Near misses",
        "",
        "The best Kalshi candidate for questions that were not paired, and the check it failed. To pair one",
        "anyway, copy its IDs into the `add:` list (with `flip: true` if Kalshi's YES is Polymarket's NO).",
        "",
        "| Score | Polymarket question (ID) | Kalshi question (ticker) | Why not |",
        "|---|---|---|---|",
    ]
    for m in saved.get("near_misses") or []:
        lines.append(
            f"| {m['score']:.2f} | {_cell(m['question'])} ({m['polymarket_id']}) | "
            f"{_cell(m['kalshi_question'])} ({m['kalshi_ticker']}) | {_cell(m['reason'])} |"
        )
    path = report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ----------------------------- one run -----------------------------
def collect(
    settings: Settings,
    now: datetime,
    *,
    list_kalshi: Callable[[], Iterable[KalshiMarket]] = kalshi.iter_open_markets,
    fetch_kalshi: Callable[[list[str]], dict[str, KalshiMarket]] = kalshi.fetch_markets,
    fetch_polymarket: Callable[[list[str]], list[dict]] = fetch_markets_by_id,
) -> str:
    """One run: pair again if it is time, then record every pair's prices. Returns the summary line."""
    if not settings.kalshi_enabled:
        return "the Kalshi comparison is switched off in config.yaml"
    notes = []
    saved = load_pairing()
    if pairing_due(saved, settings, now):
        questions = catalog_questions(now)
        if not questions:
            notes.append("pairing waits for the market catalog (run the discover job)")
        else:
            try:
                pairs, misses, stats = build_pairing(questions, list_kalshi(), settings.kalshi_min_score)
            except FetchError as exc:
                log.warning("Could not list Kalshi markets (%s); keeping the last pairing.", exc)
                notes.append("could not list Kalshi markets, kept the last pairing")
            else:
                save_pairing(pairs, misses, stats, now)
                saved = load_pairing()
                notes.append(f"paired again: {len(pairs)} pairs among {stats['kalshi_markets']:,} Kalshi markets")
    if not saved:
        return "; ".join(notes) or "no pairs yet"

    try:
        overrides = load_overrides()
    except (ValueError, yaml.YAMLError) as exc:
        log.error("kalshi_pairs.yaml was not used: %s", exc)
        overrides = Overrides()
        notes.append("kalshi_pairs.yaml has a mistake (see the log) and was ignored")
    pairs = current_pairs(saved, overrides, {q.market_id: q.text for q in catalog_questions(now)})
    write_report(saved, overrides, pairs, now)
    result = record_prices(pairs, now, fetch_kalshi=fetch_kalshi, fetch_polymarket=fetch_polymarket)
    median = "n/a" if result["median_gap"] is None else f"{result['median_gap'] * 100:.1f} points"
    line = (
        f"{len(pairs)} pairs, {result['rows']} priced ({result['skipped']} not trading on one side), "
        f"median gap {median}, {result['crossed']} crossed by a point or more before fees"
    )
    return "; ".join([line, *notes])


def main() -> int:
    """Entry point for the PolyBot-Kalshi task. The network work happens before the database lock is taken."""
    return run_job(
        "kalshi",
        lambda summary: summary,
        prepare=lambda: collect(load_settings(), _utc_now()),
        wait_seconds=LOCK_WAIT_SECONDS,
    )


# ----------------------------- the gap report -----------------------------
def gap_report(now: datetime | None = None, hours: int = 24) -> str:
    """What ``polybot gaps`` prints: how far apart the two sites' prices were over the last ``hours``."""
    now = now or _utc_now()
    since = (now - timedelta(hours=hours)).replace(tzinfo=None)  # the view's times are UTC without a zone
    con = warehouse.connect()
    try:
        total = con.execute(
            f"""SELECT count(*), count(DISTINCT kalshi_ticker), median(abs(gap)), avg(abs(gap)),
                       avg(CASE WHEN abs(gap) >= {BIG_GAP} THEN 1.0 ELSE 0.0 END),
                       count(*) FILTER (WHERE crossing > 0),
                       count(*) FILTER (WHERE crossing >= {BIG_CROSSING}),
                       count(DISTINCT kalshi_ticker) FILTER (WHERE crossing >= {BIG_CROSSING})
                FROM {TABLE} WHERE ts >= ? AND gap IS NOT NULL""",
            [since],
        ).fetchone()
        widest = con.execute(
            f"""SELECT polymarket_id, kalshi_ticker, avg(gap), arg_max(gap, ts), max(crossing)
                FROM {TABLE} WHERE ts >= ? AND gap IS NOT NULL
                GROUP BY polymarket_id, kalshi_ticker HAVING count(*) >= 3
                ORDER BY avg(abs(gap)) DESC LIMIT 10""",
            [since],
        ).fetchall()
    finally:
        con.close()
    rows, pairs, median, mean, big_share, any_cross, crossed, crossed_pairs = total
    head = f"Kalshi vs Polymarket, last {hours} hours (YES prices; gap = Polymarket midpoint - Kalshi midpoint)"
    if not rows:
        return f"{head}\n\n  No prices recorded yet. The PolyBot-Kalshi task records them every 10 minutes."
    questions = {p["polymarket_id"]: p["question"] for p in (load_pairing() or {}).get("matched") or []}
    lines = [
        head,
        "",
        f"  Rows recorded     : {rows:,} across {pairs} pairs",
        f"  Typical gap       : {median * 100:.1f} points (median size), {mean * 100:.1f} on average",
        f"  Gaps of {BIG_GAP * 100:.0f}+ points : {big_share:.1%} of observations",
        f"  Books crossed     : {crossed} observations in {crossed_pairs} pairs by a point or more "
        f"({any_cross} by any amount), before fees on either site",
        "",
        "  A point is one cent per share. Widest average gaps (needs 3+ observations):",
        "    avg gap   now   best cross  question",
    ]
    for pm_id, ticker, avg_gap, latest, best_cross in widest:
        cross = "-" if best_cross is None else f"{best_cross * 100:+.1f}"
        question = questions.get(pm_id, pm_id)
        lines.append(f"    {avg_gap * 100:+6.1f}  {latest * 100:+5.1f}  {cross:>9}   {question[:70]}  [{ticker}]")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
