"""Pairing Polymarket questions with Kalshi markets. The cases come from real mistakes seen while building it."""

from __future__ import annotations

from datetime import date

from polybot.kalshi import KalshiMarket
from polybot.kalshi_match import Matcher, Question, deadline, numbers, words, years


def km(ticker: str, title: str, outcome: str = "", close: str = "2027-06-01T00:00:00Z") -> KalshiMarket:
    """A Kalshi market; its event is the ticker without the last part, as on Kalshi."""
    return KalshiMarket(ticker, ticker.rsplit("-", 1)[0], title, outcome, close, 0.40, 0.42, 0.41, 100.0)


def match(questions: list[Question], markets: list[KalshiMarket], min_score: float = 0.6):
    matcher = Matcher(questions)
    for market in markets:
        matcher.add(market)
    return matcher.pairs(min_score)


# ----------------------------- reading a question -----------------------------
def test_words_drop_small_words_fold_accents_and_group_meanings():
    assert words("Will Sébastien Lecornu win the 2027 French presidential election?") == {
        "sebastien",
        "lecornu",
        "win",
        "2027",
        "french",
        "president",
        "election",
    }
    assert {"win", "nomination"} <= words("Will Gina Raimondo be the Democratic nominee?")
    assert words("Will Fed cut rates by Dec 31?") == words("Will the Federal Reserve cut rates by December 31?")


def test_numbers_agree_however_they_are_written_and_skip_years_and_dates():
    assert numbers("above $50,000?") == numbers("50k or above") == {"50000"}
    assert numbers("above $2,000 on November 12?") == {"2000"}  # a price, even though 2000 looks like a year
    assert numbers("Will it happen by December 31, 2026?") == frozenset()
    assert numbers("hike 25 bps") == numbers("Hike 25bps") == {"25"}
    assert years("Will Roma win the 2026-27 Serie A?") == {2026}


def test_deadlines():
    assert deadline("Will Anthropic IPO before 2027?").due == date(2027, 1, 1)
    by_day = deadline("Ilhan Omar arrested by December 31, 2026?")
    assert (by_day.due, by_day.precision) == (date(2026, 12, 31), "day")
    assert deadline("Will Maduro be the leader of Venezuela end of 2026?").due == date(2026, 12, 31)
    assert deadline("Will Bitcoin hit $100k by November 2026?").due == date(2026, 11, 30)
    on_day = deadline("Will the price of Bitcoin be above $76,000 on November 12?", near=date(2026, 11, 13))
    assert (on_day.due, on_day.word) == (date(2026, 11, 12), "on")
    assert deadline("Will Satoshi move any Bitcoin in 2026?").word == "in"
    assert deadline("Will Benny Gantz be the next Prime Minister of Israel?") is None


# ----------------------------- pairing -----------------------------
def test_the_same_question_is_paired_with_a_score_of_one():
    q = Question("1", "Will Marine Le Pen win the 2027 French presidential election?", "2027-05-09T00:00:00Z")
    pairs, misses = match([q], [km("KXFRENCHPRES-27-MLEP", q.text)])
    assert [(p.polymarket_id, p.kalshi_ticker, p.score) for p in pairs] == [("1", "KXFRENCHPRES-27-MLEP", 1.0)]
    assert misses == []


def test_being_the_nominee_is_winning_the_nomination_but_running_for_it_is_not():
    q = Question("1", "Will Gina Raimondo win the 2028 Democratic presidential nomination?", "2028-08-31T00:00:00Z")
    run = km("KXRUN-28-GR", "Who will run for the Democratic presidential nomination in 2028?", "Gina Raimondo")
    nominee = km("KXNOM-28-GR", "Will Gina Raimondo be the Democratic Presidential nominee in 2028?")
    pairs, _ = match([q], [run, nominee])
    assert [p.kalshi_ticker for p in pairs] == ["KXNOM-28-GR"]
    pairs, misses = match([q], [run])
    assert pairs == [] and "meaning words differ" in misses[0].reason


def test_the_vice_presidential_race_is_not_the_presidential_one():
    q = Question("1", "Will Gavin Newsom win the 2028 US Presidential Election?", "2028-11-07T00:00:00Z")
    vice = km("KXVPRES-29-GNEW", "Who will win the 2028 United States vice presidential election?", "Gavin Newsom")
    president = km(
        "KXPRES-28-GNEW", "Who will win the next presidential election?", "Gavin Newsom", "2028-11-07T00:00:00Z"
    )
    pairs, _ = match([q], [vice, president])
    assert [p.kalshi_ticker for p in pairs] == ["KXPRES-28-GNEW"]


