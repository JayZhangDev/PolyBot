"""Pair Polymarket questions with the same questions on Kalshi.

The two sites word their questions differently. "Will Marine Le Pen win the 2027
French presidential election?" appears word for word on both, but Polymarket's
"Anthropic IPO before 2027?" is Kalshi's "Who will IPO before 2027?" with the
answer "Anthropic". A pair has to pass these checks:

1. **Words.** Each question becomes a set of meaningful words: small words such as
   "will" and "the" are dropped, accents are removed and months are spelled out.
   Rare words, such as names, count for more than common ones. That weight is
   called IDF (inverse document frequency) and comes from how many Polymarket
   questions use the word. The score is the weighted share of words the two
   questions have in common, from 0 (none) to 1 (the same words).
2. **Numbers.** Every number except years, dates and times must be the same on both
   sides: "$50,000" and "50k" agree, "$50,000" and "$60,000" do not.
3. **Meaning words.** Words that change what is being asked must agree. Winning a
   nomination is not running for it, the presidency is not a Senate seat, a group
   stage is not the whole tournament, "above" is not "below", and "not" turns a
   question around. ``MEANING`` lists them.
4. **Deadline.** When the Polymarket question names a deadline ("before 2027", "by
   December 31, 2026", "on November 12"), the Kalshi market must close close to it.
   Otherwise one site gives the event more time to happen than the other, and the
   two prices answer different questions.
5. **One answer.** In a Kalshi event with many answers (one market per team or per
   candidate), only one answer may fit the question. "Inter Milan" fits both the
   "Inter" and the "Milan" market, so it is not paired at all.

A price on one particular day ("Bitcoin above $76,000 on November 12?") is never
paired: each site checks the price at its own time of day, so they are different bets.

Each Polymarket market gets at most one Kalshi market and the other way round.
Pairs that pass with a score of at least ``min_score`` are kept. The hand-checked
file ``kalshi_pairs.yaml`` adds pairs the matcher missed and removes wrong ones.
"""

from __future__ import annotations

import calendar
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from polybot.kalshi import KalshiMarket

SMALL_WORDS = frozenset(
    """a an the will be by of in on to at or and is are was for with from as before after end his her its
    their this that than any who what which when how does do did have has get gets yes no next""".split()
)
MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9
MONTH_NAMES = {number: calendar.month_name[number].lower() for number in range(1, 13)}

# Words that change what a question asks, grouped by meaning. Both questions must
# contain the same groups: "win" and "run" do not match, "out" and "resign" do.
_MEANING_GROUPS = {
    # what has to happen
    "win": "win wins won winner winning nominee elected champion",
    "run": "run runs running candidacy",
    "up": "above over exceed exceeds reach reaches hit hits higher high increase increases hike hikes raise "
    "raises rise rises up",
    "down": "below under dip dips drop drops fall falls lower low decrease decreases cut cuts down",
    "out": "out leave leaves resign resigns fired removed ousted exit exits depart departs",
    "not": "not never",
    "arrested": "arrested arrest",
    "indicted": "indicted indictment",
    "convicted": "convicted conviction",
    "impeached": "impeached impeachment",
    "pardon": "pardon pardons pardoned",
    "meet": "meet meets meeting",
    "visit": "visit visits",
    "ipo": "ipo",
    "recognize": "recognize recognizes recognise recognises",
    "acquire": "acquire acquires acquired acquisition",
    "ban": "ban bans banned",
    "sign": "sign signs signed",
    "approve": "approve approves approved",
    "announce": "announce announces announced announcement",
    "launch": "launch launches launched",
    "release": "release releases released",
    "invade": "invade invades invasion",
    # which contest: a nomination is not the election, and one office is not another
    "nomination": "nomination primary",
    "president": "president presidential presidency",
    "vice": "vice",  # the vice-presidential race is not the presidential one
    "senate": "senate senator senatorial",
    "governor": "governor gubernatorial",
    "mayor": "mayor mayoral",
    # which stage of a competition
    "group": "group",
    "semifinal": "semifinal semifinals semis",
    "quarterfinal": "quarterfinal quarterfinals",
    "playoff": "playoff playoffs postseason",
    "qualify": "qualify qualifies qualified qualification",
    "relegated": "relegated relegation",
}
MEANING = {word: group for group, words in _MEANING_GROUPS.items() for word in words.split()}
# "the Democratic nominee" asks the same as "win the Democratic nomination".
EXTRA_WORDS = {"nominee": ("nomination",)}
# The same thing said two ways, written one way before the words are read.
PHRASES = {"federal reserve": "fed", "united states": "us", "u.s.": "us"}

