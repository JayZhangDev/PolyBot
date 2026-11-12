"""The calibration tracker: how good are the market's prices, and can a model do better?

A price of 0.30 is the market saying "30% chance". The market is *calibrated*
if, across many markets priced near 0.30, about 30% really do resolve YES.

For every resolved market we take the price at a fixed time before it was
decided (1 day and 1 week) and compare it with what happened:

* **Brier score**: the average of (probability - outcome) squared, where the
  outcome is 1 for YES and 0 for NO. 0 is perfect, always saying 50% scores
  0.25, and lower is better.
* **Calibration curve**: markets grouped into price buckets; for each bucket,
  the average price against the share that actually resolved YES.

The same scoring is applied to every model in the ``models/`` folder. A model
is shown what was known at that moment (the price, time left, category and so
on) and returns its own probability. Its Brier score is compared with the
market's on exactly the same markets, which is the question the whole project
is asking: can anything beat the price?

Rules that keep the scoring honest:

* "Decided" is the earlier of the market's close and the first snapshot where
  its price was already at 99% or more for the winning side, so a market that
  was settled in everyone's mind early is not scored on those late prices.
* The price must have been recorded in the day before the cut-off. A market
  PolyBot stopped watching is left out rather than scored on a stale price.
* VOID markets are left out.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from polybot import rawstore, warehouse
from polybot.calibration import PINNED_HI, PINNED_LO, brier, brier_skill
from polybot.db import PROJECT_ROOT

HORIZONS_DAYS = (1, 7)
STALE_DAYS = 1  # how old the price may be at the cut-off
BUCKETS = 10  # price buckets of 10 points each
MIN_VERDICT = 100  # markets a model must be scored on before it gets a verdict
MIN_CATEGORY = 5  # categories with fewer markets are folded into "(smaller categories)"

log = logging.getLogger("polybot.tracker")


@dataclass(frozen=True)
class Sample:
    """One resolved market at one horizon: what was known then, and what happened."""

    market_id: str
    question: str
    category: str
    outcome: int  # 1 if it resolved YES, 0 if NO
    horizon_days: int
    price: float  # the market's YES price at the cut-off
    price_time: datetime
    best_bid: float | None
    best_ask: float | None
    volume: float | None
    liquidity: float | None
    days_to_end: float | None  # scheduled days left at the cut-off
    change_24h: float | None  # price now minus the price a day earlier, if we have one


@dataclass(frozen=True)
class Model:
    """A forecasting model loaded from the ``models/`` folder."""

    name: str
    description: str
    predict: Callable[[dict], float | None]


# ---------------------------------------------------------------------------
# Reading the samples from the warehouse
# ---------------------------------------------------------------------------
def samples_sql(horizon_days: int) -> str:
    """One row per resolved market that has a fresh enough price ``horizon_days`` before it was decided."""
    return f"""
    WITH resolved AS (
        SELECT market_id, question, COALESCE(category, 'other') AS category, end_date, resolved_at,
               CASE outcome WHEN 'YES' THEN 1 ELSE 0 END AS outcome
        FROM markets WHERE outcome IN ('YES', 'NO') AND resolved_at IS NOT NULL
    ), pinned AS (
        SELECT s.market_id, MIN(s.ts) AS first_pinned
        FROM snapshots s JOIN resolved r USING (market_id)
        WHERE (r.outcome = 1 AND s.yes_price >= {PINNED_HI}) OR (r.outcome = 0 AND s.yes_price <= {PINNED_LO})
        GROUP BY s.market_id
    ), decided AS (
        SELECT r.*, LEAST(r.resolved_at, COALESCE(p.first_pinned, r.resolved_at)) AS decided_at
        FROM resolved r LEFT JOIN pinned p USING (market_id)
    ), priced AS (
        SELECT d.market_id, d.question, d.category, d.outcome, d.end_date, s.ts AS price_time, s.yes_price AS price,
               s.best_bid, s.best_ask, s.volume, s.liquidity
        FROM decided d JOIN snapshots s ON s.market_id = d.market_id
        WHERE s.yes_price IS NOT NULL
          AND s.ts <= d.decided_at - INTERVAL '{horizon_days} days'
          AND s.ts >= d.decided_at - INTERVAL '{horizon_days + STALE_DAYS} days'
        QUALIFY row_number() OVER (PARTITION BY d.market_id ORDER BY s.ts DESC, s.snapshot_id DESC) = 1
    ), day_before AS (
        SELECT p.market_id, arg_max(s.yes_price, s.ts) AS earlier_price
        FROM priced p JOIN snapshots s ON s.market_id = p.market_id
        WHERE s.yes_price IS NOT NULL AND s.ts <= p.price_time - INTERVAL '1 day'
          AND s.ts >= p.price_time - INTERVAL '2 days'
        GROUP BY p.market_id
    )
    SELECT p.market_id, p.question, p.category, p.outcome, p.price, p.price_time, p.best_bid, p.best_ask,
           p.volume, p.liquidity, date_diff('second', p.price_time, p.end_date) / 86400.0 AS days_to_end,
           p.price - b.earlier_price AS change_24h
    FROM priced p LEFT JOIN day_before b USING (market_id)
    ORDER BY p.market_id
    """


def load_samples(horizon_days: int) -> list[Sample]:
    con = warehouse.connect()
    try:
        rows = con.execute(samples_sql(horizon_days)).fetchall()
    finally:
        con.close()
    return [
        Sample(
            market_id=r[0],
            question=r[1],
            category=r[2],
            outcome=r[3],
            horizon_days=horizon_days,
            price=r[4],
            price_time=r[5],
            best_bid=r[6],
            best_ask=r[7],
            volume=r[8],
            liquidity=r[9],
            days_to_end=r[10],
            change_24h=r[11],
        )
        for r in rows
    ]


def data_window() -> dict:
    """What the warehouse holds right now, so the report can say how much evidence it rests on."""
    con = warehouse.connect()
    try:
        first, last = con.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
        counts = con.execute("""
            SELECT COUNT(*), COUNT(*) FILTER (WHERE outcome IN ('YES', 'NO')), COUNT(*) FILTER (WHERE outcome = 'VOID')
            FROM markets
        """).fetchone()
    finally:
        con.close()
    return {
        "first_snapshot": first,
        "last_snapshot": last,
        "markets": counts[0],
        "resolved": counts[1],
        "void": counts[2],
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def calibration_buckets(probabilities: list[float], outcomes: list[int]) -> list[dict]:
    """Group forecasts into ten equal price buckets; skip empty ones."""
    groups: list[list[tuple[float, int]]] = [[] for _ in range(BUCKETS)]
    for p, o in zip(probabilities, outcomes, strict=True):
        groups[min(int(p * BUCKETS), BUCKETS - 1)].append((p, o))
    return [
        {
            "low": i / BUCKETS,
            "high": (i + 1) / BUCKETS,
            "n": len(g),
            "mean_probability": sum(p for p, _ in g) / len(g),
            "yes_rate": sum(o for _, o in g) / len(g),
        }
        for i, g in enumerate(groups)
        if g
    ]


def score(probabilities: list[float], outcomes: list[int]) -> dict:
    """Brier score and friends for one set of forecasts. ``n`` is 0 when there is nothing to score."""
    if not probabilities:
        return {"n": 0}
    return {
        "n": len(probabilities),
        "yes_rate": sum(outcomes) / len(outcomes),
        "brier": brier(probabilities, outcomes),
        # Skill against "always predict how often YES happens": above 0 means the forecasts carry information.
        "skill_vs_base_rate": brier_skill(probabilities, outcomes),
    }


def score_by_category(samples: list[Sample]) -> list[dict]:
    """The market's score per category, biggest first; thin categories are pooled so they are not over-read."""
    by_category: dict[str, list[Sample]] = {}
    for s in samples:
        by_category.setdefault(s.category, []).append(s)
    pooled = [s for group in by_category.values() if len(group) < MIN_CATEGORY for s in group]
    rows = [
        {"category": name, **score([s.price for s in group], [s.outcome for s in group])}
        for name, group in by_category.items()
        if len(group) >= MIN_CATEGORY
    ]
    rows.sort(key=lambda r: -r["n"])
    if pooled:
        rows.append(
            {"category": "(smaller categories)", **score([s.price for s in pooled], [s.outcome for s in pooled])}
        )
    return rows


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def models_dir() -> Path:
    """``models/`` in the project, or the folder named by ``POLYBOT_MODELS_DIR``."""
    return Path(os.environ.get("POLYBOT_MODELS_DIR", PROJECT_ROOT / "models"))


