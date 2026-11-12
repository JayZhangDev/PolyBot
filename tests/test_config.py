"""config.yaml: defaults, real values, and clear errors for mistakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from polybot.config import ConfigError, Settings, load_settings

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def write(tmp_path, text: str) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_means_defaults(tmp_path):
    assert load_settings(tmp_path / "nope.yaml") == Settings()


def test_empty_file_means_defaults(tmp_path):
    assert load_settings(write(tmp_path, "# nothing set\n")) == Settings()


def test_shipped_config_matches_the_documented_defaults():
    settings = load_settings(REPO_CONFIG)
    assert settings == Settings()
    assert settings.max_stream_markets == 300 and settings.rank_by == "volume"


def test_values_are_read_from_their_sections(tmp_path):
    settings = load_settings(
        write(
            tmp_path,
            """
filters:
  min_liquidity: 2500
  min_volume: 0
  include_categories: [Politics, crypto]
stream:
  max_markets: 50
  rank_by: volume_24h
  flush_seconds: 5
""",
        )
    )
    assert settings.min_liquidity == 2500.0 and settings.min_volume == 0.0
    assert settings.include_categories == ("politics", "crypto")  # names are not case-sensitive
    assert settings.max_stream_markets == 50 and settings.rank_by == "volume_24h" and settings.flush_seconds == 5.0
    assert settings.skip_categories == ()  # left out, so the default applies


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("filtres:\n  min_volume: 1\n", "unknown section 'filtres'"),
        ("filters:\n  min_volum: 1\n", "unknown setting 'filters.min_volum'"),
        ("filters:\n  min_volume: lots\n", "filters.min_volume must be a number"),
        ("filters:\n  min_liquidity: -5\n", "filters.min_liquidity must be a number"),
        ("filters:\n  skip_categories: sports\n", "must be a list of category names"),
        ("filters:\n  skip_categories: [footy]\n", "unknown categories ['footy']"),
        ("filters:\n  include_categories: [sports]\n  skip_categories: [sports]\n", "appear in both"),
        ("stream:\n  max_markets: 0\n", "stream.max_markets must be a whole number"),
        ("stream:\n  max_markets: 2.5\n", "stream.max_markets must be a whole number"),
        ("stream:\n  rank_by: vibes\n", "stream.rank_by must be one of"),
        ("stream:\n  flush_seconds: 0\n", "stream.flush_seconds must be a number of seconds between 0.5 and 60"),
        ("stream:\n  flush_seconds: fast\n", "stream.flush_seconds must be a number of seconds"),
        ("stream: 300\n", "section 'stream' must contain settings"),
        ("- just\n- a list\n", "must be a set of sections"),
        ("filters: [unclosed\n", "is not valid YAML"),
    ],
)
def test_mistakes_are_reported_by_name(tmp_path, text, message):
    with pytest.raises(ConfigError) as error:
        load_settings(write(tmp_path, text))
    assert message in str(error.value)


# --------------------------- paper account, fees and the status file ---------------------------


def test_paper_fee_and_status_settings_are_read(tmp_path):
    settings = load_settings(
        write(
            tmp_path,
            """
paper:
  starting_balance: 2500
  model: momentum_24h
  min_edge: 0.02
  exit_edge:
  stake_fraction: 0.05
  max_positions: 8
  max_days_to_end: 30
  min_shares: 10
fees:
  default_taker_rate: 0.06
  taker_rate_by_category:
    crypto: 0.08
    geopolitics: 0
status:
  path: C:\\Users\\me\\page\\polybot.json
""",
        )
    )
    assert (settings.paper_starting_balance, settings.paper_model, settings.paper_min_edge) == (
        2500.0,
        "momentum_24h",
        0.02,
    )
    assert settings.paper_exit_edge is None  # left empty: hold until the market resolves
    assert (settings.paper_stake_fraction, settings.paper_max_positions, settings.paper_min_shares) == (0.05, 8, 10.0)
    assert settings.fee_rate("crypto") == 0.08 and settings.fee_rate("geopolitics") == 0.0
    assert settings.fee_rate("sports") == 0.06  # not in the table: the default
    assert settings.fee_rate("crypto", market_rate=0.03) == 0.03  # a market's own rate always wins
    assert settings.status_path.endswith("polybot.json")


def test_default_fee_table_matches_polymarkets_published_rates():
    s = Settings()
    assert (s.paper_starting_balance, s.paper_model, s.paper_max_positions) == (1000.0, "favorite_longshot", 20)
    assert (s.fee_rate("crypto"), s.fee_rate("politics"), s.fee_rate("geopolitics")) == (0.07, 0.04, 0.0)
    assert s.fee_rate("science") == 0.05


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("paper:\n  starting_balance: 0\n", "paper.starting_balance must be a number above zero"),
        ("paper:\n  model: ''\n", "paper.model must be some text"),
        ("paper:\n  min_edge: 5\n", "paper.min_edge must be a number between 0 and 1"),
        ("paper:\n  stake_fraction: 2\n", "paper.stake_fraction must be a share between 0.001 and 1"),
        ("paper:\n  max_positions: 2.5\n", "paper.max_positions must be a whole number"),
        ("fees:\n  default_taker_rate: 7\n", "fees.default_taker_rate must be a rate between 0 and 1"),
        ("fees:\n  taker_rate_by_category: 0.05\n", "must list a rate per category"),
        ("fees:\n  taker_rate_by_category:\n    footy: 0.05\n", "unknown category 'footy'"),
        ("fees:\n  taker_rate_by_category:\n    crypto: lots\n", "fees.taker_rate_by_category.crypto must be a rate"),
        ("status:\n  path: 5\n", "status.path must be some text"),
        ("paper:\n  leverage: 10\n", "unknown setting 'paper.leverage'"),
    ],
)
def test_paper_and_fee_mistakes_are_reported_by_name(tmp_path, text, message):
    with pytest.raises(ConfigError) as error:
        load_settings(write(tmp_path, text))
    assert message in str(error.value)
