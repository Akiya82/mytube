"""Slug normalization, transliteration, and collision resolution (ADDR-06).

Wraps `python-slugify` 8.0.4 (default backend `text-unidecode`, Artistic
License -- the GPL `unidecode` extra, `python-slugify[unidecode]`, is
deliberately not installed). Verified against real Cyrillic anime titles;
see the transliteration fixtures in tests/test_slugs.py for worked
examples.

`python-slugify` transliterates but does not detect near-duplicate
collisions: "Re:Zero" -> "re-zero" and "ReZero" -> "rezero" are two
different, both-valid slugs that a human would read as the same title. That
detection is this module's job (`similar_key`), not the library's -- the
library only guarantees a valid slug shape, not semantic uniqueness.
"""
import re
from dataclasses import dataclass

from slugify import slugify

_SLUG_SHAPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG_LENGTH = 80

# A digit run longer than this is refused before int() ever sees it
# (T-05-05, T-05-23): Postgres's own `episode_number_history.number` column
# is a 32-bit integer, so no real episode number is ever more than ten
# digits, and refusing well above that bound still leaves no room for a
# pathologically long string to cost anything before the address answers
# not found. Read by `parse_public_address` below and by
# `orchestrator.resolve`, which used to define its own copy of this same
# number before the grammar moved here.
MAX_NUMERIC_SEGMENT_LENGTH = 15


class SlugError(Exception):
    """Raised when a name cannot be turned into a usable slug."""


def normalize_slug(name: str) -> str:
    """Transliterate/normalize `name` into a slug matching ^[a-z0-9]+(-[a-z0-9]+)*$.

    Truncates to MAX_SLUG_LENGTH first, *then* strips any trailing hyphen
    left by the cut (slugify's own max_length truncates mid-string without
    respecting word boundaries) -- doing it this way instead of trusting
    slugify(max_length=...) keeps the result provably shape-valid rather than
    merely "usually valid".
    """
    slug = slugify(name)[:MAX_SLUG_LENGTH].rstrip("-")
    if not slug or not _SLUG_SHAPE.match(slug):
        raise SlugError(
            f"Could not derive a slug from {name!r} -- transliteration produced "
            "an empty or invalid string. Set the slug explicitly with the "
            "--slug flag."
        )
    return slug


def similar_key(slug: str) -> str:
    """Comparison key that catches "re-zero" vs "rezero" -- same key, different slug."""
    return slug.replace("-", "")


class EditionSlugRefused(Exception):
    """Raised when a candidate cannot be used as an edition slug."""


def normalize_edition_slug(name: str) -> str:
    """`normalize_slug`, plus one more refusal: an edition slug may never
    be made only of digits.

    An edition slug and an episode number share the same second path
    segment (GET /v/{slug}/{segment}, orchestrator.resolve) -- the route
    decides which resolver to run purely from whether that segment is made
    only of digits, in Python, before either query runs. A slug that
    normalised to an all-digit string would be silently read as an episode
    number instead of an edition slug the moment it was saved, and the
    address would resolve to whichever episode happened to hold that
    number -- or to nothing at all -- rather than to the edition it names.

    Uniqueness is scoped to one episode, not global, and is the caller's
    job, checked inside its own locked transaction exactly as a title
    slug's uniqueness already is (create_title_with_edition). It is
    deliberately not checked here, so this stays a pure function and
    nobody later reaches for a database read inside it.
    """
    slug = normalize_slug(name)
    if slug.isdigit():
        raise EditionSlugRefused(
            f"{slug!r} is made only of digits -- an edition slug shaped "
            "like that would be read as an episode number at the same "
            "address, and the edition would never resolve. Choose a slug "
            "with at least one non-digit character."
        )
    return slug


def pick_unique_slug(candidate: str, taken: set[str]) -> tuple[str, list[str]]:
    """Deterministically pick a free slug close to `candidate`.

    Returns (chosen_slug, similar_existing_slugs). `similar_existing_slugs`
    lists already-taken slugs whose `similar_key` matches `candidate`'s --
    reported regardless of whether the exact candidate was free, so callers
    can warn before saving (the "Re:Zero" / "ReZero" case, D-10/ADDR-06).

    Exact-match collisions get a numeric suffix starting at 2, incrementing
    until free. Same (candidate, taken) input always produces the same
    output -- pure function, no randomness, no time-of-check dependency.
    """
    key = similar_key(candidate)
    similar = sorted(s for s in taken if s != candidate and similar_key(s) == key)

    if candidate not in taken:
        return candidate, similar

    suffix = 2
    while f"{candidate}-{suffix}" in taken:
        suffix += 1
    return f"{candidate}-{suffix}", similar


