"""No planning identifier reaches a person using this tool.

Walks every Python file under `orchestrator/` with the standard library's
own `ast` module and collects exactly two kinds of string literal: the
first positional argument of a call to `typer.echo(...)` or the builtin
`print(...)` (this codebase uses both -- `cli/cache_report.py` prints
exclusively through the builtin), and any literal passed as the `help`
keyword of a call (covers `typer.Option(..., help=...)`,
`typer.Argument(..., help=...)` and `@app.command(..., help=...)` alike).

Docstrings, comments and ordinary variables are deliberately **not**
collected -- per `.claude/CLAUDE.md` "Planning traces never reach the
user", an identifier in a comment is a pointer for a maintainer once the
reason is given in words, and is allowed there. An identifier in output or
in `--help` text is someone else's internal bookkeeping spilling out of the
tool, which is what this guard exists to catch.

The command line is not the only surface any more. The interface added
this phase puts user-facing text in two more places that the Python walk
cannot see: the rendered body of `orchestrator/web/templates/*.html`, and
every value in `orchestrator/locales/{en,ru}.json`. Both get their own
pass below, through the same pattern.

**Jinja comments are stripped before a template is checked.** `{# ... #}`
never reaches the browser, so an identifier inside one is a maintainer's
pointer exactly like a Python comment, and is allowed there -- which is
where every identifier in the current templates lives.

**CSS and JavaScript comments are not checked, deliberately.**
`static/style.css` and `static/app.js` are served to the browser verbatim,
so their comments are technically "shipped". They are still source
comments: nobody reads them as product text, this repository is public and
its whole source is readable anyway, and treating them as output would
mean the rule forbids explaining a stylesheet at all. They are governed by
the same allowance as a Python comment -- reason in words, identifier as a
trailing pointer.

The identifier pattern is kept in a module-level constant, deliberately
never interpolated into any string this module itself passes to
`typer.echo`/`print`/`help=` -- this file is not under `orchestrator/` and
is not walked by the guard, but the constant is kept isolated on principle
so a future move of this file could not make it fail on its own source.
"""
import ast
import json
import re
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
TEMPLATES_DIR = ORCHESTRATOR_DIR / "web" / "templates"
LOCALES_DIR = ORCHESTRATOR_DIR / "locales"

# `{# ... #}`, possibly spanning lines. Replaced by its own newlines so the
# reported line numbers still point at the real file.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)

# Inline SVG geometry. `<path d="M5 15H4a1 1 0 0 1-1-1V4...">` is a list of
# coordinates, and "M5" reads to the identifier pattern as a milestone
# reference. Dropped before matching -- it carries no text, so nothing a
# reader could see is lost by not checking it.
_SVG_GEOMETRY = re.compile(r'\s(?:d|viewBox|points|transform)="[^"]*"')

# Requirement IDs (see .planning/REQUIREMENTS.md prefixes), decision IDs
# (D-NN), verdict references (T-NN-XX, MNN/mNN, BNN), and phase/plan/task/
# criterion numbers in either language -- the exact list CLAUDE.md names.
_ID_PATTERN = re.compile(
    r"("
    r"D-[0-9]{2}"
    r"|ASIS-[0-9]{2}|ADDR-[0-9]{2}|DELIV-[0-9]{2}|DIST-[0-9]{2}"
    r"|CAT-[0-9]{2}|UI-[0-9]{2}|CONV-[0-9]{2}|PLST-[0-9]{2}|WORK-[0-9]{2}"
    r"|T-[0-9]{2}-[A-Za-z]+"
    r"|\b[Mm][0-9]{1,2}\b|\bB[0-9]{1,2}\b"
    r"|[Pp]hase [0-9]|[Pp]lan [0-9]|[Tt]ask [0-9]|[Cc]riterion [0-9]"
    r"|фаз[аеы]|верди[кс]т"
    r")"
)


