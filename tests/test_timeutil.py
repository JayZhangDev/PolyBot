from datetime import datetime, timezone

import pytest

from polybot.timeutil import parse_ts

UTC = timezone.utc


@pytest.mark.parametrize("raw", [
    "2026-01-02T03:04:05Z",
    "2026-01-02 03:04:05+00",
    "2026-01-02T03:04:05+00:00",
    "2026-01-02T03:04:05",
])
def test_parses_gamma_and_iso_formats(raw):
    assert parse_ts(raw) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_handles_odd_fraction_lengths():
    assert parse_ts("2026-01-02T03:04:05.12Z").microsecond == 120000


def test_converts_offsets_to_utc():
    assert parse_ts("2026-01-02T05:00:00+02:00") == datetime(2026, 1, 2, 3, tzinfo=UTC)


@pytest.mark.parametrize("raw", [None, "", "not a date"])
def test_bad_values_return_none(raw):
    assert parse_ts(raw) is None