# --- The public address grammar (D-156, D-187, T-05-23) --------------------
#
# What a second (and third) path segment of GET /v/{slug}/... means used to
# be decided twice: once inline in orchestrator.resolve's own routes, and
# a second time -- the plan was -- inline in the command line. One pasted
# or typed address deciding what gets read or written on the command line
# and what a browser redirects to are the exact same trust boundary, so
# this is one function instead of two: the route and the command line both
# call it and can no longer disagree about what a segment means.


class AddressRefused(Exception):
    """Raised when a path cannot be read as one of the four shapes the
    public address grammar accepts.
    """


@dataclass(frozen=True)
class PublicAddress:
    """One parsed public address: a title slug alone, a title slug and an
    episode number, a title slug and an edition slug (a film's alternative
    edition), or all three at once. `number` and `edition_slug` are `None`
    when the address did not carry them. Resolving this into a real row is
    entirely the caller's job -- this function never touches a database.
    """

    slug: str
    number: int | None = None
    edition_slug: str | None = None


def _parse_number_segment(segment: str) -> int | None:
    """`None` when `segment` is not made only of digits; the parsed integer
    otherwise, refusing an absurdly long digit run before it ever reaches
    `int()`.
    """
    if not segment.isdigit():
        return None
    if len(segment) > MAX_NUMERIC_SEGMENT_LENGTH:
        raise AddressRefused(f"numeric segment {segment!r} is too long")
    return int(segment)


def _parse_edition_segment(segment: str) -> str:
    """An edition slug segment, refused when it fails the same shape rule
    `normalize_edition_slug` enforces at save time (valid slug shape, never
    all digits) -- a segment reaching here is meant to already be a stored
    slug, not a name to transliterate, so this validates rather than
    normalizes.
    """
    if segment.isdigit() or not _SLUG_SHAPE.match(segment):
        raise AddressRefused(f"{segment!r} cannot be an edition slug")
    return segment


def parse_public_address(path: str) -> PublicAddress:
    """Parse a public address -- GET /v/{slug}[/{segment}[/{edition_slug}]]
    -- into a `PublicAddress`, or raise `AddressRefused`.

    Accepts the four shapes the product promises: a bare title slug; a
    title slug and a number; a title slug and an edition slug; a title
    slug, a number and an edition slug. Refuses everything else: a fourth
    segment, an empty segment, or an edition slug that fails the edition
    rule.

    A second segment made only of digits is an episode number -- every
    leading-zero form (`"05"`, `"5"`, `"005"`) collapses onto the same
    integer, which is the whole reason a number dictated aloud works. A
    segment that is not all digits is an edition slug. In the three-segment
    shape the second segment must be a number (an edition of an episode
    always answers at `/v/{slug}/{number}/{edition}`, never
    `/v/{slug}/{edition}/{number}`) and the third is always an edition
    slug, refused if it is made only of digits.

    Takes a path with or without a leading `v` marker segment, so an
    operator can paste the exact address a browser shows
    (`/v/horimiya/05`) or type the bare form (`horimiya/05`) -- both parse
    identically. Never touches a database.
    """
    trimmed = path.strip("/")
    if not trimmed:
        raise AddressRefused("empty address")

    segments = trimmed.split("/")
    if segments[0] == "v":
        segments = segments[1:]

    if not segments or any(not segment for segment in segments):
        raise AddressRefused(f"empty path segment in {path!r}")
    if len(segments) > 3:
        raise AddressRefused(f"too many path segments in {path!r}")

    slug = segments[0]
    if len(segments) == 1:
        return PublicAddress(slug=slug)

    if len(segments) == 2:
        number = _parse_number_segment(segments[1])
        if number is not None:
            return PublicAddress(slug=slug, number=number)
        return PublicAddress(slug=slug, edition_slug=_parse_edition_segment(segments[1]))

    number = _parse_number_segment(segments[1])
    if number is None:
        raise AddressRefused(f"second segment of {path!r} is not a number")
    edition_slug = _parse_edition_segment(segments[2])
    return PublicAddress(slug=slug, number=number, edition_slug=edition_slug)


def format_episode_number(number: int) -> str:
    """The canonical display form of an episode number (D-155): zero-padded
    to two digits, three from 100. This is a rendering rule only -- what an
    operator types (a renumber form's own number field, a leading-zero
    address segment) is never required to already look like this;
    `_parse_number_segment` above accepts any leading-zero form and
    collapses it to the same integer before this function ever runs.
    """
    return f"{number:03d}" if number >= 100 else f"{number:02d}"
