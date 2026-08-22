"""Both locale catalogs carry the same key set, no value leaks a planning
identifier (requirement/decision IDs, phase numbers -- see .claude/CLAUDE.md
"Planning traces never reach the user"), and every `t("...")` key a
template references actually exists in both catalogs.
"""
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "orchestrator" / "locales"
TEMPLATES_DIR = REPO_ROOT / "orchestrator" / "web" / "templates"

_PLANNING_ID_PATTERN = re.compile(
    r"(D-[0-9]{2}|ASIS-[0-9]{2}|UI-[0-9]{2}|ADDR-[0-9]{2}|phase [0-9])"
)
_TEMPLATE_KEY_PATTERN = re.compile(r"""t\(\s*["']([^"']+)["']""")


def _load(lang: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_catalogs_have_identical_key_sets():
    assert _load("en").keys() == _load("ru").keys()


def test_no_catalog_value_carries_a_planning_identifier():
    for lang in ("en", "ru"):
        for key, value in _load(lang).items():
            assert not _PLANNING_ID_PATTERN.search(value), (lang, key, value)


def test_every_template_translation_key_exists_in_both_catalogs():
    en = _load("en")
    ru = _load("ru")
    used_keys: set[str] = set()
    for path in TEMPLATES_DIR.rglob("*.html"):
        used_keys.update(_TEMPLATE_KEY_PATTERN.findall(path.read_text(encoding="utf-8")))

    assert used_keys, "expected at least one t(...) call across the templates"
    for key in used_keys:
        assert key in en, f"{key!r} missing from en.json"
        assert key in ru, f"{key!r} missing from ru.json"


def test_every_verdict_reason_key_exists_in_both_catalogs():
    """`analyze` picks a reason key at runtime and the template renders it
    as t(verdict.reason_key), so the template scan above cannot see these.
    They are the one family of keys a missing entry would only reveal by
    showing the operator a bare key instead of a sentence.
    """
    en = _load("en")
    ru = _load("ru")
    for key in ("reason.codec", "reason.level", "reason.no_audio",
                "reason.pixfmt", "reason.profile", "reason.unreadable"):
        assert key in en, f"{key!r} missing from en.json"
        assert key in ru, f"{key!r} missing from ru.json"
