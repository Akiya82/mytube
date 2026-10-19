"""No planning identifier ships.

The project is planned outside this repository, so a pointer at that
planning -- a requirement, decision, threat or review-finding id, a phase,
plan or task number, the name of a planning document, a UI-spec screen
label -- leads a reader to a file they will never have. Everything below
fails the build when one is written into something that ships.

Two compiled patterns define "pointer". `_ID_PATTERN` covers the id
families and the phase/plan/task/criterion wording, in English and Russian.
`_POINTER_PATTERN` covers the families `_ID_PATTERN` does not: threat ids
with a dotted or lettered tail, planning-document names, review-finding
ids, UI-spec screen and amendment labels, and bare plan numbers such as
`04-11`. It is a separate pattern so the two families can be tested at
their edges independently; `_has_pointer` applies both, and every pass
uses it.

**Output surfaces.** What a person reads while using the tool:

- the first argument of `typer.echo(...)` and the builtin `print(...)`, and
  the `help=` keyword of any call, under `orchestrator/`;
- the rendered body of `orchestrator/web/templates/*.html`, with inline
  SVG geometry removed (`M5 15H4...` reads as a milestone reference but is
  coordinates);
- every value in `orchestrator/locales/*.json`;
- the double-quoted strings the shell tools print;
- the first argument of logging calls and of `raise` in `worker/` and
  `mytube_media/`.

**Source surfaces.** This repository is public and its whole source is
read, so a comment or a docstring is as published as a help string:

- every `#` comment and every module, class and function docstring in
  `orchestrator/`, `worker/`, `mytube_media/`, `tools/`, `tests/` and
  `migrations/` (comments through `tokenize`, because `ast` does not
  expose them);
- the body of every Jinja `{# ... #}` comment, which the body pass above
  removes on purpose and this one reads;
- the `/* ... */` and `//` comments of `orchestrator/static/style.css` and
  `orchestrator/static/app.js`;
- the full-line comments of the sample environment file, both compose
  files, the Dockerfiles, the shell entrypoints and tools, `.gitignore`
  and the nginx templates under `edge/`;
- every line of the Markdown files at the repository root and of the
  Markdown and YAML files under `.github/`.

**One file is excluded: this one.** It contains the patterns it searches
for and the fixture strings its self-checks feed to its collectors. The
excluded set is a single named path and a test pins its size, so a failure
cannot be silenced by widening it.

**`docs/experiments/` is out of reach on purpose, not by exclusion.** Those
records are prose about measurement. They legitimately write "phase of the
conversion" and number their own steps, so a pass over them would fire on
ordinary English and be switched off within a month. They were cleaned by
hand, and no collector here looks inside `docs/`.

Every collector has a test on a small `tmp_path` tree that asserts the
exact list it returns, and a walk-reach test on the real tree, so a walk
that silently collects nothing cannot report a clean repository.
"""

import ast
import io
import json
import re
import textwrap
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
TEMPLATES_DIR = ORCHESTRATOR_DIR / "web" / "templates"
LOCALES_DIR = ORCHESTRATOR_DIR / "locales"
TOOLS_DIR = REPO_ROOT / "tools"
WORKER_DIR = REPO_ROOT / "worker"
MYTUBE_MEDIA_DIR = REPO_ROOT / "mytube_media"
TESTS_DIR = REPO_ROOT / "tests"
MIGRATIONS_DIR = REPO_ROOT / "migrations"
GITHUB_DIR = REPO_ROOT / ".github"

# The stylesheet and the script are served to the browser verbatim. Kept as
# constants so moving the directory is a two-line change that the reach
# check below catches if it is forgotten.
STATIC_DIR = ORCHESTRATOR_DIR / "static"
STYLESHEET = STATIC_DIR / "style.css"
SCRIPT = STATIC_DIR / "app.js"

# Every directory whose Python comments and docstrings are checked.
PYTHON_COMMENT_DIRS = (
    ORCHESTRATOR_DIR,
    WORKER_DIR,
    MYTUBE_MEDIA_DIR,
    TOOLS_DIR,
    TESTS_DIR,
    MIGRATIONS_DIR,
)

