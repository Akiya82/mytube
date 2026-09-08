"""A release filename becomes a title, a season, an episode number and a
provenance key -- or an honest blank the operator fills in, never a guess.

`anitopy` is the primary parser (purpose-built for anime-release naming,
right on every filename this project's own library was measured against);
`guessit` is the documented fallback for a name `anitopy` extracts no
usable episode number from. Neither library's own season/episode guess is
trusted blind: `anitopy` already handles the common "Nth Season" phrasing
and its own `anime_season` tag, but leaves a trailing roman numeral fully
inside the title, and returns a real number for a special/half-numbered
file that this product wants discarded, not trusted. `guessit`'s general-
purpose heuristics are, measured live, more willing than `anitopy`'s to
invent a season/episode pair out of a bare number that is really a year --
its own guard exists specifically to close that hole before the fallback's
answer ever reaches the operator.

Nothing here touches the filesystem, and nothing raises for a name it
cannot read.
"""
import re
from dataclasses import dataclass

import anitopy
import guessit

# D-151: a trailing arabic number becomes a season only up to this bound --
# above it, a four-digit year ("Blade Runner 2049") or a large identifier
# in a title's last word must stay part of the name rather than being read
# as a season count no real single-cour anime series would have.
MAX_SEASON_NUMBER = 20

# The exact key each `normalize_season` branch returns, so a caller (the
# confirmation screen) can say where a season number came from rather than
# just showing a number nobody can trace back to a decision.
SEASON_PROVENANCE_KEYS = frozenset(
    {
        "detected_tag",
        "detected_numeral",
        "detected_ordinal",
        "detected_trailing_digit",
        "detected_fallback",
        "none",
    }
)

# A closed, narrow alphabet -- II..XX covers every real season count a
# release group would ever write as a roman numeral. Not a general roman-
# numeral parser: "Blade Runner 2049" must never match this, and a bound
# closed alphabet is what keeps it from doing so by construction rather
# than by coincidence.
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10}
_ROMAN_TRAILING = re.compile(r"^(?P<title>.+?)\s+(?P<numeral>[IVX]+)$")

# `anitopy` already strips "2nd Season"/"3rd Season" itself in every case
# this project measured -- this is a defensive fallback for a phrasing its
# own pattern library does not cover, not the common path.
_ORDINAL_TRAILING = re.compile(
    r"^(?P<title>.+?)\s+(?P<n>\d{1,2})(?:st|nd|rd|th)\s+Season$", re.IGNORECASE
)

# D-151's own bound: one or two digits, never three or four -- a year can
# never match this pattern by its shape alone, regardless of its value.
_TRAILING_DIGIT = re.compile(r"^(?P<title>.+?)\s+(?P<n>\d{1,2})$")

# `guessit`'s own year-splitting heuristic ("Blade Runner 2049" -> season
# 20, episode 49, measured live) lands exactly on the season-20 boundary
# the primary path's own trailing-digit guard would still accept as a real
# season. The fallback's own guard is therefore one stricter than the
# primary path -- no title in this owner's library has a 20th season, and
# a real one arriving would still be caught first by `anitopy`'s own
# `anime_season` tag, never by this fallback.
_MAX_FALLBACK_SEASON = MAX_SEASON_NUMBER - 1
_MAX_FALLBACK_EPISODE = 999


def _roman_to_int(numeral: str) -> int | None:
    total, previous = 0, 0
    for char in reversed(numeral):
        if char not in _ROMAN_VALUES:
            return None
        value = _ROMAN_VALUES[char]
        total += value if value >= previous else -value
        previous = value
    return total if 1 <= total <= MAX_SEASON_NUMBER else None


