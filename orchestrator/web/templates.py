"""Jinja2 environment, the plain-JSON i18n catalogs, and the per-request
render() helper every route in this package uses instead of calling
Jinja2Templates directly.
"""
import json
from pathlib import Path

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


_templates.env.filters["filesizeformat_binary"] = filesizeformat_binary
_templates.env.filters["duration_hms"] = duration_hms
_templates.env.filters["humanize_seconds"] = humanize_seconds


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
    """Render `template_name`, injecting `t`, `lang`, and `public_base_url`
    fresh for this request. Built per call rather than stored on
    `_templates.env.globals` -- a shared mutable global would race under
    concurrent requests carrying different language cookies.
    """
    from orchestrator.config import get_settings

    lang = resolve_lang(request)
    settings = get_settings()
    ctx = {
        "t": translator(lang),
        "lang": lang,
        "public_base_url": settings.public_base_url,
        **context,
    }
    return _templates.TemplateResponse(request, template_name, ctx, status_code=status_code)