def load_models(folder: Path | None = None) -> list[Model]:
    """Load every ``.py`` file in the models folder whose name does not start with an underscore.

    A model file needs a ``NAME`` and a ``predict(market)`` function. A file
    that cannot be loaded is reported and skipped; it never stops the report.
    """
    folder = folder or models_dir()
    models: list[Model] = []
    for path in sorted(folder.glob("*.py")) if folder.exists() else []:
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"polybot_model_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            name, predict = module.NAME, module.predict
            if not isinstance(name, str) or not callable(predict):
                raise TypeError("NAME must be text and predict must be a function")
        except Exception as exc:  # a broken model file is the model author's bug, not a reason to fail
            log.warning("Skipping model file %s: %s: %s", path.name, type(exc).__name__, exc)
            continue
        models.append(Model(name, str(getattr(module, "DESCRIPTION", "")).strip(), predict))
    return models


def market_view(sample: Sample) -> dict:
    """What a model is allowed to see: only facts that were known at the cut-off. Never the outcome."""
    return {
        "market_id": sample.market_id,
        "question": sample.question,
        "category": sample.category,
        "price": sample.price,
        "best_bid": sample.best_bid,
        "best_ask": sample.best_ask,
        "volume": sample.volume,
        "liquidity": sample.liquidity,
        "days_to_end": sample.days_to_end,
        "change_24h": sample.change_24h,
    }