RARE_COUNT = 25  # a word in at most this many Polymarket questions is "rare" and can suggest a pair
MIN_RARE_SHARED = 2  # a Kalshi market is kept only if it shares this many rare words with one question
MAX_CANDIDATES = 300  # Kalshi markets compared in full for one Polymarket question
NEAR_MISS_SCORE = 0.45  # failed pairs scoring at least this go into the review list
SAME_DEADLINE_DAYS = 7  # when both questions name a deadline, they must be this close
# How far the Kalshi close may be from the Polymarket deadline, in days (before, after),
# by how exactly the deadline is stated. A whole year is looser before the deadline, because
# some events have a known date inside the year (a prize is announced in October).
DEADLINE_SLACK = {"day": (3, 3), "month": (31, 31), "year": (92, 31)}

_MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATE = re.compile(rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_TIME = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.IGNORECASE)
_NUMBER = re.compile(
    r"(\$)?(\d+(?:\.\d+)?)\s*(k|m|bn|b|thousand|million|billion|trillion|%|bps|bp)?(?![a-z])", re.IGNORECASE
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "trillion": 1e12}
_DEADLINE = re.compile(
    rf"\b(before|by|through|on|at|in|end\s+of)\s+(?:the\s+end\s+of\s+)?"
    rf"(?:({_MONTH_PATTERN})\.?\s+(?:(\d{{1,2}})(?:st|nd|rd|th)?\b,?\s*)?)?(20\d\d)?",
    re.IGNORECASE,
)