# The only file no source-surface pass reads: this one.
EXCLUDED_FILES = frozenset({Path(__file__).resolve()})

# The sample environment file, both compose files, both Dockerfiles, the
# shell entrypoints and tools, `.gitignore` and the nginx templates -- the
# shipped configuration an operator reads, none of it Python.
CONFIG_FILES_FOR_COMMENT_SCAN = (
    REPO_ROOT / ".env.example",
    REPO_ROOT / ".gitignore",
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker-compose.worker.yml",
    REPO_ROOT / "Dockerfile",
    REPO_ROOT / "Dockerfile.worker",
    REPO_ROOT / "docker-entrypoint.sh",
    REPO_ROOT / "backup" / "backup.sh",
    REPO_ROOT / "storage" / "entrypoint.sh",
    *sorted((REPO_ROOT / "edge").glob("*.template")),
    *sorted(TOOLS_DIR.glob("*.sh")),
)

# The standard logging methods -- collected off any attribute access with
# one of these names, not off a specific logger variable, so a second
# module-level logger (or a differently-named one) is caught the same way.
_LOG_METHOD_NAMES = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

# A double-quoted string on a line that is not wholly a comment. The shell
# tools print through several helpers of their own (`pass`, `fail`, `note`)
# as well as bare `echo`, so matching the quoted text rather than the
# command name catches all of them and keeps catching a helper added later.
_SHELL_STRING = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')

# `{# ... #}`, possibly spanning lines. The body pass replaces it by its own
# newlines so the reported line numbers still point at the real file; the
# comment pass reads what it matched.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)

# Inline SVG geometry. `<path d="M5 15H4a1 1 0 0 1-1-1V4...">` is a list of
# coordinates, and "M5" reads to the identifier pattern as a milestone
# reference. Dropped before matching -- it carries no text, so nothing a
# reader could see is lost by not checking it.
_SVG_GEOMETRY = re.compile(r'\s(?:d|viewBox|points|transform)="[^"]*"')

# Requirement ids, decision ids (D-NN), verdict references (T-NN-XX,
# MNN/mNN, BNN), and phase/plan/task/criterion wording in either language.
_ID_PATTERN = re.compile(
    r"("
    r"D-[0-9]{2}"
    r"|ASIS-[0-9]{2}|ADDR-[0-9]{2}|DELIV-[0-9]{2}|DIST-[0-9]{2}"
    r"|AUTH-[0-9]{2}"
    r"|CAT-[0-9]{2}|UI-[0-9]{2}|CONV-[0-9]{2}|PLST-[0-9]{2}|WORK-[0-9]{2}"
    r"|T-[0-9]{2}-[A-Za-z]+"
    r"|\b[Mm][0-9]{1,2}\b|\bB[0-9]{1,2}\b"
    r"|[Pp]hase [0-9]|[Pp]lan [0-9]|[Tt]ask [0-9]|[Cc]riterion [0-9]"
    r"|фаз[аеы]|верди[кс]т"
    r")"
)

# The families `_ID_PATTERN` does not cover: threat ids (`T-02.1-84`,
# `T-06-22b`), planning-document names, review-finding ids, UI-spec screen
# and amendment labels, and bare plan numbers. A bare plan number must start
# at a boundary and not continue into a longer dashed token, so dates
# (`2026-09-14`) and file names (`02-05-avpro-redirect-and-cache.md`) pass.
_POINTER_PATTERN = re.compile(
    r"\bT-[0-9]{2}(?:\.[0-9]+)?-[0-9]{2}[A-Za-z]?\b"
    r"|\b[0-9]{2}(?:\.[0-9]+)?-(?:UI-SPEC|CONTEXT|RESEARCH|PLAN|SUMMARY|VALIDATION"
    r"|VERIFICATION|UAT|HUMAN-UAT|PATTERNS|REVIEW|SECURITY|DISCUSSION-LOG)\b"
    r"|\bUI-SPEC\b"
    r"|\b(?:ROADMAP|REQUIREMENTS|STATE|PROJECT)\.md\b"
    r"|\.planning/"
    r"|\b(?:CR|WR|IN)-[0-9]{2}\b"
    r"|\bScreen [A-Z0-9]\b"
    r"|\bAmendment [0-9]+\b"
    r"|(?<![0-9A-Za-z.-])0[0-9](?:\.[0-9])?-[0-9]{2}(?![0-9A-Za-z-])"
)


