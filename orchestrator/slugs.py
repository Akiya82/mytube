"""Slug normalization, transliteration, and collision resolution (ADDR-06).

Wraps `python-slugify` 8.0.4 (default backend `text-unidecode`, Artistic
License -- the GPL `unidecode` extra, `python-slugify[unidecode]`, is
deliberately not installed; see 01-RESEARCH.md "Standard Stack"). Verified
locally in 01-RESEARCH.md against real Cyrillic strings: "Атака Титанов" ->
"ataka-titanov", "Пираты Карибского моря" -> "piraty-karibskogo-moria".

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
            f"Не удалось получить слаг из {name!r} -- транслитерация дала "
            "пустую или недопустимую строку. Задайте слаг вручную флагом "
            "--slug."
        )
    return slug


def similar_key(slug: str) -> str:
    """Comparison key that catches "re-zero" vs "rezero" -- same key, different slug."""
    return slug.replace("-", "")


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
