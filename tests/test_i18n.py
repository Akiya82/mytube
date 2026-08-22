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
# A reason key as it appears in source: quoted, so the same identifier
# mentioned in a docstring or a comment is not collected.
_REASON_KEY_PATTERN = re.compile(r"""["']((?:failed\.)?reason\.[a-z_]+)["']""")


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


def test_every_failure_reason_key_in_the_source_exists_in_both_catalogs():
    """`analyze` and the worker's failure handler pick a reason key at
    runtime and the templates render it as `t(verdict.reason_key)` /
    `t(row.failure_reason)`, so the template scan above cannot see any of
    them. They are the one family of keys where a missing entry surfaces
    only as a bare key shown to the operator instead of a sentence.

    Collected from the source rather than listed here, so a reason added
    to the worker without a catalog line fails this test instead of
    waiting to be noticed on screen.
    """
    en = _load("en")
    ru = _load("ru")

    found: set[str] = set()
    for pattern in ("orchestrator/**/*.py", "orchestrator/**/*.html"):
        for path in REPO_ROOT.glob(pattern):
            found.update(
                _REASON_KEY_PATTERN.findall(path.read_text(encoding="utf-8"))
            )

    # The mapper's own five, as a floor: a scan that silently collected
    # nothing would otherwise pass.
    assert {
        "reason.storage_full",
        "reason.unreadable",
        "failed.reason.remux",
        "failed.reason.upload",
        "failed.reason.unknown",
    } <= found

    for key in sorted(found):
        assert key in en, f"{key!r} missing from en.json"
        assert key in ru, f"{key!r} missing from ru.json"
