# Contributing

Issues and pull requests are read and answered. If you open one, expect a
reply, even when the reply is a no.

## Environment

```bash
python3 -m venv venv
./venv/bin/pip install -e '.[dev]'
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

The last line makes `git blame` skip the one commit that only reformatted
the tree. It is local configuration, so every clone needs it once.

## Before you open a pull request

```bash
./venv/bin/ruff check .
./venv/bin/ruff format --check .
./venv/bin/python -m pytest tests -q
```

Both ruff commands block a pull request, as does a failing test. Run pytest
as a module (`python -m pytest`) rather than the `pytest` binary.

A few tests depend on the machine and can fail for reasons unrelated to your
change; for example
`test_a_real_burn_in_opens_a_subtitle_filename_with_an_apostrophe` needs an
ffmpeg built with libass. Compare the set of failures before and after your
change instead of looking at the exit code.

## Commits

Conventional Commits, in English: `feat:`, `fix:`, `docs:`, `test:`,
`refactor:`, `chore:`, with the area in parentheses when it helps, for
example `fix(edge): keep the 206 status in the cache`. Keep one logical
change per commit.

Code that needs a comment to be understood is usually code that should be
clearer. Comment what the code cannot say: a measured constraint, a platform
trap, a reason a plausible alternative was rejected.

## After you open a pull request

The checks above run on the pull request. A maintainer reviews it, may ask
for changes, and either merges it or explains why not. Describe what changed,
why, and how you verified it; the template asks for exactly that.
