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
# A catalog key as it appears in source: quoted and dotted, so the same
# identifier mentioned in a docstring or a comment is not collected.
# Deliberately not a list of key families -- see
# test_every_runtime_selected_key_in_the_source_exists_in_both_catalogs.
_SOURCE_KEY_PATTERN = re.compile(
    r"""["']([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)["']"""
)
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


def _keys_referenced_in_the_source() -> set[str]:
    """Every dotted identifier in the source that sits in a namespace the
    catalogs serve.

    The namespaces are read out of the catalog rather than listed here,
    which is the whole point: `reason.*`, `failed.reason.*` and
    `playlist.refused.*` are all collected because `reason`, `failed` and
    `playlist` are catalog namespaces, and so is the next family somebody
    adds without touching this file. A dotted identifier in a namespace no
    catalog uses -- a log tag, an internal signal -- is not a translation
    key and is not collected.

    Template file names are the one shape that reads like a key and is
    not one, so `render("library.html")` is excluded explicitly.
    """
    namespaces = {key.split(".")[0] for key in _load("en")}
    found: set[str] = set()
    for pattern in ("orchestrator/**/*.py", "orchestrator/**/*.html"):
        for path in REPO_ROOT.glob(pattern):
            for candidate in _SOURCE_KEY_PATTERN.findall(
                path.read_text(encoding="utf-8")
            ):
                if candidate.endswith(".html"):
                    continue
                if candidate.split(".")[0] in namespaces:
                    found.add(candidate)
    return found


def test_every_runtime_selected_key_in_the_source_exists_in_both_catalogs():
    """`analyze`, the add worker and the playlist build worker each pick a
    catalog key at runtime, and the templates render it as
    `t(verdict.reason_key)` / `t(row.failure_reason)` /
    `reason_text(row.refused_reason_key, ...)`. The template scan above
    cannot see any of them: a missing entry surfaces only as a bare key
    shown to the operator instead of a sentence.

    Collected from the source by namespace rather than by an enumeration
    of key families. The enumeration is what failed before: this test
    matched `reason.*` and `failed.reason.*` only, so when the playlist
    work introduced `playlist.refused.*` -- a second family selected the
    same way and rendered the same way -- it landed outside the guard, and
    a key added there without a catalog line would have reached the
    operator with nothing failing.
    """
    en = _load("en")
    ru = _load("ru")

    found = _keys_referenced_in_the_source()

    # A floor across all three families, so a scan that silently collected
    # nothing -- or stopped seeing one of them -- cannot pass.
    assert {
        "reason.storage_full",
        "reason.unreadable",
        "failed.reason.remux",
        "failed.reason.upload",
        "failed.reason.unknown",
        "playlist.refused.empty",
        "playlist.refused.storage_full",
        "playlist.refused.checks",
        "playlist.refused.assembly",
    } <= found

    for key in sorted(found):
        assert key in en, f"{key!r} missing from en.json"
        assert key in ru, f"{key!r} missing from ru.json"
