import sqlite3

from conftest import make_market

from polybot.db import connect
from polybot.storage import counts, init_db, save_snapshot


def test_save_snapshot_stores_book_and_event(temp_db):
    save_snapshot([make_market("7", yes=0.4, bid=0.39, ask=0.41, event_id="e1", neg_risk=True)])
    with connect() as c:
        m = c.execute("SELECT event_id, neg_risk FROM markets WHERE id='7'").fetchone()
        s = c.execute("SELECT yes_price, no_price, best_bid, best_ask FROM snapshots").fetchone()
    assert (m["event_id"], m["neg_risk"]) == ("e1", 1)
    assert (s["yes_price"], s["best_bid"], s["best_ask"]) == (0.4, 0.39, 0.41)
    assert counts() == {"markets": 1, "snapshots": 1}


def test_upsert_keeps_one_market_row_per_id(temp_db):
    save_snapshot([make_market("7")])
    save_snapshot([make_market("7")])
    assert counts() == {"markets": 1, "snapshots": 2}


def test_migrates_a_v0_1_database_without_losing_rows(temp_db):
    # Build the original v0.1 schema by hand with one snapshot in it.
    with sqlite3.connect(temp_db) as c:
        c.executescript("""
        CREATE TABLE markets (id TEXT PRIMARY KEY, question TEXT NOT NULL, slug TEXT,
            category TEXT, end_date TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            raw_json TEXT);
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL,
            captured_at TEXT NOT NULL, volume REAL, liquidity REAL, yes_price REAL, no_price REAL);
        INSERT INTO markets VALUES ('old', 'Old market?', NULL, NULL, NULL, 'x', 'x', '{}');
        INSERT INTO snapshots (market_id, captured_at, yes_price) VALUES ('old', 'x', 0.2);
        """)
    init_db()
    with connect() as c:
        snap_cols = {r["name"] for r in c.execute("PRAGMA table_info(snapshots)")}
        market_cols = {r["name"] for r in c.execute("PRAGMA table_info(markets)")}
    assert {"best_bid", "best_ask"} <= snap_cols
    assert {"event_id", "neg_risk"} <= market_cols
    assert counts() == {"markets": 1, "snapshots": 1}


def test_skips_markets_without_id(temp_db):
    m = make_market("1")
    del m["id"]
    assert save_snapshot([m]) == 0


def test_category_comes_from_tags_and_survives_a_later_fetch_without_them():
    from polybot.db import connect

    market = make_market("9")
    save_snapshot([{**market, "tags": [{"slug": "geopolitics"}, {"slug": "politics"}]}])
    save_snapshot([market])  # fetched again, this time with no tags
    with connect() as c:
        assert c.execute("SELECT category FROM markets WHERE id = '9'").fetchone()[0] == "geopolitics"
