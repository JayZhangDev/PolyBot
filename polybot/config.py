"""Settings from ``config.yaml``: markets to follow, the paper account, fees and the status file.

The file holds plain choices only (thresholds, categories, limits). Secrets
never go in it. Every setting has a default, so a missing file or a missing
line still works; a misspelled name or a value of the wrong kind is an error
with a message that says exactly what to fix, rather than a setting that is
silently ignored.
"""

from __future__ import annotations

import os
from collections.abc import Callable
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

# Polymarket's taker fee rates by category, from docs.polymarket.com/trading/fees (read 2026-11-11).
# The fee on a trade is shares x rate x price x (1 - price). Only the taker pays; a market's own
# rate from the API is used when it is known, and this table is the fallback.
DEFAULT_FEE_RATES = (
    ("crypto", 0.07),
    ("sports", 0.05),
    ("economics", 0.05),
    ("culture", 0.05),
    ("weather", 0.05),
    ("politics", 0.04),
    ("finance", 0.04),
    ("tech", 0.04),
    ("mentions", 0.04),
    ("geopolitics", 0.0),
)


class ConfigError(ValueError):
    """config.yaml has a setting PolyBot cannot use."""


@dataclass(frozen=True)
class Settings:
    """The values PolyBot runs with. The defaults here match the shipped config.yaml."""

    # --- which markets to list and stream
    min_liquidity: float = 10_000.0  # dollars of resting orders in the book
    min_volume: float = 50_000.0  # dollars traded over the market's life
    include_categories: tuple[str, ...] = ()  # empty means every category
    skip_categories: tuple[str, ...] = ()
    max_stream_markets: int = 300  # how many markets to stream live
    rank_by: str = "volume"  # which number decides "top" when cutting to the maximum
    flush_seconds: float = 2.0  # how often the streamer writes what it has collected

    # --- the paper account (fake money; no order is ever sent anywhere)
    paper_starting_balance: float = 1_000.0
    paper_model: str = "favorite_longshot"  # a file in models/, without ".py"
    paper_min_edge: float = 0.01  # buy when the model's probability beats the all-in cost by this much
    paper_exit_edge: float | None = 0.0  # sell when the edge has shrunk to this; None holds until the market resolves
    paper_stake_fraction: float = 0.02  # share of the account put into one trade
    paper_max_positions: int = 20
    paper_max_days_to_end: float = 60.0  # skip markets that close later than this
    paper_min_shares: float = 5.0  # Polymarket's smallest order

    # --- fees
    fee_default_rate: float = 0.05  # used for a category that is not in the table
    fee_rates: tuple[tuple[str, float], ...] = DEFAULT_FEE_RATES

    # --- the status file read by the Hong Kong island page
    status_path: str = "status/polybot.json"

    def fee_rate(self, category: str, market_rate: float | None = None) -> float:
        """The taker fee rate for a market: its own rate when the API gave one, else its category's."""
        if market_rate is not None:
            return market_rate
        return dict(self.fee_rates).get(category, self.fee_default_rate)


def config_path() -> Path:
    """``config.yaml`` in the project root, or the file named by ``POLYBOT_CONFIG``."""
    return Path(os.environ.get("POLYBOT_CONFIG", PROJECT_ROOT / "config.yaml"))


# ---------------------------------------------------------------------------
# Checks: each takes the setting's name and its value from the file, and returns
# the value in the type Settings uses or raises ConfigError saying what is wrong.
# ---------------------------------------------------------------------------
def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(name: str, value: object) -> float:
    if not _is_number(value) or value < 0:
        raise ConfigError(f"{name} must be a number that is zero or more, got {value!r}")
    return float(value)


def _between(low: float, high: float, what: str) -> Callable[[str, object], float]:
    def check(name: str, value: object) -> float:
        if not _is_number(value) or not low <= value <= high:
            raise ConfigError(f"{name} must be {what} between {low:g} and {high:g}, got {value!r}")
        return float(value)

    return check


def _positive(name: str, value: object) -> float:
    if not _is_number(value) or value <= 0:
        raise ConfigError(f"{name} must be a number above zero, got {value!r}")
    return float(value)


def _whole(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{name} must be a whole number of 1 or more, got {value!r}")
    return value


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be some text, got {value!r}")
    return value.strip()


def _ranking(name: str, value: object) -> str:
    if value not in RANKINGS:
        raise ConfigError(f"{name} must be one of {', '.join(RANKINGS)}, got {value!r}")
    return str(value)


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


def _exit_edge(name: str, value: object) -> float | None:
    """A number, or nothing at all (``exit_edge:`` left empty) to hold every position until it resolves."""
    return None if value is None else _between(-1, 1, "a number")(name, value)


def _fee_table(name: str, value: object) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must list a rate per category, for example 'crypto: 0.07', got {value!r}")
    rate = _between(0, 1, "a rate")
    table = []
    for category, number in value.items():
        if category not in CATEGORIES:
            raise ConfigError(f"{name} has an unknown category '{category}'; choose from {', '.join(CATEGORIES)}")
        table.append((category, rate(f"{name}.{category}", number)))
    return tuple(table)


# Where each setting lives in the YAML file: section -> {yaml name: (Settings field, check)}.
_LAYOUT: dict[str, dict[str, tuple[str, Callable[[str, object], object]]]] = {
    "filters": {
        "min_liquidity": ("min_liquidity", _number),
        "min_volume": ("min_volume", _number),
        "include_categories": ("include_categories", _categories),
        "skip_categories": ("skip_categories", _categories),
    },
    "stream": {
        "max_markets": ("max_stream_markets", _whole),
        "rank_by": ("rank_by", _ranking),
        "flush_seconds": ("flush_seconds", _between(0.5, 60, "a number of seconds")),
    },
    "paper": {
        "starting_balance": ("paper_starting_balance", _positive),
        "model": ("paper_model", _text),
        "min_edge": ("paper_min_edge", _between(0, 1, "a number")),
        "exit_edge": ("paper_exit_edge", _exit_edge),
        "stake_fraction": ("paper_stake_fraction", _between(0.001, 1, "a share")),
        "max_positions": ("paper_max_positions", _whole),
        "max_days_to_end": ("paper_max_days_to_end", _positive),
        "min_shares": ("paper_min_shares", _number),
    },
    "fees": {
        "default_taker_rate": ("fee_default_rate", _between(0, 1, "a rate")),
        "taker_rate_by_category": ("fee_rates", _fee_table),
    },
    "status": {"path": ("status_path", _text)},
}


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
            field, check = _LAYOUT[section][key]
            values[field] = check(f"{section}.{key}", value)
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