def model_probability(model: Model, sample: Sample) -> float | None:
    """The model's probability for one market, or None if it passed, failed or answered nonsense."""
    try:
        value = model.predict(market_view(sample))
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value) or not 0 <= value <= 1:
        return None
    return float(value)


def score_model(model: Model, samples: list[Sample]) -> dict:
    """Score a model against the market price on the markets where the model gave an answer.

    ``gain`` is how much lower the model's squared error is than the market's,
    averaged per market (positive means the model was better). ``gain_error`` is
    the standard error of that average: if the gain is smaller than about twice
    its error, the difference could easily be luck.
    """
    answered = [(model_probability(model, s), s) for s in samples]
    answered = [(p, s) for p, s in answered if p is not None]
    result = {
        "model": model.name,
        "description": model.description,
        "n": len(answered),
        "skipped": len(samples) - len(answered),
    }
    if not answered:
        return result
    outcomes = [s.outcome for _, s in answered]
    model_brier = brier([p for p, _ in answered], outcomes)
    market_brier = brier([s.price for _, s in answered], outcomes)
    gains = [(s.price - s.outcome) ** 2 - (p - s.outcome) ** 2 for p, s in answered]
    mean_gain = sum(gains) / len(gains)
    spread = math.sqrt(sum((g - mean_gain) ** 2 for g in gains) / (len(gains) - 1)) if len(gains) > 1 else 0.0
    gain_error = spread / math.sqrt(len(gains))
    return {
        **result,
        "model_brier": model_brier,
        "market_brier": market_brier,
        "gain": mean_gain,
        "gain_error": gain_error,
        "verdict": _verdict(mean_gain, gain_error, len(gains)),
    }


