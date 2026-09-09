"""Jinja2 environment, the plain-JSON i18n catalogs, and the per-request
render() helper every route in this package uses instead of calling
Jinja2Templates directly.
"""
import json
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.templating import Jinja2Templates

LOCALES = ("en", "ru")
DEFAULT_LOCALE = "en"

_LOCALES_DIR = Path(__file__).parents[1] / "locales"
_CATALOGS: dict[str, dict[str, str]] = {
    lang: json.loads((_LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    for lang in LOCALES
}

# Never pass autoescape=False here, and never flip an environment option
# that would disable it -- Jinja2Templates leaves autoescaping at its
# default, which is what keeps every raw, unsanitized filename this phase
# renders (D-46) inert text instead of live HTML (T-02-02).
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def filesizeformat_binary(num_bytes) -> str:
    """Render a byte count as `1.4 GB`-style text, binary (1024) units."""
    size = float(num_bytes)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units[:-1]:
        if abs(size) < 1024.0:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} {units[-1]}"


def duration_hms(seconds) -> str:
    """Render a second count as `1:32:10` (or `7:05` under an hour)."""
    total = int(round(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def humanize_seconds(seconds) -> str:
    """Render a second count as the loose phrasing the estimate line and
    the "working" status use -- "about 7 minutes", never a raw number of
    seconds.
    """
    total_minutes = float(seconds) / 60.0
    if total_minutes < 1:
        return "under a minute"
    if total_minutes < 60:
        minutes = round(total_minutes)
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    hours = round(total_minutes / 60.0, 1)
    return f"{hours} hour" + ("" if hours == 1 else "s")


def pad_episode_number(value) -> str:
    """The renumber and move forms' own display filter for D-155's
    canonical zero-padded number -- every address either form prints goes
    through this, never the raw column value. Tolerant of a value that
    is not a plain non-negative integer (a form re-rendering a refused,
    still-being-typed number) rather than raising mid-template: an address
    preview simply shows the typed text back until it parses.
    """
    from orchestrator.slugs import format_episode_number

    try:
        return format_episode_number(int(str(value).strip()))
    except (TypeError, ValueError):
        return str(value)


def urlencode_strict(value) -> str:
    """Percent-encode `value` for embedding as a single query-string value.

    Overrides Jinja2's own `urlencode` filter, which leaves `/` unescaped
    (the right default for encoding a bare path) -- every caller of this
    filter in this project embeds a *whole relative address*, path and
    query string together, as the value of a single `next=` parameter.
    Jinja's default would leave the embedded `?` and `/` unescaped and
    produce a malformed nested query string; this escapes every reserved
    character, matching `urllib.parse.quote(value, safe="")`.
    """
    return quote(str(value), safe="")


_templates.env.filters["filesizeformat_binary"] = filesizeformat_binary
_templates.env.filters["duration_hms"] = duration_hms
_templates.env.filters["humanize_seconds"] = humanize_seconds
_templates.env.filters["urlencode"] = urlencode_strict
_templates.env.filters["pad_episode_number"] = pad_episode_number


def reason_text_for(t):
    """One refusal sentence, built from a catalogue key and the values it
    substitutes -- never from a stored English sentence.

    A key the catalogs carry wins and is filled in with its own
    parameters, which is what makes "not enough free space" read as that
    and not as a sentence about clips that cannot play together. A key
    with no catalogue line falls back to the generic prefix plus whatever
    values were recorded, so an operator still sees which of their clips
    is involved instead of a bare identifier.
    """

    def reason_text(key, params):
        values = params or {}
        if not key:
            return ""
        sentence = t(key)
        if sentence == key:
            return t("playlist.refused.reason_prefix").format(
                reason=", ".join(str(value) for value in values.values())
            )
        try:
            return sentence.format(**values)
        except (KeyError, IndexError):
            return sentence

    return reason_text


def resolve_lang(request: Request) -> str:
    """The `mytube_lang` cookie value, or DEFAULT_LOCALE for anything the
    catalogs don't have -- an unknown or missing cookie never raises.
    """
    lang = request.cookies.get("mytube_lang", DEFAULT_LOCALE)
    return lang if lang in LOCALES else DEFAULT_LOCALE


def translator(lang: str):
    """A lookup callable for `lang`: returns the key itself when missing,
    so a template referencing a not-yet-translated key degrades to the key
    text rather than raising.
    """
    catalog = _CATALOGS.get(lang, _CATALOGS[DEFAULT_LOCALE])
    return lambda key: catalog.get(key, key)


def render(request: Request, template_name: str, status_code: int = 200, **context):
    """Render `template_name`, injecting `t`, `lang`, `public_base_url`, and
    `current_url` fresh for this request. Built per call rather than
    stored on `_templates.env.globals` -- a shared mutable global would
    race under concurrent requests carrying different language cookies.

    `current_url` is `request.url.path` plus `?` + the query string when
    there is one -- the address the language switcher builds its `next=`
    from (`base.html`). **It is placed before `**context`, not after, and
    that ordering is the feature:** a route that passes `current_url=`
    explicitly overrides the computed value. Exactly two routes need to --
    the login route's and the setup route's own failure branches. The
    switcher works from the request's own address for every `GET`, but a
    form that fails validation re-renders on a `POST`, and on a `POST` the
    state lives in the body, not in the query string: the computed
    `current_url` would then be bare `/login` or bare `/setup`, and
    switching language after an error would throw away the destination
    the operator was heading to, or destroy a one-time setup link and land
    them on a banner that tells them their link is spent. Do not reorder
    this dictionary for tidiness -- the override depends on this position.
    """
    from orchestrator.config import get_settings

    lang = resolve_lang(request)
    settings = get_settings()
    current_url = request.url.path
    if request.url.query:
        current_url += "?" + request.url.query
    ctx = {
        "t": translator(lang),
        "reason_text": reason_text_for(translator(lang)),
        "lang": lang,
        "public_base_url": settings.public_base_url,
        "current_url": current_url,
        **context,
    }
    return _templates.TemplateResponse(request, template_name, ctx, status_code=status_code)
