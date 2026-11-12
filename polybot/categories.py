"""Turn Polymarket's tags into one category per market.

Kept in its own small file because both the catalog and the snapshot store need
it, and neither should have to import the other.
"""

from __future__ import annotations

# Which of Polymarket's tags decides a market's category. A market carries many
# tags ("Politics", "Trump", "Middle East", ...), so the list is checked in
# order and the first category with a matching tag wins. Narrow categories come
# before broad ones: an Iran market tagged both "geopolitics" and "politics" is
# geopolitics, and a "Bitcoin" market tagged "finance" too is crypto.
CATEGORY_TAGS: tuple[tuple[str, frozenset[str]], ...] = (
    ("sports", frozenset({"sports", "esports", "football", "nfl", "nba", "mlb", "nhl", "soccer", "tennis"})),
    ("crypto", frozenset({"crypto", "crypto-prices"})),
    ("weather", frozenset({"weather"})),
    ("mentions", frozenset({"mentions"})),
    ("geopolitics", frozenset({"geopolitics"})),
    ("politics", frozenset({"politics", "elections", "world-elections", "global-elections"})),
    ("economics", frozenset({"economy", "economics", "fed", "fomc", "federal-reserve", "inflation"})),
    ("finance", frozenset({"finance", "business", "stocks", "commodities", "ipos"})),
    ("tech", frozenset({"tech", "ai"})),
    ("science", frozenset({"science", "space"})),
    ("culture", frozenset({"culture", "pop-culture", "movies", "music"})),
)


def category_from_tags(tag_slugs: tuple[str, ...]) -> str:
    """Pick one category for a market from its tag slugs; "other" when none match."""
    present = set(tag_slugs)
    for category, slugs in CATEGORY_TAGS:
        if present & slugs:
            return category
    return "other"


def tag_slugs(market: dict) -> tuple[str, ...]:
    """The tag slugs on a market as the API sends it (empty when it was fetched without tags)."""
    tags = market.get("tags") or []
    return tuple(str(t["slug"]).lower() for t in tags if isinstance(t, dict) and t.get("slug"))


def category_of(market: dict) -> str | None:
    """A market's category from its tags, or None when the response carried no tags at all."""
    slugs = tag_slugs(market)
    return category_from_tags(slugs) if slugs else None