# ----------------------------- reading a question -----------------------------
def plain(text: str) -> str:
    """Lower case, accents removed ("Sébastien" becomes "sebastien"), no thousands commas, phrases unified."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    out = re.sub(r"(?<=\d),(?=\d)", "", folded.lower()).replace("’", "'")
    for phrase, same in PHRASES.items():
        out = out.replace(phrase, same)
    return out


def years(text: str) -> frozenset[int]:
    """The years a question mentions ("2026-27" counts as 2026)."""
    return frozenset(int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", text))


def _without_dates(text: str) -> str:
    """Drop the day from dates ("December 31" becomes "December") and clock times; the deadline check covers them."""
    return _TIME.sub(" ", _DATE.sub(lambda m: m.group(1), text))


def words(text: str) -> frozenset[str]:
    """The meaningful words of a question, with meaning words replaced by their group name."""
    out: set[str] = set()
    for word in re.findall(r"[a-z]+|\d+(?:\.\d+)?", _without_dates(plain(text)).replace("'s", "")):
        if word in SMALL_WORDS or (len(word) < 2 and not word.isdigit()):
            continue
        if word in MONTHS:
            word = MONTH_NAMES[MONTHS[word]]
        out.add(MEANING.get(word, word))
        out.update(EXTRA_WORDS.get(word, ()))
    return frozenset(out)


def numbers(text: str) -> frozenset[str]:
    """Every number in a question except years, dates and times, scaled: "$50k" and "50,000" both give "50000"."""
    found = set()
    for dollar, digits, unit in _NUMBER.findall(_without_dates(plain(text))):
        value = float(digits) * _SCALE.get(unit.lower(), 1)
        if not dollar and not unit and value.is_integer() and 1990 <= value <= 2100:
            continue  # a year ("$2000" is a price, not a year)
        found.add(f"{value:g}")
    return frozenset(found)


def meaning(word_set: frozenset[str]) -> frozenset[str]:
    return frozenset(w for w in word_set if w in _MEANING_GROUPS)


@dataclass(frozen=True)
class Deadline:
    """A deadline named in a question, and how exactly: to the "day", "month" or "year"."""

    due: date
    precision: str
    word: str  # "before", "by", "on", ...


def deadline(text: str, near: date | None = None) -> Deadline | None:
    """The last deadline a question names, if any.

    "before 2027" is 2027-01-01; "by 2026" and "end of 2026" are 2026-12-31;
    "by November 2026" is the last day of November. A date without a year
    ("on November 12") takes the year that puts it closest to ``near``.
    "in 2026" comes back with ``word`` "in": it may be a deadline ("happen in
    2026") or just the year of an event ("the Senate race in 2026").
    """
    for match in reversed(list(_DEADLINE.finditer(plain(text)))):
        word, month_name, day_text, year_text = match.groups()
        if month_name is None and year_text is None:
            continue  # "on the", "at least": not a date
        word = " ".join(word.split())
        if year_text is None:
            if near is None or month_name is None:
                continue
            years = (near.year - 1, near.year, near.year + 1)
        else:
            years = (int(year_text),)
        options = [d for d in (_build_date(word, month_name, day_text, y) for y in years) if d is not None]
        if options:
            best = min(options, key=lambda d: abs((d.due - near).days)) if near else options[0]
            return best
    return None


def _build_date(word: str, month_name: str | None, day_text: str | None, year: int) -> Deadline | None:
    if month_name is None:
        return Deadline(date(year, 1, 1) if word == "before" else date(year, 12, 31), "year", word)
    month = MONTHS[month_name]
    if day_text:
        try:
            return Deadline(date(year, month, int(day_text)), "day", word)
        except ValueError:
            return None
    if word == "before":
        return Deadline(date(year, month, 1), "month", word)
    return Deadline(date(year, month, calendar.monthrange(year, month)[1]), "month", word)


def _day(text: str | None) -> date | None:
    """The date in an ISO time such as "2026-12-31T23:59:00Z", or None."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _years_agree(ours: frozenset[int], theirs: frozenset[int]) -> bool:
    """True when some year on one side is within one of a year on the other (deadlines straddle New Year)."""
    return any(abs(a - b) <= 1 for a in ours for b in theirs)


def year_problem(question: Question, market: KalshiMarket) -> str | None:
    """Do the two questions point at the same year?

    When both name years, they must agree (within one, as "by December 31, 2026" and
    "before Jan 1, 2027" do). When only one side names a year, the other side's own
    end date has to fit it: an undated "win the CO-04 House seat" that ends in 2026
    is not Kalshi's "2028 House race for CO-04".
    """
    ours, theirs = years(plain(question.text)), years(plain(market.text))
    if ours and theirs:
        return None if _years_agree(ours, theirs) else f"the years differ ({min(ours)} vs {min(theirs)})"
    end = _day(question.end_date)
    if theirs and end and not min(theirs) <= end.year <= max(theirs) + 1:
        return f"Kalshi asks about {min(theirs)}, the Polymarket market ends in {end.year}"
    closes = _day(market.close_time)
    if ours and closes and not min(ours) <= closes.year <= max(ours) + 1:
        return f"Polymarket asks about {min(ours)}, the Kalshi market closes in {closes.year}"
    return None


# ----------------------------- the result -----------------------------
@dataclass(frozen=True)
class Question:
    """A Polymarket market as the matcher sees it."""

    market_id: str
    text: str
    end_date: str | None = None  # when the market ends (ISO text), used when the question names no year


@dataclass(frozen=True)
class Pair:
    """A Polymarket market and the Kalshi market asking the same question."""

    polymarket_id: str
    kalshi_ticker: str
    score: float
    question: str  # Polymarket's wording
    kalshi_question: str
    flip: bool = False  # True when Kalshi's YES means Polymarket's NO (only set by hand)
    source: str = "matched"  # "matched" by the matcher, or "added" by hand in kalshi_pairs.yaml


@dataclass(frozen=True)
class NearMiss:
    """The best Kalshi candidate for a question that was not paired, and why, for a person to review."""

    polymarket_id: str
    kalshi_ticker: str
    score: float
    question: str
    kalshi_question: str
    reason: str


# ----------------------------- the matcher -----------------------------
class Matcher:
    """Collects Kalshi markets that might match, then pairs them with the Polymarket questions.

    Feed Kalshi markets in with :meth:`add` as they download. Only those sharing at
    least two rare words with one Polymarket question are kept, so the 200,000
    Kalshi markets never have to sit in memory at once.
    """

    def __init__(self, questions: Sequence[Question]) -> None:
        self.questions = list(questions)
        self.question_words = [words(q.text) for q in self.questions]
        counts = Counter(w for ws in self.question_words for w in ws)
        n = len(self.questions)
        self.idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in counts.items()}
        self.unseen_weight = math.log(n + 1) + 1  # a word no Polymarket question uses is as rare as can be
        self.rare = frozenset(w for w, c in counts.items() if c <= RARE_COUNT)
        self.questions_by_word: dict[str, list[int]] = defaultdict(list)
        for i, ws in enumerate(self.question_words):
            for w in ws & self.rare:
                self.questions_by_word[w].append(i)
        self.kalshi: list[KalshiMarket] = []
        self.kalshi_words: list[frozenset[str]] = []
        self.outcome_words: list[frozenset[str]] = []
        self.index: dict[str, list[int]] = defaultdict(list)

    def add(self, market: KalshiMarket) -> bool:
        """Keep a Kalshi market if it shares enough rare words with one Polymarket question."""
        ws = words(market.text)
        keys = ws & self.rare
        shared = Counter(i for w in keys for i in self.questions_by_word[w])
        if not shared or max(shared.values()) < min(MIN_RARE_SHARED, len(keys)):
            return False
        position = len(self.kalshi)
        self.kalshi.append(market)
        self.kalshi_words.append(ws)
        self.outcome_words.append(words(market.outcome))
        for w in keys:
            self.index[w].append(position)
        return True

    def similarity(self, a: frozenset[str], b: frozenset[str]) -> float:
        """Weighted share of words in common: the weight of shared words over the weight of all words."""
        union = a | b
        if not union:
            return 0.0
        weight = self.idf.get
        shared = sum(weight(w, self.unseen_weight) for w in a & b)
        return shared / sum(weight(w, self.unseen_weight) for w in union)

    def problem(self, question: Question, qwords: frozenset[str], k: int) -> str | None:
        """Why the question and Kalshi market ``k`` are not the same question, or None if they pass."""
        market, kwords = self.kalshi[k], self.kalshi_words[k]
        if numbers(question.text) != numbers(market.text):
            return "the numbers differ"
        if meaning(qwords) != meaning(kwords):
            ours, theirs = sorted(meaning(qwords)), sorted(meaning(kwords))
            return f"meaning words differ ({', '.join(ours) or 'none'} vs {', '.join(theirs) or 'none'})"
        wrong_year = year_problem(question, market)
        if wrong_year:
            return wrong_year
        closes = _day(market.close_time)
        due = deadline(question.text, near=closes)
        if due and due.word == "on" and numbers(question.text):
            return "a price on one day: each site checks it at its own time"
        if due and closes and due.word != "in":  # "in 2026" may just name the event's year (see deadline)
            early, late = DEADLINE_SLACK[due.precision]
            if not -early <= (closes - due.due).days <= late:
                return f"Kalshi closes {closes}, the Polymarket deadline is {due.due}"
        theirs = deadline(market.text, near=closes)
        if due and theirs:
            # Two exact days must agree to the day ("by December 31" is "before January 1").
            allowed = 1 if due.precision == theirs.precision == "day" else SAME_DEADLINE_DAYS
            if abs((theirs.due - due.due).days) > allowed:
                return f"the deadlines differ ({due.due} vs {theirs.due})"
        return None

    def _second_answer(self, qwords: frozenset[str], k: int, candidates: list[int]) -> str | None:
        """Another answer in the same Kalshi event that also fits the question, if there is one.

        "Inter Milan" fits both "Inter" and "Milan". "Los Angeles FC" fits both "Los
        Angeles F" and "Los Angeles G", which read the same once single letters are dropped.
        """
        event, own = self.kalshi[k].event_ticker, self.outcome_words[k]
        if not own:
            return None
        for other in candidates:
            if other != k and self.kalshi[other].event_ticker == event:
                theirs = self.outcome_words[other]
                if theirs and theirs <= qwords:
                    return self.kalshi[other].outcome
        return None

    def _best(self, i: int) -> tuple[tuple[float, int] | None, tuple[float, int, str] | None]:
        """For question ``i``: the best Kalshi candidate that passes the checks, and the best one that fails.

        Both are kept apart so a high-scoring wrong candidate ("run for the nomination")
        never hides a slightly lower-scoring right one ("be the nominee").
        """
        question, qwords = self.questions[i], self.question_words[i]
        shared = Counter(k for w in qwords & self.rare for k in self.index.get(w, ()))
        candidates = [k for k, _ in shared.most_common(MAX_CANDIDATES)]
        best_pass: tuple[float, int] | None = None
        best_fail: tuple[float, int, str] | None = None
        for k in candidates:
            score = self.similarity(qwords, self.kalshi_words[k])
            if best_pass and score <= best_pass[0] and best_fail and score <= best_fail[0]:
                continue  # cannot beat either, so skip the slower checks
            reason = self.problem(question, qwords, k)
            if reason is None:
                other = self._second_answer(qwords, k, candidates)
                if other:
                    reason = f"two Kalshi answers fit: {self.kalshi[k].outcome} and {other}"
            if reason is None and (best_pass is None or score > best_pass[0]):
                best_pass = (score, k)
            elif reason is not None and (best_fail is None or score > best_fail[0]):
                best_fail = (score, k, reason)
        return best_pass, best_fail

    def pairs(self, min_score: float) -> tuple[list[Pair], list[NearMiss]]:
        """Every confident pair, best first, each market used once; and the near misses for review."""
        proposals, misses = [], []
        for i, question in enumerate(self.questions):
            best_pass, best_fail = self._best(i)
            if best_pass and best_pass[0] >= min_score:
                proposals.append((best_pass[0], i, best_pass[1]))
                continue
            # Not paired: keep the most telling candidate for a person to look at.
            if best_fail and best_fail[0] >= NEAR_MISS_SCORE:
                score, k, why = best_fail
            elif best_pass and best_pass[0] >= NEAR_MISS_SCORE:
                score, k = best_pass
                why = f"score {score:.2f} is below {min_score:.2f}"
            else:
                continue
            market = self.kalshi[k]
            misses.append(NearMiss(question.market_id, market.ticker, round(score, 3), question.text, market.text, why))

        pairs, used_kalshi = [], set()
        for score, i, k in sorted(proposals, key=lambda p: -p[0]):
            ticker = self.kalshi[k].ticker
            if ticker in used_kalshi:
                continue  # a closer Polymarket question already took this Kalshi market
            used_kalshi.add(ticker)
            question = self.questions[i]
            pairs.append(Pair(question.market_id, ticker, round(score, 3), question.text, self.kalshi[k].text))
        misses.sort(key=lambda m: -m.score)
        return pairs, misses