def _literal_text(node: ast.AST) -> str | None:
    """The literal text of a plain string constant, or the concatenated
    literal parts of an f-string (its `Constant` pieces -- interpolated
    values are skipped, since only the literal text can carry a hardcoded
    identifier).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value for part in node.values if isinstance(part, ast.Constant)
        )
    return None


def _is_output_call(func: ast.AST) -> bool:
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "echo":
        return True
    return False


def collect_user_facing_strings(root: Path) -> list[tuple[Path, int, str]]:
    """Every string this guard cares about, from every `.py` file under
    `root`: `(path, line, text)` for each `print`/`typer.echo` first
    argument and each `help=` keyword literal.
    """
    found: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_output_call(node.func) and node.args:
                text = _literal_text(node.args[0])
                if text is not None:
                    found.append((path, node.lineno, text))
            for kw in node.keywords:
                if kw.arg == "help":
                    text = _literal_text(kw.value)
                    if text is not None:
                        found.append((path, node.lineno, text))
    return found


def collect_template_body_lines(root: Path) -> list[tuple[Path, int, str]]:
    """Every line of every template with its Jinja comments and its inline
    SVG geometry removed -- `(path, line, text)`. What is left is what the
    browser can see: body text, attribute values (`alt`, `aria-label`,
    `title` and the rest are user-facing too), and the template's own tags.
    """
    found: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.html")):
        text = _JINJA_COMMENT.sub(
            lambda m: "\n" * m.group(0).count("\n"),
            path.read_text(encoding="utf-8"),
        )
        text = _SVG_GEOMETRY.sub(" ", text)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                found.append((path, lineno, line))
    return found


def collect_catalog_values(locales_dir: Path) -> list[tuple[Path, str, str]]:
    """Every translated string in every catalog -- `(path, key, value)`.
    These are rendered verbatim; nothing strips anything from them.
    """
    found: list[tuple[Path, str, str]] = []
    for path in sorted(locales_dir.glob("*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for key, value in catalog.items():
            found.append((path, key, value))
    return found


def test_no_orchestrator_output_or_help_string_carries_a_planning_identifier():
    offenders = [
        (str(path.relative_to(REPO_ROOT)), lineno, text)
        for path, lineno, text in collect_user_facing_strings(ORCHESTRATOR_DIR)
        if _ID_PATTERN.search(text)
    ]
    assert not offenders, offenders


def test_orchestrator_cli_actually_has_output_and_help_strings_to_check():
    # A guard that silently collects nothing is worse than no guard --
    # confirm the walk actually reaches the CLI's real print/echo/help
    # call sites before trusting its "no offenders" verdict above.
    found = collect_user_facing_strings(ORCHESTRATOR_DIR)
    assert len(found) > 10


def test_no_rendered_template_text_carries_a_planning_identifier():
    offenders = [
        (str(path.relative_to(REPO_ROOT)), lineno, text.strip())
        for path, lineno, text in collect_template_body_lines(TEMPLATES_DIR)
        if _ID_PATTERN.search(text)
    ]
    assert not offenders, offenders


def test_the_template_walk_actually_reaches_the_templates():
    # Same reason as the CLI check above: a walk that collects nothing
    # would report "no offenders" forever.
    assert len(collect_template_body_lines(TEMPLATES_DIR)) > 50


def test_no_catalog_value_carries_a_planning_identifier():
    offenders = [
        (str(path.relative_to(REPO_ROOT)), key, value)
        for path, key, value in collect_catalog_values(LOCALES_DIR)
        if _ID_PATTERN.search(value)
    ]
    assert not offenders, offenders


def test_the_catalog_walk_actually_reaches_both_catalogs():
    paths = {path.name for path, _, _ in collect_catalog_values(LOCALES_DIR)}
    assert paths == {"en.json", "ru.json"}


def test_a_jinja_comment_is_stripped_but_body_text_is_not(tmp_path):
    """The distinction the template pass rests on: an identifier a
    maintainer left in `{# #}` is a pointer the browser never sees, and one
    in body text is output.
    """
    (tmp_path / "page.html").write_text(
        "{# rendered per D-49, the poll interval #}\n"
        "<p>Waiting</p>\n",
        encoding="utf-8",
    )
    lines = [text for _, _, text in collect_template_body_lines(tmp_path)]
    assert lines == ["<p>Waiting</p>"]

    (tmp_path / "leaky.html").write_text("<p>Waiting per D-49</p>\n", encoding="utf-8")
    offenders = [
        text
        for path, _, text in collect_template_body_lines(tmp_path)
        if path.name == "leaky.html" and _ID_PATTERN.search(text)
    ]
    assert offenders == ["<p>Waiting per D-49</p>"]


def test_a_catalog_value_carrying_an_identifier_is_caught(tmp_path):
    (tmp_path / "xx.json").write_text(
        '{"library.status.working": "Working (D-49), about {estimate} left"}',
        encoding="utf-8",
    )
    offenders = [
        value
        for _, _, value in collect_catalog_values(tmp_path)
        if _ID_PATTERN.search(value)
    ]
    assert offenders == ["Working (D-49), about {estimate} left"]


# --- Fixture cases proving the guard's own behavior, not the real tree ---


def test_guard_flags_an_identifier_added_to_a_help_string(tmp_path):
    (tmp_path / "cmd.py").write_text(
        textwrap.dedent(
            '''
            import typer

            def f(x: str = typer.Option(..., help="Confirm reuse (D-99)")):
                pass
            '''
        ),
        encoding="utf-8",
    )

    offenders = [
        text for _, _, text in collect_user_facing_strings(tmp_path) if _ID_PATTERN.search(text)
    ]
    assert offenders == ["Confirm reuse (D-99)"]


def test_guard_passes_again_once_the_identifier_is_removed(tmp_path):
    (tmp_path / "cmd.py").write_text(
        textwrap.dedent(
            '''
            import typer

            def f(x: str = typer.Option(..., help="Confirm reuse of a deleted title's address")):
                pass
            '''
        ),
        encoding="utf-8",
    )

    offenders = [
        text for _, _, text in collect_user_facing_strings(tmp_path) if _ID_PATTERN.search(text)
    ]
    assert offenders == []


def test_docstring_and_comment_identifiers_are_never_collected(tmp_path):
    (tmp_path / "mod.py").write_text(
        textwrap.dedent(
            '''
            """Module docstring naming D-10, safe to keep as a pointer."""
            import typer

            def f():
                # comment naming D-10 as well, also a pointer, also safe.
                typer.echo("nothing planning-related here")
            '''
        ),
        encoding="utf-8",
    )

    found = collect_user_facing_strings(tmp_path)
    assert found == [(tmp_path / "mod.py", 7, "nothing planning-related here")]


def test_guard_collects_bare_print_as_well_as_typer_echo(tmp_path):
    (tmp_path / "report.py").write_text(
        textwrap.dedent(
            '''
            def f():
                print("violates D-99 right here")
            '''
        ),
        encoding="utf-8",
    )

    offenders = [
        text for _, _, text in collect_user_facing_strings(tmp_path) if _ID_PATTERN.search(text)
    ]
    assert offenders == ["violates D-99 right here"]