def _verdict(gain: float, error: float, n: int) -> str:
    """A deliberately cautious one-phrase reading of a model's gain.

    Markets that resolve together (ten candidates in one election, say) are not
    independent, so the standard error understates the real uncertainty. Below
    ``MIN_VERDICT`` markets no verdict is given at all, and above it the wording
    stays "so far": a lead has to keep holding as more markets resolve.
    """
    if n < MIN_VERDICT:
        return "too few markets to say"
    if abs(gain) <= 2 * error:
        return "no clear difference"
    return "ahead of the market so far" if gain > 0 else "behind the market so far"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def reports_dir() -> Path:
    """Where the report is written (see ``rawstore.reports_dir``)."""
    return rawstore.reports_dir()


def build_report(models: list[Model] | None = None) -> dict:
    """Everything the report shows, as plain data (no files written)."""
    models = load_models() if models is None else models
    horizons = []
    for days in HORIZONS_DAYS:
        samples = load_samples(days)
        prices, outcomes = [s.price for s in samples], [s.outcome for s in samples]
        horizons.append(
            {
                "horizon_days": days,
                "market": score(prices, outcomes),
                "buckets": calibration_buckets(prices, outcomes),
                "categories": score_by_category(samples),
                "models": [score_model(m, samples) for m in models],
            }
        )
    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": data_window(),
        "horizons": horizons,
    }


def _number(value: float | None, pattern: str = "{:.4f}") -> str:
    return "n/a" if value is None else pattern.format(value)