def _has_pointer(text: str) -> bool:
    return bool(_ID_PATTERN.search(text) or _POINTER_PATTERN.search(text))


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


def collect_shell_output_strings(root: Path) -> list[tuple[Path, int, str]]:
    """Every double-quoted string on a non-comment line of every `.sh` under
    `root` -- `(path, line, text)`.

    The shell tools print straight to an operator's terminal, which makes
    them the same kind of surface as the command line.

    A line that is entirely a comment is skipped here: it is not output, and
    `collect_config_comment_lines` reads it.
    """
    found: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.sh")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            for match in _SHELL_STRING.finditer(line):
                found.append((path, lineno, match.group(1)))
    return found


def test_no_shell_tool_output_carries_a_planning_identifier():
    offenders = [
        (str(path.relative_to(REPO_ROOT)), lineno, text)
        for path, lineno, text in collect_shell_output_strings(TOOLS_DIR)
        if _has_pointer(text)
    ]
    assert not offenders, offenders


def test_the_shell_walk_actually_reaches_the_tools():
    # Same reason as the other walks: one that collects nothing would
    # report "no offenders" forever. The smoke tests alone carry far more
    # than this many quoted strings.
    assert len(collect_shell_output_strings(TOOLS_DIR)) > 50


def test_a_shell_comment_is_skipped_but_an_output_string_is_not(tmp_path):
    """The distinction this pass rests on: a comment line is not output, and
    `collect_config_comment_lines` reads it instead.
    """
    (tmp_path / "t.sh").write_text(
        "# Step 13: the cache-off switch (DELIV-08).\n"
        'pass "step 13: cache off, bytes still served"\n'
        'fail "step 13 (DELIV-08): expected 206"\n',
        encoding="utf-8",
    )
    offenders = [
        text
        for _, _, text in collect_shell_output_strings(tmp_path)
        if _has_pointer(text)
    ]
    assert offenders == ["step 13 (DELIV-08): expected 206"]


def collect_config_comment_lines(paths) -> list[tuple[Path, int, str]]:
    """Every full-line comment's text (the part after `#`, whitespace
    stripped) from the given files -- `(path, line, text)`.

    A line that is a comment for only part of its length (real content
    followed by a trailing `# ...`) is not collected.
    """
    found: list[tuple[Path, int, str]] = []
    for path in paths:
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                found.append((path, lineno, stripped[1:].strip()))
    return found


def collect_message_strings(root: Path) -> list[tuple[Path, int, str]]:
    """Every string this guard cares about from every `.py` file under
    `root`: `(path, line, text)` for the first positional argument of a
    logging call (any `x.info(...)`/`x.warning(...)`/... in
    `_LOG_METHOD_NAMES`, matched on the attribute name rather than a
    specific logger variable) and of a `raise` of any exception class.

    These reach an operator's log and the service's own error paths --
    the same kind of surface `collect_user_facing_strings` already covers
    for `typer.echo`/`print` in `orchestrator/`, which this walk never
    reaches.
    """
    found: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in _LOG_METHOD_NAMES
                    and node.args
                ):
                    text = _literal_text(node.args[0])
                    if text is not None:
                        found.append((path, node.lineno, text))
            elif (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and node.exc.args
            ):
                text = _literal_text(node.exc.args[0])
                if text is not None:
                    found.append((path, node.lineno, text))
    return found


def test_no_shipped_config_comment_carries_a_planning_pointer():
    offenders = [
        (str(path.relative_to(REPO_ROOT)), lineno, text)
        for path, lineno, text in collect_config_comment_lines(
            CONFIG_FILES_FOR_COMMENT_SCAN
        )
        if _has_pointer(text)
    ]
    assert not offenders, offenders


