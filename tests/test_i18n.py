"""Both locale catalogs carry the same key set and the same
substitutions per key, and every key the templates and the worker
reference actually exists in both.

Planning identifiers in catalog values are checked by
`test_no_planning_traces.py` instead -- it owns the full identifier
pattern and now walks the catalogs and the templates as well as the
Python, so a second narrower copy of the same check here could only drift
out of agreement with it.
"""
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "orchestrator" / "locales"
TEMPLATES_DIR = REPO_ROOT / "orchestrator" / "web" / "templates"
_TEMPLATE_KEY_PATTERN = re.compile(r"""t\(\s*["']([^"']+)["']""")
# A reason key as it appears in source: quoted, so the same identifier
# mentioned in a docstring or a comment is not collected.
_REASON_KEY_PATTERN = re.compile(r"""["']((?:failed\.)?reason\.[a-z_]+)["']""")
# A substitution as `format`/`str.format` sees it: `{name}`. Anything else
# inside braces is not a parameter the renderer supplies.
_PLACEHOLDER_PATTERN = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")


def _load(lang: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_catalogs_have_identical_key_sets():
    assert _load("en").keys() == _load("ru").keys()


def test_catalogs_substitute_the_same_placeholders_for_every_key():
    """The same key set is only half of the agreement between the two
    catalogs. A value that references a substitution its counterpart does
    not is a render-time failure for the viewers of that one language, and
    nobody testing in English ever sees it.
    """
    en = _load("en")
    ru = _load("ru")

    parametrised = 0
    for key in sorted(en.keys() & ru.keys()):
        en_names = set(_PLACEHOLDER_PATTERN.findall(en[key]))
        ru_names = set(_PLACEHOLDER_PATTERN.findall(ru[key]))
        parametrised += bool(en_names or ru_names)
        assert en_names == ru_names, (
            f"{key!r} substitutes different names: en={sorted(en_names)} "
            f"ru={sorted(ru_names)}"
        )

    # A catalog pair that collected nothing would otherwise pass silently.
    assert parametrised, "expected at least one key carrying a substitution"


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