def _horizon_name(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"


def markdown(report: dict) -> str:
    """The report as Markdown: a summary, then one section per horizon."""
    w = report["window"]
    lines = [
        "# Calibration report",
        "",
        f"Built {report['built_at'][:16].replace('T', ' ')} UTC from the warehouse. Research only; paper trading.",
        "",
    ]
    if w["first_snapshot"] is None:
        return "\n".join([*lines, "No price history in the warehouse yet. Run `python -m polybot rollup` first.", ""])
    days = (w["last_snapshot"] - w["first_snapshot"]).total_seconds() / 86400
    lines += [
        f"- Price history: {w['first_snapshot']:%Y-%m-%d} to {w['last_snapshot']:%Y-%m-%d} ({days:.0f} days).",
        f"- Markets known: {w['markets']:,}. Resolved YES or NO: {w['resolved']:,}. VOID (left out): {w['void']:,}.",
        "- Brier score: 0 is perfect, 0.25 is what always saying 50% scores, lower is better.",
        "",
        "## Summary",
        "",
        "| Price taken | Markets | Resolved YES | Market Brier | Skill vs base rate |",
        "|---|---|---|---|---|",
    ]
    for h in report["horizons"]:
        m = h["market"]
        if m["n"]:
            lines.append(
                f"| {_horizon_name(h['horizon_days'])} before | {m['n']} | {m['yes_rate']:.0%} | "
                f"{m['brier']:.4f} | {_number(m['skill_vs_base_rate'], '{:+.3f}')} |"
            )
        else:
            lines.append(f"| {_horizon_name(h['horizon_days'])} before | 0 | n/a | n/a | n/a |")
    lines += [
        "",
        '"Skill vs base rate" compares the price with always guessing how often YES happens. '
        "Above 0 means prices carry real information.",
        "",
    ]
    for h in report["horizons"]:
        lines += _horizon_section(h)
    lines += [
        "## How to read this",
        "",
        "- A small number of markets can make any score look good or bad by luck. Treat a bucket or category "
        "with fewer than about 30 markets as a hint, not a result.",
        "- Markets already priced at 99% or more for the winning side a full horizon before they closed are "
        "left out, so the easiest cases are not counted.",
        "- A model's score is only compared with the market on the markets the model answered.",
        "- A model gets no verdict until it has been scored on {MIN_VERDICT} markets. Markets that resolve "
        "together are not independent, so even then the \u00b1 understates the uncertainty.",
        "",
    ]
    return "\n".join(lines)


def _horizon_section(h: dict) -> list[str]:
    name = _horizon_name(h["horizon_days"])
    lines = [f"## Price {name} before the outcome", ""]
    if not h["market"]["n"]:
        return [*lines, "No resolved market has a price this far back yet.", ""]
    lines += [
        f"![Calibration curve, {name} before](calibration_{h['horizon_days']}d.png)",
        "",
        "| Price bucket | Markets | Average price | Resolved YES |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {b['low']:.0%} to {b['high']:.0%} | {b['n']} | {b['mean_probability']:.1%} | {b['yes_rate']:.1%} |"
        for b in h["buckets"]
    ]
    lines += [
        "",
        f"![Brier score by category, {name} before](brier_by_category_{h['horizon_days']}d.png)",
        "",
        "| Category | Markets | Resolved YES | Market Brier |",
        "|---|---|---|---|",
    ]
    lines += [f"| {c['category']} | {c['n']} | {c['yes_rate']:.0%} | {c['brier']:.4f} |" for c in h["categories"]]
    lines += ["", "### Models against the market", ""]
    if not h["models"]:
        return [*lines, "No models in the `models/` folder.", ""]
    lines += [
        "| Model | Markets | Model Brier | Market Brier | Gain per market | Verdict |",
        "|---|---|---|---|---|---|",
    ]
    for m in h["models"]:
        if m["n"]:
            lines.append(
                f"| {m['model']} | {m['n']} | {m['model_brier']:.4f} | {m['market_brier']:.4f} | "
                f"{m['gain']:+.4f} ± {m['gain_error']:.4f} | {m['verdict']} |"
            )
        else:
            lines.append(f"| {m['model']} | 0 | n/a | n/a | n/a | gave no answers |")
    return [
        *lines,
        "",
        "Gain is the market's squared error minus the model's, averaged per market: positive means "
        "the model was closer. The ± is one standard error.",
        "",
    ]


def summary_for_status(report: dict) -> dict:
    """The few numbers other parts of PolyBot read (kept small and stable)."""
    by_days = {h["horizon_days"]: h["market"] for h in report["horizons"]}
    return {
        "built_at": report["built_at"],
        **{f"brier_market_{d}d": by_days[d].get("brier") for d in by_days},
        **{f"markets_{d}d": by_days[d]["n"] for d in by_days},
    }


def write_report(report: dict, folder: Path | None = None) -> Path:
    """Write the Markdown, the charts and a small JSON summary. Returns the Markdown path."""
    from polybot import charts  # imported here so that scoring works even where matplotlib is not installed

    folder = folder or reports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for h in report["horizons"]:
        if h["market"]["n"]:
            charts.calibration_curve(h, folder / f"calibration_{h['horizon_days']}d.png")
            charts.brier_by_category(h, folder / f"brier_by_category_{h['horizon_days']}d.png")
    path = folder / "calibration.md"
    path.write_text(markdown(report), encoding="utf-8")
    rawstore.write_json_atomic(folder / "calibration.json", summary_for_status(report))
    return path


def console_summary(report: dict) -> str:
    """A few lines for the terminal and the job log."""
    parts = []
    for h in report["horizons"]:
        m = h["market"]
        name = _horizon_name(h["horizon_days"])
        parts.append(
            f"{name} before: {m['n']} markets, market Brier {m['brier']:.4f}"
            if m["n"]
            else f"{name} before: no markets yet"
        )
    return "; ".join(parts)


def main() -> int:
    """``polybot calibrate``: rebuild the report from what is in the warehouse."""
    report = build_report()
    path = write_report(report)
    print(console_summary(report))
    for h in report["horizons"]:
        for m in h["models"]:
            if m["n"]:
                print(
                    f"  {_horizon_name(h['horizon_days'])} before, {m['model']}: Brier {m['model_brier']:.4f} "
                    f"vs market {m['market_brier']:.4f} on {m['n']} markets ({m['verdict']})"
                )
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
