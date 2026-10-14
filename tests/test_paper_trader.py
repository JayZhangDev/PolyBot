import pytest
from conftest import make_market

from polybot import paper_trader
from polybot.portfolio import STARTING_BANKROLL, get_bankroll, get_open_positions, summary
from polybot.storage import save_snapshot


def open_longshot(mid="1"):
    save_snapshot([make_market(mid, yes=0.10, bid=0.09, ask=0.11)])
    result = paper_trader.run()
    assert result["opened"] == 1
    [pos] = get_open_positions()
    return pos


def test_opens_at_entry_price_and_debits_stake():
    pos = open_longshot()
    stake = STARTING_BANKROLL * 0.01
    assert pos["side"] == "NO"
    assert pos["entry_price"] == pytest.approx(0.91)
    assert pos["shares"] == pytest.approx(stake / 0.91, rel=1e-3)
    assert get_bankroll() == pytest.approx(STARTING_BANKROLL - stake)


def test_resolved_win_pays_one_per_share():
    pos = open_longshot()
    # Market closes with NO winning: YES price goes to 0.
    save_snapshot([make_market("1", yes=0.0, closed=True)])
    assert paper_trader.run()["closed"] == 1
    s = summary()
    assert s["wins"] == 1 and s["open_positions"] == 0
    assert get_bankroll() == pytest.approx(STARTING_BANKROLL - pos["stake"] + pos["shares"])


def test_resolved_loss_loses_stake():
    pos = open_longshot()
    save_snapshot([make_market("1", yes=1.0, closed=True)])
    paper_trader.run()
    assert summary()["losses"] == 1
    assert get_bankroll() == pytest.approx(STARTING_BANKROLL - pos["stake"])


def test_unresolved_market_stays_open():
    open_longshot()
    save_snapshot([make_market("1", yes=0.12)])
    assert paper_trader.run()["closed"] == 0
    assert len(get_open_positions()) == 1


def test_never_doubles_up_on_a_held_market():
    open_longshot()
    assert paper_trader.run()["opened"] == 0
