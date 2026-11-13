"""The calibration tracker: which price counts, how it is scored, and how models are compared."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from polybot import cli, rollup, tracker, warehouse
from polybot.db import connect
from polybot.storage import init_db
from polybot.tracker import Model, Sample, calibration_buckets, score, score_by_category, score_model

RESOLVED = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
REPO_MODELS = Path(__file__).resolve().parent.parent / "models"


def add_market(
    market_id: str,
    outcome: str | None,
    prices: list[tuple[float, float]],
    *,
    category: str | None = "politics",
    end_in_days: float = 0.0,
) -> None:
    """Record a market in SQLite: ``prices`` is a list of (days before it resolved, YES price)."""
    init_db()
    with connect() as c:
        c.execute(
            "INSERT INTO markets (id, question, slug, category, end_date, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                market_id,
                f"Will {market_id} happen?",
                f"m-{market_id}",
                category,
                (RESOLVED + timedelta(days=end_in_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                RESOLVED.isoformat(),
                RESOLVED.isoformat(),
            ),
        )
        for days_before, price in prices:
            c.execute(
                "INSERT INTO snapshots (market_id, captured_at, yes_price, no_price, best_bid, best_ask, "
                "volume, liquidity) "
                "VALUES (?, ?, ?, ?, ?, ?, 1000, 500)",
                (
                    market_id,
                    (RESOLVED - timedelta(days=days_before)).isoformat(),
                    price,
                    1 - price,
                    round(price - 0.01, 4),
                    round(price + 0.01, 4),
                ),
            )
        if outcome:
            c.execute(
                "INSERT INTO resolutions (market_id, outcome, resolved_at, last_checked) VALUES (?, ?, ?, ?)",
                (market_id, outcome, RESOLVED.isoformat(), RESOLVED.isoformat()),
            )


def to_warehouse() -> None:
    con = warehouse.connect()
    try:
        rollup.copy_snapshots(con)
        rollup.copy_markets(con)
    finally:
        con.close()


def one(horizon: int) -> Sample:
    to_warehouse()
    (sample,) = tracker.load_samples(horizon)
    return sample


def sample(price: float, outcome: int, category: str = "politics", change: float | None = None) -> Sample:
    return Sample("m", "Q?", category, outcome, 1, price, RESOLVED, None, None, None, None, 3.0, change)


# --------------------------- which price is used ---------------------------


def test_uses_the_last_price_at_least_one_day_before():
    add_market("1", "YES", [(3.0, 0.20), (1.5, 0.40), (0.5, 0.90)])
    s = one(1)
    assert (s.price, s.outcome, s.category, s.question) == (0.40, 1, "politics", "Will 1 happen?")
    assert s.price_time == datetime(2026, 11, 9, 0, 0) and (s.best_bid, s.best_ask) == (0.39, 0.41)


def test_one_week_horizon_needs_a_price_from_a_week_before():
    add_market("1", "NO", [(7.5, 0.30), (1.5, 0.10)])
    to_warehouse()
    assert tracker.load_samples(7)[0].price == 0.30 and tracker.load_samples(7)[0].outcome == 0
    assert tracker.load_samples(1)[0].price == 0.10


def test_stale_price_is_not_used():
    add_market("1", "YES", [(5.0, 0.20)])  # nothing recorded in the day before the cut-off
    to_warehouse()
    assert tracker.load_samples(1) == []


def test_prices_after_the_market_was_effectively_decided_do_not_count():
    # At 2.5 days out the price was already 99.5% for the winning side, so that is when it was "decided".
    add_market("1", "YES", [(4.0, 0.60), (2.5, 0.995), (1.2, 0.999)])
    assert one(1).price == 0.60


def test_void_and_unresolved_markets_are_left_out():
    add_market("void", "VOID", [(1.5, 0.50)])
    add_market("open", None, [(1.5, 0.50)])
    add_market("ok", "NO", [(1.5, 0.50)])
    to_warehouse()
    assert [s.market_id for s in tracker.load_samples(1)] == ["ok"]
    assert tracker.data_window()["void"] == 1 and tracker.data_window()["resolved"] == 1


def test_change_over_the_previous_day_and_days_left_are_given():
    add_market("1", "YES", [(2.6, 0.40), (1.5, 0.50)], end_in_days=2.0)
    s = one(1)
    assert s.change_24h == pytest.approx(0.10) and s.days_to_end == pytest.approx(3.5)


def test_no_earlier_price_means_no_change_figure():
    add_market("1", "YES", [(1.5, 0.50)])
    assert one(1).change_24h is None


def test_market_with_no_category_counts_as_other():
    add_market("1", "YES", [(1.5, 0.50)], category=None)
    assert one(1).category == "other"


# --------------------------- scoring ---------------------------


def test_brier_score_of_known_forecasts():
    result = score([0.9, 0.2, 0.5, 0.5], [1, 0, 1, 0])
    assert result["n"] == 4 and result["yes_rate"] == 0.5
    assert result["brier"] == pytest.approx((0.01 + 0.04 + 0.25 + 0.25) / 4)
    assert result["skill_vs_base_rate"] == pytest.approx(1 - 0.1375 / 0.25)
    assert score([], []) == {"n": 0}


def test_buckets_compare_average_price_with_how_often_yes_happened():
    buckets = calibration_buckets([0.05, 0.08, 0.92, 1.0], [0, 1, 1, 1])
    assert [(b["low"], b["high"], b["n"]) for b in buckets] == [(0.0, 0.1, 2), (0.9, 1.0, 2)]
    assert buckets[0]["mean_probability"] == pytest.approx(0.065) and buckets[0]["yes_rate"] == 0.5


def test_small_categories_are_pooled_and_big_ones_listed_by_size():
    samples = [sample(0.5, 1, "politics")] * 6 + [sample(0.5, 0, "sports")] * 8 + [sample(0.2, 0, "weather")] * 2
    rows = score_by_category(samples)
    assert [(r["category"], r["n"]) for r in rows] == [("sports", 8), ("politics", 6), ("(smaller categories)", 2)]


# --------------------------- models ---------------------------


def always(value: object) -> Model:
    return Model("fixed", "always the same answer", lambda market: value)


def test_model_sees_what_was_known_and_never_the_outcome():
    view = tracker.market_view(sample(0.3, 1, change=0.05))
    assert view["price"] == 0.3 and view["change_24h"] == 0.05 and view["days_to_end"] == 3.0
    assert "outcome" not in view and 1 not in view.values()


def test_model_that_is_closer_than_the_market_shows_a_positive_gain():
    samples = [sample(0.6, 1)] * 120
    result = score_model(Model("bold", "", lambda m: 0.9), samples)
    assert (
        result["n"] == 120
        and result["model_brier"] == pytest.approx(0.01)
        and result["market_brier"] == pytest.approx(0.16)
    )
    assert result["gain"] == pytest.approx(0.15) and result["verdict"] == "ahead of the market so far"
    assert score_model(Model("timid", "", lambda m: 0.5), samples)["verdict"] == "behind the market so far"


def test_model_that_repeats_the_price_scores_exactly_like_the_market():
    samples = [sample(0.3, 0), sample(0.7, 1)] * 60
    result = score_model(Model("copycat", "", lambda m: m["price"]), samples)
    assert result["gain"] == 0 and result["verdict"] == "no clear difference"


def test_few_markets_never_earn_a_verdict():
    assert score_model(Model("bold", "", lambda m: 0.9), [sample(0.6, 1)] * 99)["verdict"] == "too few markets to say"


@pytest.mark.parametrize("answer", [None, "0.5", 1.5, -0.1, float("nan"), True])
def test_answers_that_are_not_probabilities_are_skipped(answer):
    result = score_model(always(answer), [sample(0.6, 1)] * 3)
    assert (result["n"], result["skipped"]) == (0, 3) and "model_brier" not in result


def test_model_that_crashes_is_skipped_for_that_market():
    def predict(market: dict) -> float:
        return 1 / (market["change_24h"] or 0)

    result = score_model(Model("fragile", "", predict), [sample(0.6, 1, change=None), sample(0.6, 1, change=2.0)])
    assert (result["n"], result["skipped"]) == (1, 1)


def test_models_are_loaded_from_files_and_bad_files_are_skipped(tmp_path, caplog):
    (tmp_path / "good.py").write_text(
        'NAME = "good"\nDESCRIPTION = " An idea. "\n\ndef predict(market):\n    return 0.5\n', encoding="utf-8"
    )
    (tmp_path / "_draft.py").write_text('NAME = "draft"\ndef predict(market):\n    return 0.1\n', encoding="utf-8")
    (tmp_path / "broken.py").write_text("this is not python", encoding="utf-8")
    (tmp_path / "no_name.py").write_text("def predict(market):\n    return 0.5\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        models = tracker.load_models(tmp_path)
    assert [(m.name, m.description) for m in models] == [("good", "An idea.")]
    assert "broken.py" in caplog.text and "no_name.py" in caplog.text and "_draft" not in caplog.text
    assert tracker.load_models(tmp_path / "missing") == []


def test_shipped_models_load_and_behave_as_described():
    models = {m.name: m for m in tracker.load_models(REPO_MODELS)}
    assert sorted(models) == ["favorite_longshot", "momentum_24h"]
    longshot, momentum = models["favorite_longshot"].predict, models["momentum_24h"].predict
    assert longshot({"price": 0.10}) < 0.10 and longshot({"price": 0.90}) > 0.90
    assert longshot({"price": 0.50}) == pytest.approx(0.50) and longshot({"price": 1.0}) is None
    assert momentum({"price": 0.40, "change_24h": 0.10}) == pytest.approx(0.45)
    assert momentum({"price": 0.40, "change_24h": None}) is None
    assert momentum({"price": 0.98, "change_24h": 0.20}) == 0.99


# --------------------------- the report ---------------------------


def history() -> None:
    """Twelve resolved markets with a price one day out; half also have one a week out."""
    for i in range(12):
        yes = i % 3 == 0
        prices = [(1.5, 0.80 if yes else 0.15), (2.6, 0.70 if yes else 0.20)]
        if i % 2 == 0:
            prices.append((7.5, 0.55))
        add_market(str(i), "YES" if yes else "NO", prices, category="politics" if i < 7 else "sports")


def test_report_files_are_written(monkeypatch):
    monkeypatch.setenv("POLYBOT_MODELS_DIR", str(REPO_MODELS))
    history()
    to_warehouse()
    report = tracker.build_report()
    path = tracker.write_report(report)
    folder = path.parent
    assert sorted(p.name for p in folder.iterdir()) == [
        "brier_by_category_1d.png",
        "brier_by_category_7d.png",
        "calibration.json",
        "calibration.md",
        "calibration_1d.png",
        "calibration_7d.png",
    ]
    for png in folder.glob("*.png"):
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 5000
    text = path.read_text(encoding="utf-8")
    assert "| 1 day before | 12 | 33% |" in text and "| 7 days before | 6 |" in text
    assert "| politics | 7 |" in text and "| sports | 5 |" in text
    assert "| favorite_longshot | 12 |" in text and "too few markets to say" in text
    assert "![Calibration curve, 1 day before](calibration_1d.png)" in text
    assert "scored on 100 markets" in text and "{" not in text  # every placeholder was filled in
    summary = tracker.summary_for_status(report)
    assert summary["markets_1d"] == 12 and summary["markets_7d"] == 6 and 0 < summary["brier_market_1d"] < 0.25


def test_report_on_an_empty_warehouse_says_so_and_draws_nothing(capsys):
    assert cli.main(["calibrate"]) == 0
    assert "1 day before: no markets yet; 7 days before: no markets yet" in capsys.readouterr().out
    files = sorted(p.name for p in tracker.reports_dir().iterdir())
    assert files == ["calibration.json", "calibration.md"]
    assert "No price history in the warehouse yet" in (tracker.reports_dir() / "calibration.md").read_text(
        encoding="utf-8"
    )


def test_horizon_with_no_markets_is_reported_plainly():
    add_market("1", "YES", [(1.5, 0.60), (2.6, 0.50)])
    to_warehouse()
    text = tracker.markdown(tracker.build_report())
    assert "| 7 days before | 0 | n/a | n/a | n/a |" in text
    assert "No resolved market has a price this far back yet." in text
    assert "No models in the `models/` folder." in text
