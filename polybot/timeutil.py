"""Timestamp parsing for the formats the Gamma API and SQLite rows use."""
from __future__ import annotations

import re
from datetime import datetime, timezone

_SHORT_OFFSET = re.compile(r"([+-]\d{2})$")
_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(value: str | None) -> datetime | None:
    """Parse '2026-01-01T00:00:00Z', '2026-01-01 00:00:00+00', ISO with fractions, etc.

    Returns an aware UTC datetime, or None when the value is empty or unparseable.
    Naive values are assumed to be UTC.
    """
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    s = _SHORT_OFFSET.sub(r"\1:00", s)
    # Python 3.10's fromisoformat wants exactly 3 or 6 fractional digits.
    s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