def test_no_pair_when_two_answers_in_one_kalshi_event_fit():
    q = Question("1", "Will Inter Milan win the 2026-27 Serie A Championship?", "2027-05-31T00:00:00Z")
    inter = km("KXSERIEA-27-INT", "Will Inter win the 2026-27 Serie A?", "Inter")
    milan = km("KXSERIEA-27-ACM", "Will Milan win the 2026-27 Serie A?", "Milan")
    pairs, misses = match([q], [inter, milan])
    assert pairs == [] and misses[0].reason.startswith("two Kalshi answers fit")


def test_a_kalshi_market_that_runs_past_the_deadline_asks_something_else():
    q = Question("1", "Will Zelenskyy and Putin meet next in Hungary before 2027?", "2026-12-31T00:00:00Z")
    pairs, misses = match(
        [q], [km("KXMEET-28-HUN", "Will Putin and Zelenskyy meet next in Hungary?", close="2028-12-31T00:00:00Z")]
    )
    assert pairs == [] and misses[0].reason == "Kalshi closes 2028-12-31, the Polymarket deadline is 2027-01-01"


def test_deadlines_named_on_both_sides_must_agree():
    q = Question("1", "Will Miguel Diaz-Canel leave office before 2027?", "2026-12-31T00:00:00Z")
    k = km(
        "KXOUT-26DEC01", "Will Miguel Diaz-Canel leave office before December 1, 2026?", close="2026-12-01T00:00:00Z"
    )
    pairs, misses = match([q], [k])
    assert pairs == [] and misses[0].reason.startswith("the deadlines differ")
    same = km("KXOUT-27JAN01", "Will Miguel Diaz-Canel leave office before Jan 1, 2027?", close="2027-01-01T00:00:00Z")
    assert [p.kalshi_ticker for p in match([q], [same])[0]] == ["KXOUT-27JAN01"]


def test_a_price_on_one_day_is_never_paired():
    q = Question("1", "Will the price of Bitcoin be above $76,000 on November 13?", "2026-11-13T17:00:00Z")
    k = km("KXBTCD-26NOV1317-T76K", "Bitcoin price on Nov 13, 2026? $76,000 or above", close="2026-11-13T22:00:00Z")
    pairs, misses = match([q], [k])
    assert pairs == [] and misses[0].reason.startswith("a price on one day")


def test_the_numbers_must_match():
    q = Question("1", "Will Bitcoin reach $100,000 by December 31, 2026?", "2026-12-31T00:00:00Z")
    k = km("KXBTC-26-90K", "Will Bitcoin exceed $90,000 by Dec 31, 2026?", close="2026-12-31T23:59:00Z")
    pairs, misses = match([q], [k])
    assert pairs == [] and misses[0].reason == "the numbers differ"


def test_an_undated_question_takes_its_year_from_the_market_end_date():
    q = Question("1", "Will the Republican Party win the CO-04 House seat?", "2026-11-03T00:00:00Z")
    k = km(
        "KXHOUSE-CO04-28-R",
        "Will the Republican Party win the 2028 House race for CO-04?",
        close="2028-11-07T00:00:00Z",
    )
    pairs, misses = match([q], [k])
    assert pairs == [] and misses[0].reason.startswith("Kalshi asks about 2028")


def test_each_kalshi_market_goes_to_the_closest_question_only():
    exact = Question("1", "Will Ann Kao win the 2026 Hsinchu City mayoral election?", "2026-11-28T00:00:00Z")
    looser = Question("2", "Will Ann Kao win the next Hsinchu City Mayor election?", "2026-11-28T00:00:00Z")
    k = km("KXHSINCHU-26NOV28-AKAO", exact.text, close="2026-11-28T00:00:00Z")
    pairs, _ = match([exact, looser], [k])
    assert [p.polymarket_id for p in pairs] == ["1"]


def test_a_weak_match_is_listed_as_a_near_miss():
    q = Question("1", "Will Chicago Fire FC win the 2026 MLS Cup?", "2026-12-06T00:00:00Z")
    pairs, misses = match(
        [q], [km("KXMLSCUP-26-CHI", "Will Chicago win the MLS Cup?", close="2026-12-06T00:00:00Z")], 0.9
    )
    assert pairs == [] and misses[0].reason.startswith("score") and misses[0].kalshi_ticker == "KXMLSCUP-26-CHI"


def test_only_kalshi_markets_sharing_two_rare_words_are_kept():
    matcher = Matcher([Question("1", "Will Marine Le Pen win the 2027 French presidential election?")])
    assert not matcher.add(km("KXRAIN-26", "Will it rain in Paris on Christmas?"))
    assert matcher.add(km("KXFRENCHPRES-27-MLEP", "Will Marine Le Pen win the 2027 French presidential election?"))
    assert len(matcher.kalshi) == 1
