"""A language code, a language name in any language, or a folder token all
resolve to one canonical language tag -- or to an honest nothing. Never a
guess: the product would rather show "language unknown" with the folder
name still visible than label a studio folder or a resolution tag as a
resolved language (D-184).

The CLDR data behind `langcodes`/`language_data` is the dictionary this
project would otherwise have to hand-type one language at a time -- see
`pyproject.toml`'s own comment for why that split is deliberate. Track
kind is a different shape of problem: four closed values in two
languages, small enough that a maintained dependency would be the wrong
rung of the ladder -- extended the same way this project's own interface
languages are, by adding words.
"""
import langcodes
from langcodes import Language

# D-185's own vocabulary, English and Russian to start. A plain dict, not
# a library: track kind is a closed, small enumeration, unlike a language
# name, which is an open set with a maintained data source behind it.
TRACK_KIND_WORDS = {
    "sound": "audio",
    "audio": "audio",
    "озвучка": "audio",
    "дубляж": "audio",
    "subs": "subtitles",
    "subtitles": "subtitles",
    "субтитры": "subtitles",
    "signs": "signs_only",
    "надписи": "signs_only",
    "full": "full",
    "полные": "full",
}


def resolve_language(token: str) -> str | None:
    """A BCP-47 tag, or `None` when `token` names no real language.

    Two calls, in this order, because neither alone covers both shapes a
    caller hands in: `Language.get()` handles a short code in any case
    (`ru`, `RUS`, `rus`), `langcodes.find()` handles a full name in any
    language (`Russian`, `русский`, `日本語`). `Language.get()` does **not**
    raise for short alphanumeric junk -- measured live, `Language.get("Sound")`
    and `Language.get("720p")` both "succeed", returning an object whose
    own `.is_valid()` is `False`. That check is load-bearing and is called
    before the tag is ever trusted; skipping it would label a studio
    folder name or a resolution tag as a resolved language.
    """
    try:
        candidate = Language.get(token)
        if candidate.is_valid():
            return candidate.to_tag()
    except langcodes.LanguageTagError:
        pass
    try:
        return langcodes.find(token).to_tag()
    except LookupError:
        return None


def resolve_track_kind(token: str) -> str | None:
    """`"audio"` / `"subtitles"` / `"signs_only"` / `"full"`, or `None` for
    a word the vocabulary above does not carry. A plain lookup, lowercased
    and stripped -- the same case- and whitespace-tolerance every other
    token match in this codebase already applies.
    """
    return TRACK_KIND_WORDS.get(token.strip().lower())


def language_from_tokens(tokens: list[str]) -> tuple[str | None, str | None]:
    """`(tag, source_token)` for the first token in `tokens` that resolves
    to a language, or `(None, None)` when nothing in the list does.

    `tokens` is the ordered candidate list a caller assembled -- an
    embedded tag first, then a filename suffix, then every folder segment
    between the source and the sidecar -- so the caller can show the
    operator exactly where the answer came from, or that nothing
    resolvable was found at all. This function never guesses: a token
    list with nothing recognisable returns `(None, None)`, the same
    "language unknown, folder name still visible" answer D-184 asks for.
    """
    for token in tokens:
        tag = resolve_language(token)
        if tag is not None:
            return tag, token
    return None, None
