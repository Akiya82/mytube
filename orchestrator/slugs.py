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

from slugify import slugify

_SLUG_SHAPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG_LENGTH = 80


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
