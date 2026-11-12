"""Settings from ``config.yaml``: which markets PolyBot keeps track of and streams.

The file holds plain choices only (thresholds, categories, limits). Secrets
never go in it. Every setting has a default, so a missing file or a missing
line still works; a misspelled name or a value of the wrong kind is an error
with a message that says exactly what to fix, rather than a setting that is
silently ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from polybot.db import PROJECT_ROOT

# Polymarket's own top-level groupings, plus "other" for anything that fits none.
CATEGORIES = (
    "politics",
    "geopolitics",
    "sports",
    "crypto",
    "finance",
    "economics",
    "tech",
    "culture",
    "weather",
    "science",
    "mentions",
    "other",
)
RANKINGS = ("volume", "volume_24h", "liquidity")


class ConfigError(ValueError):
    """config.yaml has a setting PolyBot cannot use."""


@dataclass(frozen=True)
class Settings:
    """The values PolyBot runs with. The defaults here match the shipped config.yaml."""

    min_liquidity: float = 10_000.0  # dollars of resting orders in the book
    min_volume: float = 50_000.0  # dollars traded over the market's life
    include_categories: tuple[str, ...] = ()  # empty means every category
    skip_categories: tuple[str, ...] = ()
    max_stream_markets: int = 300  # how many markets to stream live
    rank_by: str = "volume"  # which number decides "top" when cutting to the maximum
    flush_seconds: float = 2.0  # how often the streamer writes what it has collected


# Where each setting lives in the YAML file: section -> {yaml name: Settings field}.
_LAYOUT = {
    "filters": {
        "min_liquidity": "min_liquidity",
        "min_volume": "min_volume",
        "include_categories": "include_categories",
        "skip_categories": "skip_categories",
    },
    "stream": {"max_markets": "max_stream_markets", "rank_by": "rank_by", "flush_seconds": "flush_seconds"},
}


def config_path() -> Path:
    """``config.yaml`` in the project root, or the file named by ``POLYBOT_CONFIG``."""
    return Path(os.environ.get("POLYBOT_CONFIG", PROJECT_ROOT / "config.yaml"))


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{name} must be a number that is zero or more, got {value!r}")
    return float(value)


def _categories(name: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{name} must be a list of category names such as [politics, crypto], got {value!r}")
    cleaned = tuple(v.strip().lower() for v in value)
    unknown = [v for v in cleaned if v not in CATEGORIES]
    if unknown:
        raise ConfigError(f"{name} has unknown categories {unknown}; choose from {', '.join(CATEGORIES)}")
    return cleaned


def _convert(field: str, name: str, value: object) -> object:
    """Check one value from the file and turn it into the type its Settings field uses."""
    if field in ("min_liquidity", "min_volume"):
        return _number(name, value)
    if field in ("include_categories", "skip_categories"):
        return _categories(name, value)
    if field == "max_stream_markets":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"{name} must be a whole number of 1 or more, got {value!r}")
        return value
    if field == "flush_seconds":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.5 <= value <= 60:
            raise ConfigError(f"{name} must be a number of seconds between 0.5 and 60, got {value!r}")
        return float(value)
    if value not in RANKINGS:
        raise ConfigError(f"{name} must be one of {', '.join(RANKINGS)}, got {value!r}")
    return value


def parse_settings(raw: object) -> Settings:
    """Turn the parsed YAML (a dict of sections) into :class:`Settings`."""
    if raw is None:
        return Settings()
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a set of sections such as 'filters:' and 'stream:'")
    values: dict[str, object] = {}
    for section, body in raw.items():
        if section not in _LAYOUT:
            raise ConfigError(f"unknown section '{section}'; expected one of {', '.join(_LAYOUT)}")
        if not isinstance(body, dict):
            raise ConfigError(f"section '{section}' must contain settings, one per line")
        for key, value in body.items():
            if key not in _LAYOUT[section]:
                raise ConfigError(f"unknown setting '{section}.{key}'; expected one of {', '.join(_LAYOUT[section])}")
            field = _LAYOUT[section][key]
            values[field] = _convert(field, f"{section}.{key}", value)
    settings = Settings(**values)  # type: ignore[arg-type]
    both = sorted(set(settings.include_categories) & set(settings.skip_categories))
    if both:
        raise ConfigError(f"{both} appear in both include_categories and skip_categories; pick one")
    return settings


def load_settings(path: Path | None = None) -> Settings:
    """Read the settings file. A missing file means "use every default"."""
    path = path or config_path()
    if not path.exists():
        return Settings()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    return parse_settings(raw)