def test_the_config_comment_walk_actually_reaches_the_files():
    # Same reason as every other walk here: one that silently collects
    # nothing would report "no offenders" forever.
    found = collect_config_comment_lines(CONFIG_FILES_FOR_COMMENT_SCAN)
    assert len(found) > 100
    reached = {path.name for path, _, _ in found}
    assert {
        ".env.example",
        "docker-compose.yml",
        "Dockerfile",
        "nginx.conf.template",
    } <= reached


def test_a_config_comment_identifier_is_flagged_wherever_it_stands(tmp_path):
    sample = tmp_path / "sample.env"
    sample.write_text(
        "# D-99: leads, so this stands in for the reason instead of following it\n"
        "# the actual reason, with the identifier trailing instead (D-99)\n"
        "# a comment with nothing to flag\n",
        encoding="utf-8",
    )
    offenders = [
        text
        for _, _, text in collect_config_comment_lines([sample])
        if _has_pointer(text)
    ]
    assert offenders == [
        "D-99: leads, so this stands in for the reason instead of following it",
        "the actual reason, with the identifier trailing instead (D-99)",
    ]


def test_no_worker_or_media_message_string_carries_a_planning_identifier():
    offenders = [
        (str(path.relative_to(REPO_ROOT)), lineno, text)
        for path, lineno, text in (
            collect_message_strings(WORKER_DIR)
            + collect_message_strings(MYTUBE_MEDIA_DIR)
        )
        if _has_pointer(text)
    ]
    assert not offenders, offenders


def test_the_message_string_walk_actually_reaches_worker_and_media():
    found = collect_message_strings(WORKER_DIR) + collect_message_strings(
        MYTUBE_MEDIA_DIR
    )
    assert len(found) > 15


def test_a_logging_message_and_a_docstring_identifier_are_each_caught_by_their_own_pass(
    tmp_path,
):
    (tmp_path / "mod.py").write_text(
        textwrap.dedent(
            '''
            """Module docstring naming D-10."""
            import logging

            logger = logging.getLogger(__name__)

            def f():
                logger.warning("refuses the fallback (D-10)")
            '''
        ),
        encoding="utf-8",
    )
    messages = [
        text for _, _, text in collect_message_strings(tmp_path) if _has_pointer(text)
    ]
    assert messages == ["refuses the fallback (D-10)"]
    documentation = [
        text
        for _, _, text in collect_python_comments_and_docstrings([tmp_path])
        if _has_pointer(text)
    ]
    assert documentation == ["Module docstring naming D-10."]


def test_no_orchestrator_output_or_help_string_carries_a_planning_identifier():
    offenders = [
        (str(path.relative_to(REPO_ROOT)), lineno, text)
        for path, lineno, text in collect_user_facing_strings(ORCHESTRATOR_DIR)
        if _has_pointer(text)
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
        if _has_pointer(text)
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
        if _has_pointer(value)
    ]
    assert not offenders, offenders


def test_the_catalog_walk_actually_reaches_both_catalogs():
    paths = {path.name for path, _, _ in collect_catalog_values(LOCALES_DIR)}
    assert paths == {"en.json", "ru.json"}


def test_a_jinja_comment_is_left_out_of_the_body_pass_but_read_by_its_own(tmp_path):
    """The body pass removes `{# #}` because the browser never sees it; the
    comment pass reads exactly what the body pass removed.
    """
    (tmp_path / "page.html").write_text(
        "{# rendered per D-49, the poll interval #}\n<p>Waiting</p>\n",
        encoding="utf-8",
    )
    lines = [text for _, _, text in collect_template_body_lines(tmp_path)]
    assert lines == ["<p>Waiting</p>"]
    comments = [text for _, _, text in collect_template_comment_bodies(tmp_path)]
    assert comments == ["{# rendered per D-49, the poll interval #}"]

    (tmp_path / "leaky.html").write_text("<p>Waiting per D-49</p>\n", encoding="utf-8")
    offenders = [
        text
        for path, _, text in collect_template_body_lines(tmp_path)
        if path.name == "leaky.html" and _has_pointer(text)
    ]
    assert offenders == ["<p>Waiting per D-49</p>"]


