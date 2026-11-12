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