def normalize_season(title: str, anime_season: str | None) -> tuple[str, int | None, str]:
    """`(name, season_number, provenance_key)` -- first match wins, in the
    order D-151 fixes: the parser's own season tag, a trailing roman
    numeral, a trailing ordinal (defensive), a trailing one-or-two-digit
    number. A four-digit number, a number above the bound, or a named
    season with no number of its own all leave the title untouched.

    Called directly on a folder name as well as on a parsed filename's own
    title -- a pure function over two strings, nothing else.
    """
    if anime_season:
        return title, int(anime_season), "detected_tag"
    if match := _ROMAN_TRAILING.match(title):
        season = _roman_to_int(match["numeral"])
        if season:
            return match["title"], season, "detected_numeral"
    if match := _ORDINAL_TRAILING.match(title):
        return match["title"], int(match["n"]), "detected_ordinal"
    if match := _TRAILING_DIGIT.match(title):
        season = int(match["n"])
        if season <= MAX_SEASON_NUMBER:
            return match["title"], season, "detected_trailing_digit"
    return title, None, "none"


def parse_episode_number(parsed: dict) -> int | None:
    """D-168: integers only. `anitopy` genuinely returns a number for a
    special/half-numbered file (tagged separately via `anime_type`, or as
    a string with a literal decimal point) -- the product wants both
    discarded, not trusted, so the operator assigns them by hand.
    """
    if parsed.get("anime_type"):
        return None
    raw = parsed.get("episode_number")
    if raw is None:
        return None
    stripped = raw.lstrip("0")
    if not (stripped.isdigit() or raw == "0"):
        return None
    return int(raw)


def guessit_fallback(filename: str) -> tuple[str | None, int | None, int | None]:
    """`(title, season, episode)` or `(None, None, None)` on anything the
    guard cannot trust -- an ambiguous list of candidate episode numbers,
    or a season/episode pair large enough to be a hallucinated split of a
    bare number that was never a season/episode pair at all.
    """
    try:
        result = guessit.guessit(filename)
    except Exception:
        return None, None, None

    episode = result.get("episode")
    if isinstance(episode, list):  # two candidates, genuinely ambiguous
        return None, None, None
    season = result.get("season")
    title = result.get("title")
    if episode is None or title is None:
        return None, None, None
    if season is not None and season >= _MAX_FALLBACK_SEASON:
        return None, None, None
    if episode > _MAX_FALLBACK_EPISODE:
        return None, None, None
    return str(title), int(season) if season is not None else None, int(episode)


@dataclass(frozen=True)
class Recognised:
    """One filename's recognition result. `season`/`episode` are `None`
    when nothing was found or trusted -- never a guess. `raw_name` is the
    original filename, kept so the confirmation screen can always show
    what it started from even when nothing else was recognised.
    """

    name: str
    season: int | None
    season_provenance: str
    episode: int | None
    raw_name: str


def recognize_filename(filename: str) -> Recognised:
    """The whole chain: `anitopy` first, its own season/episode guards
    applied, `guessit` only when `anitopy` found no episode number at all
    (never for a special or a half number -- those are a deliberate D-168
    discard, not a parser failure to revisit with a second, less careful
    tool).
    """
    try:
        parsed = anitopy.parse(filename) or {}
    except Exception:
        parsed = {}

    title = parsed.get("anime_title") or filename
    anime_type = parsed.get("anime_type")
    if anime_type:
        # `anitopy` leaks the type tag into the title when it sits right
        # after a dash separator ("Title - OVA" for "Title - OVA1...") --
        # stripped here so the name field never shows it.
        title = title.removesuffix(f" - {anime_type}")

    name, season, season_provenance = normalize_season(title, parsed.get("anime_season"))
    episode = parse_episode_number(parsed)

    raw_episode = parsed.get("episode_number")
    if episode is None and not anime_type and raw_episode is None:
        _fallback_title, fallback_season, fallback_episode = guessit_fallback(filename)
        if fallback_episode is not None:
            episode = fallback_episode
        if season is None and fallback_season is not None:
            season = fallback_season
            season_provenance = "detected_fallback"

    return Recognised(
        name=name,
        season=season,
        season_provenance=season_provenance,
        episode=episode,
        raw_name=filename,
    )