def test_a_catalog_value_carrying_an_identifier_is_caught(tmp_path):
    (tmp_path / "xx.json").write_text(
        '{"library.status.working": "Working (D-49), about {estimate} left"}',
        encoding="utf-8",
    )
    offenders = [
        value for _, _, value in collect_catalog_values(tmp_path) if _has_pointer(value)
    ]
    assert offenders == ["Working (D-49), about {estimate} left"]


# --- Fixture cases proving the guard's own behavior, not the real tree ---


def test_guard_flags_an_identifier_added_to_a_help_string(tmp_path):
    (tmp_path / "cmd.py").write_text(
        textwrap.dedent(
            """
            import typer

            def f(x: str = typer.Option(..., help="Confirm reuse (D-99)")):
                pass
            """
        ),
        encoding="utf-8",
    )

    offenders = [
        text
        for _, _, text in collect_user_facing_strings(tmp_path)
        if _has_pointer(text)
    ]
    assert offenders == ["Confirm reuse (D-99)"]


def test_guard_passes_again_once_the_identifier_is_removed(tmp_path):
    (tmp_path / "cmd.py").write_text(
        textwrap.dedent(
            """
            import typer

            def f(x: str = typer.Option(..., help="Confirm reuse of a deleted title's address")):
                pass
            """
        ),
        encoding="utf-8",
    )

    offenders = [
        text
        for _, _, text in collect_user_facing_strings(tmp_path)
        if _has_pointer(text)
    ]
    assert offenders == []


def test_docstring_and_comment_identifiers_are_collected_by_the_comment_pass_only(
    tmp_path,
):
    (tmp_path / "mod.py").write_text(
        textwrap.dedent(
            '''
            """Module docstring naming D-10."""
            import typer

            def f():
                # comment naming D-10 as well.
                typer.echo("nothing planning-related here")
            '''
        ),
        encoding="utf-8",
    )

    assert collect_user_facing_strings(tmp_path) == [
        (tmp_path / "mod.py", 7, "nothing planning-related here")
    ]
    assert collect_python_comments_and_docstrings([tmp_path]) == [
        (tmp_path / "mod.py", 2, "Module docstring naming D-10."),
        (tmp_path / "mod.py", 6, "comment naming D-10 as well."),
    ]


def test_guard_collects_bare_print_as_well_as_typer_echo(tmp_path):
    (tmp_path / "report.py").write_text(
        textwrap.dedent(
            """
            def f():
                print("violates D-99 right here")
            """
        ),
        encoding="utf-8",
    )

    offenders = [
        text
        for _, _, text in collect_user_facing_strings(tmp_path)
        if _has_pointer(text)
    ]
    assert offenders == ["violates D-99 right here"]


# --- Source surfaces: comments, docstrings, stylesheet, script, documents ---

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# `//` to the end of the line, except after a colon (`https://`).
_LINE_COMMENT = re.compile(r"(?<!:)//(.*)$", re.MULTILINE)
_DOCUMENT_SUFFIXES = frozenset({".md", ".yml", ".yaml"})


