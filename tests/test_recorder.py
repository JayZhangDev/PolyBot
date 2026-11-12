"""The recorder: websocket messages in, book_top and trade rows out. No network, no files."""

from __future__ import annotations

import json

from conftest import load_fixture

from polybot.recorder import BOOK_COLUMNS, TRADE_COLUMNS, Recorder, StreamMarket, iso_ms, parse_frame

REAL = load_fixture("ws_market_messages.json")
T0 = 1_794_451_120_000  # 2026-11-12T02:38:40.000Z in the feed's milliseconds
MARKETS = [
    StreamMarket("m1", "Will one happen?", "politics", "Y1", "N1"),
    StreamMarket("m2", "Will two happen?", "sports", "Y2", "N2"),
]


def book(token: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]], ts: int = T0) -> dict:
    return {
        "event_type": "book",
        "asset_id": token,
        "market": "0xcond",
        "timestamp": str(ts),
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


def change(ts: int, *entries: tuple[str, str, str, str]) -> dict:
    return {
        "event_type": "price_change",
        "market": "0xcond",
        "timestamp": str(ts),
        "price_changes": [{"asset_id": t, "side": side, "price": p, "size": s} for t, side, p, s in entries],
    }


def trade(token: str, price: str, size: str, ts: int = T0, side: str = "BUY") -> dict:
    return {
        "event_type": "last_trade_price",
        "asset_id": token,
        "market": "0xcond",
        "price": price,
        "size": size,
        "side": side,
        "fee_rate_bps": "0",
        "timestamp": str(ts),
        "transaction_hash": "0xabc",
    }


def recorder() -> Recorder:
    r = Recorder()
    r.set_selection(MARKETS)
    return r


def as_dict(row: tuple) -> dict:
    return dict(zip(BOOK_COLUMNS, row, strict=True))


# --------------------------- frames and time ---------------------------


def test_frame_can_be_one_event_or_a_list():
    assert parse_frame('{"event_type": "book"}') == [{"event_type": "book"}]
    assert parse_frame('[{"event_type": "book"}, {"event_type": "price_change"}]')[1] == {"event_type": "price_change"}
    assert parse_frame(b'{"event_type": "book"}') == [{"event_type": "book"}]


def test_pong_and_junk_frames_are_ignored():
    assert parse_frame("PONG") == [] and parse_frame("") == [] and parse_frame("not json") == []
    assert parse_frame("[1, 2]") == [] and parse_frame('"text"') == []


def test_feed_time_becomes_iso_utc_with_milliseconds():
    assert iso_ms(1_794_451_123_376) == "2026-11-12T02:38:43.376Z"
    assert iso_ms(1_794_451_123_007).endswith(".007Z")


# --------------------------- the book ---------------------------


def test_snapshot_of_the_yes_token_becomes_one_row():
    r = recorder()
    r.handle(book("Y1", [("0.41", "75"), ("0.42", "50")], [("0.44", "10")]))
    (row,), trades = r.drain()
    d = as_dict(row)
    assert (d["ts"], d["market_id"]) == ("2026-11-12T02:38:40.000Z", "m1")
    assert (d["best_bid"], d["best_ask"], d["spread"], d["bid_depth"], d["ask_depth"]) == (
        0.42,
        0.44,
        0.02,
        125.0,
        10.0,
    )
    assert (d["bid_px_1"], d["bid_sz_1"], d["bid_px_2"], d["bid_sz_2"]) == (0.42, 50.0, 0.41, 75.0)
    assert d["bid_px_3"] == "" and d["ask_px_2"] == "" and trades == []  # missing levels are blank


def test_the_no_tokens_book_is_not_recorded_because_it_mirrors_yes():
    r = recorder()
    r.handle(book("N1", [("0.56", "10")], [("0.58", "50")]))
    assert r.drain() == ([], [])


def test_level_change_updates_the_book_and_only_the_yes_side_counts():
    r = recorder()
    r.handle(book("Y1", [("0.42", "50")], [("0.44", "10")]))
    r.drain()
    # The feed sends each change twice: once for YES and once, mirrored, for NO.
    r.handle(change(T0 + 500, ("Y1", "BUY", "0.43", "30"), ("N1", "SELL", "0.57", "30")))
    (row,), _ = r.drain()
    d = as_dict(row)
    assert (d["ts"], d["best_bid"], d["bid_sz_1"], d["spread"]) == ("2026-11-12T02:38:40.500Z", 0.43, 30.0, 0.01)


def test_many_changes_between_flushes_give_one_row_with_the_latest_state():
    r = recorder()
    r.handle(book("Y1", [("0.42", "50")], [("0.44", "10")]))
    for n in range(1, 6):
        r.handle(change(T0 + n, ("Y1", "BUY", "0.42", str(50 + n))))
    (row,), _ = r.drain()
    assert as_dict(row)["bid_sz_1"] == 55.0 and as_dict(row)["ts"].endswith(".005Z")


def test_change_below_the_recorded_depth_writes_nothing():
    r = recorder()
    r.handle(book("Y1", [(f"0.{n}", "10") for n in range(40, 50)], [("0.60", "5")]))
    r.drain()
    r.handle(change(T0 + 1, ("Y1", "BUY", "0.40", "999")))  # the tenth-best bid: not in the top five
    assert r.drain() == ([], [])
    r.handle(change(T0 + 2, ("Y1", "BUY", "0.49", "11")))  # the best bid: recorded
    assert len(r.drain()[0]) == 1


def test_change_that_arrives_before_any_snapshot_is_skipped():
    r = recorder()
    r.handle(change(T0, ("Y1", "BUY", "0.43", "30")))
    assert r.drain() == ([], []) and r.counts["change_before_snapshot"] == 1


def test_each_market_gets_its_own_row():
    r = recorder()
    r.handle(book("Y1", [("0.42", "50")], []))
    r.handle(book("Y2", [("0.10", "5")], []))
    assert sorted(as_dict(row)["market_id"] for row in r.drain()[0]) == ["m1", "m2"]


def test_stale_snapshot_time_is_lifted_to_when_we_subscribed():
    r = recorder()
    r.snapshot_floor_ms = T0 + 60_000
    r.handle(book("Y1", [("0.42", "50")], [], ts=T0 - 86_400_000))  # the book last changed a day ago
    assert as_dict(r.drain()[0][0])["ts"] == "2026-11-12T02:39:40.000Z"


def test_reconnect_forgets_books_so_old_levels_cannot_linger():
    r = recorder()
    r.handle(book("Y1", [("0.42", "50")], [("0.44", "10")]))
    r.drain()
    r.reset_books()
    r.handle(change(T0 + 1, ("Y1", "BUY", "0.43", "30")))
    assert r.drain() == ([], [])  # no snapshot yet on the new connection
    r.handle(book("Y1", [("0.30", "5")], [("0.44", "10")], ts=T0 + 2))
    assert as_dict(r.drain()[0][0])["best_bid"] == 0.30


# --------------------------- trades ---------------------------


def test_trades_on_either_token_are_recorded_with_the_outcome():
    r = recorder()
    r.handle(trade("Y1", "0.43", "12.5"))
    r.handle(trade("N2", "0.90", "3", ts=T0 + 250, side="SELL"))
    _, trades = r.drain()
    assert dict(zip(TRADE_COLUMNS, trades[0], strict=True)) == {
        "ts": "2026-11-12T02:38:40.000Z",
        "market_id": "m1",
        "outcome": 0,
        "price": 0.43,
        "size": 12.5,
        "side": "BUY",
        "fee_rate_bps": "0",
        "tx_hash": "0xabc",
    }
    assert trades[1][1:6] == ("m2", 1, 0.90, 3.0, "SELL")
    assert r.drain() == ([], [])  # handed over once, not twice


def test_messages_for_tokens_we_did_not_select_are_ignored():
    r = recorder()
    r.handle(trade("SOMEONE-ELSE", "0.5", "1"))
    r.handle(book("SOMEONE-ELSE", [("0.5", "1")], []))
    assert r.drain() == ([], [])


def test_malformed_message_is_counted_and_never_raises():
    r = recorder()
    r.handle({"event_type": "book", "asset_id": "Y1", "timestamp": "not a number", "bids": [], "asks": []})
    r.handle({"event_type": "last_trade_price", "asset_id": "Y1"})
    r.handle({"event_type": "tick_size_change", "asset_id": "Y1"})
    r.handle({"no": "type"})
    assert r.counts["malformed"] == 2 and r.drain() == ([], [])


# --------------------------- selection ---------------------------


def test_tokens_cover_both_sides_of_every_market():
    assert recorder().tokens == ["Y1", "N1", "Y2", "N2"]


def test_selection_change_reports_tokens_to_add_and_remove():
    r = recorder()
    r.handle(book("Y1", [("0.42", "50")], []))
    added, removed = r.set_selection([MARKETS[1], StreamMarket("m3", "Three?", "crypto", "Y3", "N3")])
    assert (added, removed) == (["N3", "Y3"], ["N1", "Y1"])
    r.handle(change(T0 + 1, ("Y1", "BUY", "0.43", "30")))
    assert r.drain() == ([], [])  # m1 is gone, with its book


# --------------------------- real messages ---------------------------


def real_recorder() -> Recorder:
    r = Recorder()
    yes_change, no_change = REAL["price_change"]["price_changes"]
    r.set_selection(
        [
            StreamMarket("book-market", "?", "other", REAL["book"]["asset_id"], "unused-1"),
            StreamMarket("change-market", "?", "other", yes_change["asset_id"], no_change["asset_id"]),
            StreamMarket("trade-market", "?", "other", REAL["last_trade_price"]["asset_id"], "unused-2"),
        ]
    )
    return r


def test_real_book_message():
    r = real_recorder()
    r.handle(REAL["book"])
    d = as_dict(r.drain()[0][0])
    assert (d["market_id"], d["best_bid"], d["best_ask"], d["spread"]) == ("book-market", 0.998, 0.999, 0.001)
    assert d["bid_sz_1"] == 19409.48 and d["bid_px_5"] == 0.994 and len(d) == len(BOOK_COLUMNS)


def test_real_price_change_message():
    r = real_recorder()
    yes_change = REAL["price_change"]["price_changes"][0]
    r.handle({"event_type": "book", "asset_id": yes_change["asset_id"], "timestamp": "1", "bids": [], "asks": []})
    r.drain()
    r.handle(REAL["price_change"])
    d = as_dict(r.drain()[0][0])
    assert d["best_bid"] == float(yes_change["price"]) and d["bid_sz_1"] == float(yes_change["size"])


def test_real_trade_and_tick_size_messages():
    r = real_recorder()
    for frame in (json.dumps(REAL["last_trade_price"]), json.dumps([REAL["tick_size_change"]])):
        for event in parse_frame(frame):
            r.handle(event)
    (t,) = r.drain()[1]
    assert t[1:6] == ("trade-market", 0, 0.002, 5.0, "BUY") and t[7].startswith("0x")
    assert r.counts["tick_size_change"] == 1 and r.counts["malformed"] == 0