def collect_python_comments_and_docstrings(roots) -> list[tuple[Path, int, str]]:
    """Every `#` comment and every module, class and function docstring in
    every `.py` under the given directories -- `(path, line, text)`, in line
    order per file. The excluded files are skipped.

    Comments come from `tokenize`, because `ast` drops them; docstrings come
    from the tree.
    """
    found: list[tuple[Path, int, str]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path in EXCLUDED_FILES:
                continue
            source = path.read_text(encoding="utf-8")
            entries: list[tuple[int, str]] = []
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    entries.append((token.start[0], token.string.lstrip("#").strip()))
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if not isinstance(
                    node,
                    (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    continue
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr):
                    text = _literal_text(first.value)
                    if text is not None:
                        entries.append((first.lineno, text))
            found.extend((path, lineno, text) for lineno, text in sorted(entries))
    return found


def collect_template_comment_bodies(root: Path) -> list[tuple[Path, int, str]]:
    """Every Jinja `{# ... #}` comment in every template under `root`,
    whole -- `(path, line of its opening, text)`. The body pass removes
    exactly these, so together the two passes read every byte of a template.
    """
    found: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in _JINJA_COMMENT.finditer(text):
            found.append((path, text.count("\n", 0, match.start()) + 1, match.group(0)))
    return found


def collect_asset_comments(paths) -> list[tuple[Path, int, str]]:
    """Every `/* ... */` block and every `//` line comment in the given
    stylesheet and script files -- `(path, line of its opening, text)`.

    A `//` after a colon is a URL, not a comment. A `//` inside a string
    literal that is not a URL would be collected too; the two files have
    none, and the walk-reach test would not hide one.
    """
    found: list[tuple[Path, int, str]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in _BLOCK_COMMENT.finditer(text):
            found.append((path, text.count("\n", 0, match.start()) + 1, match.group(0)))
        # Blank the blocks out (keeping their newlines) so a `//` inside one
        # is not read a second time.
        rest = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
        for match in _LINE_COMMENT.finditer(rest):
            found.append(
                (path, rest.count("\n", 0, match.start()) + 1, match.group(1).strip())
            )
    return sorted(found, key=lambda item: (item[0], item[1]))


def collect_document_lines(root: Path) -> list[tuple[Path, int, str]]:
    """Every non-blank line of the Markdown files at the top of `root` and of
    the Markdown and YAML files anywhere under `root/.github` --
    `(path, line, text)`. A missing `.github` is nothing to collect.
    """
    paths = sorted(root.glob("*.md"))
    github = root / ".github"
    if github.is_dir():
        paths += sorted(p for p in github.rglob("*") if p.suffix in _DOCUMENT_SUFFIXES)
    found: list[tuple[Path, int, str]] = []
    for path in paths:
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.strip():
                found.append((path, lineno, line))
    return found


def _relative_offenders(found) -> list[tuple[str, int, str]]:
    return [
        (str(path.relative_to(REPO_ROOT)), lineno, text.strip()[:120])
        for path, lineno, text in found
        if _has_pointer(text)
    ]


def test_no_python_comment_or_docstring_carries_a_planning_pointer():
    found = collect_python_comments_and_docstrings(PYTHON_COMMENT_DIRS)
    assert not _relative_offenders(found), _relative_offenders(found)


def test_the_python_comment_walk_actually_reaches_every_directory():
    found = collect_python_comments_and_docstrings(PYTHON_COMMENT_DIRS)
    reached = {path.relative_to(REPO_ROOT).parts[0] for path, _, _ in found}
    assert reached == {d.name for d in PYTHON_COMMENT_DIRS}
    assert len(found) > 2000


def test_a_python_comment_and_a_docstring_are_both_collected(tmp_path):
    (tmp_path / "mod.py").write_text(
        textwrap.dedent(
            '''
            """Module docstring."""

            class C:
                """Class docstring."""

                def f(self):
                    """Function docstring."""
                    x = "a # not a comment"  # trailing comment
                    return x

            # last comment
            '''
        ),
        encoding="utf-8",
    )
    assert collect_python_comments_and_docstrings([tmp_path]) == [
        (tmp_path / "mod.py", 2, "Module docstring."),
        (tmp_path / "mod.py", 5, "Class docstring."),
        (tmp_path / "mod.py", 8, "Function docstring."),
        (tmp_path / "mod.py", 9, "trailing comment"),
        (tmp_path / "mod.py", 12, "last comment"),
    ]


def test_the_only_excluded_file_is_the_guard_itself(tmp_path):
    assert EXCLUDED_FILES == {Path(__file__).resolve()}
    assert len(EXCLUDED_FILES) == 1
    collected = {
        path for path, _, _ in collect_python_comments_and_docstrings([TESTS_DIR])
    }
    assert Path(__file__).resolve() not in collected
    assert collected, "the rest of tests/ is still walked"


def test_the_template_comment_walk_actually_reaches_the_templates():
    assert len(collect_template_comment_bodies(TEMPLATES_DIR)) > 30


def test_no_template_comment_carries_a_planning_pointer():
    found = collect_template_comment_bodies(TEMPLATES_DIR)
    assert not _relative_offenders(found), _relative_offenders(found)


def test_no_stylesheet_or_script_comment_carries_a_planning_pointer():
    found = collect_asset_comments((STYLESHEET, SCRIPT))
    assert not _relative_offenders(found), _relative_offenders(found)


def test_the_asset_comment_walk_actually_reaches_both_files():
    found = collect_asset_comments((STYLESHEET, SCRIPT))
    assert {path.name for path, _, _ in found} == {"style.css", "app.js"}
    assert len(found) > 50


def test_block_and_line_comments_are_collected_but_a_url_is_not(tmp_path):
    asset = tmp_path / "app.js"
    asset.write_text(
        "/* block\n   over two lines // with a slash pair */\n"
        "const url = 'https://example.com/x'; // trailing\n"
        "// whole line\n",
        encoding="utf-8",
    )
    assert collect_asset_comments([asset]) == [
        (asset, 1, "/* block\n   over two lines // with a slash pair */"),
        (asset, 3, "trailing"),
        (asset, 4, "whole line"),
    ]


def test_no_root_markdown_or_github_document_carries_a_planning_pointer():
    found = collect_document_lines(REPO_ROOT)
    assert not _relative_offenders(found), _relative_offenders(found)


def test_the_document_walk_actually_reaches_the_setup_guide():
    found = collect_document_lines(REPO_ROOT)
    assert len([1 for path, _, _ in found if path.name == "SETUP.md"]) > 50


def test_the_document_walk_never_enters_docs():
    assert not [
        p
        for p, _, _ in collect_document_lines(REPO_ROOT)
        if "docs" in p.relative_to(REPO_ROOT).parts
    ]


def test_root_markdown_and_github_files_are_collected_and_a_missing_github_is_not_an_error(
    tmp_path,
):
    (tmp_path / "README.md").write_text("# Title\n\nbody\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not collected\n", encoding="utf-8")
    assert collect_document_lines(tmp_path) == [
        (tmp_path / "README.md", 1, "# Title"),
        (tmp_path / "README.md", 3, "body"),
    ]

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\n# a comment\n", encoding="utf-8")
    (tmp_path / ".github" / "CONTRIBUTING.md").write_text("Welcome\n", encoding="utf-8")
    (tmp_path / ".github" / "icon.png").write_bytes(b"\x89PNG")
    assert collect_document_lines(tmp_path) == [
        (tmp_path / "README.md", 1, "# Title"),
        (tmp_path / "README.md", 3, "body"),
        (tmp_path / ".github" / "CONTRIBUTING.md", 1, "Welcome"),
        (workflows / "ci.yml", 1, "name: ci"),
        (workflows / "ci.yml", 2, "# a comment"),
    ]


# --- The second pattern's edges ---


@pytest.mark.parametrize(
    "text",
    [
        "T-02.1-84",
        "T-06-22b",
        "mitigated, see T-05-13.",
        "05-UI-SPEC.md",
        "UI-SPEC",
        "per .planning/ROADMAP.md",
        "ROADMAP.md",
        "CR-01",
        "WR-05",
        "IN-13",
        "Screen G",
        "Amendment 2",
        "(04-11)",
        "gap closure 05-17",
        "plan 06.1-02",
    ],
)
def test_the_pointer_pattern_matches_what_it_exists_to_catch(text):
    assert _POINTER_PATTERN.search(text), text
    assert _has_pointer(text), text


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-14",
        "docs/experiments/02-05-avpro-redirect-and-cache.md",
        "UTF-8",
        "H.264",
        "x86_64",
        "seaweedfs:3.97",
        "release 1.2-03",
        '<path d="M12 3 1 21h22L12 3Z"/>',
        "ISO-8859-1",
        "a Screen reader",
        "a function in screen.py",
    ],
)
def test_the_pointer_pattern_leaves_ordinary_text_alone(text):
    assert not _POINTER_PATTERN.search(text), text
    assert not _has_pointer(_SVG_GEOMETRY.sub(" ", text)), text
