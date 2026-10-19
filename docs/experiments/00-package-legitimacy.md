# Legitimacy of the thirteen initial PyPI packages

**English abstract:** Verified that all thirteen PyPI packages this phase
installs are the real, legitimate packages, not typosquats or hallucinated
names. An automated checker flagged all seven originally-audited packages as
suspicious for a reason unrelated to legitimacy (recency of the latest
release, not package age), so every package was instead confirmed by hand
against its real pypi.org page. Verdict: all thirteen cleared.

Gate before dependencies are first installed. The automated checker
(`gsd-tools query package-legitimacy check --ecosystem pypi`) already ran
against the original seven packages during research and returned `SUS` for
`too-new`/`unknown-downloads` reasons on all seven — a false positive (the
`too-new` signal looks at the date of the **latest** release, not the
package's age). None of the packages received `SLOP`. Six packages beyond
the original seven (`typer`, `httpx`, `sqlalchemy`, `pytest`,
`pytest-asyncio`, `testcontainers`) were never seen by the automated checker
at all — they were added under a documented decision, because
they are genuinely installed by the dependency-installation step. Re-running
the automated checker is pointless — it already produced a false positive on
packages that are known and safe; all thirteen need only the manual check
against pypi.org, done by a person.

**The table below is the automatable part of the preparation.** Opening the
thirteen pages and checking the repository against the "expected repository"
column is a step the owner performs; the executor does not fill in that
column by assumption. The values filled into the "Confirmed" column were
entered from the owner's report dated 2026-08-21 — see the "Verification
date and signature" section below.

## Verification table

| # | Package | Group | pypi.org URL | Expected repository (from the PyPI page) | Version / pin | Age, legitimacy signals | Confirmed |
|---|-------|--------|------------------|-------------------------------------------|--------------|-------------------------------|---------------|
| 1 | `fastapi` | runtime | https://pypi.org/project/fastapi/ | github.com/fastapi/fastapi | pyproject.toml pins the range `>=0.141,<0.150`; as of the research date (2026-08-20) the current release is 0.141.1 (29 July 2026) | 6+ years of history, 250+ releases since 0.1.0 | Yes |
| 2 | `uvicorn` | runtime (`[standard]` extra) | https://pypi.org/project/uvicorn/ | github.com/Kludex/uvicorn (the project moved from `encode/uvicorn` — expected, not a sign of takeover) | not pinned in pyproject.toml, installed as `latest` | 8+ years of release history | Yes |
| 3 | `asyncpg` | runtime | https://pypi.org/project/asyncpg/ | github.com/MagicStack/asyncpg | not pinned in pyproject.toml; STACK.md targets `^0.31.0` (0.31.0, 24 November 2025 — nothing newer existed as of the research date) | 10+ years, 39 releases since 0.5.0 | Yes |
| 4 | `alembic` | runtime | https://pypi.org/project/alembic/ | github.com/sqlalchemy/alembic | not pinned; as of the research date the current release is 1.19.1 (8 August 2026) | 10+ years of release history | Yes |
| 5 | `sqlalchemy` | runtime | https://pypi.org/project/sqlalchemy/ | github.com/sqlalchemy/sqlalchemy | not pinned; as of the research date the current release is 2.0.52 (11 August 2026) | Added under a documented decision — `migrations/env.py` imports it directly, so it is declared as an explicit dependency rather than pulled in transitively through `alembic`. The automated checker never audited it — it is checked only by hand, here | Yes |
| 6 | `boto3` | runtime | https://pypi.org/project/boto3/ | github.com/boto/boto3 | not pinned; as of the research date the current release is 1.43.75 (19 August 2026) | 10+ years, thousands of releases (micro-versions weekly) | Yes |
| 7 | `pydantic-settings` | runtime | https://pypi.org/project/pydantic-settings/ | github.com/pydantic/pydantic-settings | not pinned, installed as `latest` (2.x line) | Active project, releases since 2023 | Yes |
| 8 | `python-slugify` | runtime | https://pypi.org/project/python-slugify/ | github.com/un33k/python-slugify | not pinned; the **base package is installed without an extra** — see the warning below | 12+ years, 46 releases since 0.0.1 | Yes |
| 9 | `typer` | runtime | https://pypi.org/project/typer/ | github.com/fastapi/typer | not pinned, installed as `latest` | Added under a documented decision — the phase's CLI (`add`, `cache-warm`, `cache-report`). The automated checker never saw it | Yes |
| 10 | `httpx` | **runtime** (not dev) | https://pypi.org/project/httpx/ | github.com/encode/httpx | not pinned, installed as `latest` | Added under a documented decision. Used by the `cache-warm` command, which runs in production and not only in tests, so the package is declared a runtime dependency rather than dev. The automated checker never saw it | Yes |
| 11 | `pytest` | dev | https://pypi.org/project/pytest/ | github.com/pytest-dev/pytest | not pinned, installed as `latest` | The standard test runner, added to the dev group under a documented decision | Yes |
| 12 | `pytest-asyncio` | dev | https://pypi.org/project/pytest-asyncio/ | github.com/pytest-dev/pytest-asyncio | STACK.md targets `^1.4` (1.4.0, 26 May 2026) | Added under a documented decision | Yes |
| 13 | `testcontainers` | dev (`[postgres]` extra) | https://pypi.org/project/testcontainers/ | github.com/testcontainers/testcontainers-python | STACK.md targets `^4.15` (4.15.0, 24 July 2026) | Added under a documented decision. Used for `PostgresContainer` in integration tests | Yes |

## What to check during verification (for the owner)

Open each of the thirteen pypi.org links and confirm:
- there is a long release history (years, not days);
- the "Homepage"/"Source" link points to the repository named in the
  "Expected repository" column;
- pypi.org search does not show a similarly-named typosquat package that
  could be confused with it during installation.

## Separate warning: transliteration

The transliteration dependency for `python-slugify` is `text-unidecode`
(Artistic License), installed by default alongside the base package.
`src/pyproject.toml` declares **the base `python-slugify` package
specifically, without the `[unidecode]` extra** — that extra pulls in a
separate `Unidecode` package (GPL), which conflicts with the repository's
MIT license. The owner should check on the `python-slugify` page that the
`unidecode` extra has not been pulled in by accident anywhere in the
dependency tree.

## Verification date and signature

| Field | Value |
|---|---|
| Verification date | 2026-08-21 |
| Signature | Owner confirmed: "packages verified" — all thirteen pypi.org pages opened and checked by hand before dependencies were first installed; no package raised any doubt |

**Verification complete.** The owner personally opened and checked all
thirteen pypi.org pages (the owner's own report, not an automated or agent
check) — the gate is closed. Dependency installation may begin.

## Four more packages: filename recognition and language resolution

Four more packages enter the dependency set, for turning a release filename
into a title/season/episode triple and for resolving a language code, name
or folder token to a canonical tag. The automated checker flags all four the
same way it flagged the thirteen above — the unusable download-count signal
already explained there, not repeated here — so each is checked by hand
against its real registry page instead, the same way as above. Installation
happens only after this table exists, matching the same gate.

| # | Package | Group | pypi.org URL | Expected repository (from the PyPI page) | Version / pin | Age, legitimacy signals | Confirmed |
|---|-------|--------|------------------|-------------------------------------------|--------------|-------------------------------|---------------|
| 14 | `anitopy` | interface (`[api]` extra) | https://pypi.org/project/anitopy/ | github.com/igorcmoura/anitopy | pins the range `>=2.1,<3`; current release queried live at implementation time is 2.1.1 | Long-standing filename-parsing library, published 2022-07-24 and unchanged since, no post-install hook | Yes |
| 15 | `guessit` | interface (`[api]` extra) | https://pypi.org/project/guessit/ | guessit-io.github.io/guessit (GitHub org `guessit-io`) | pins the range `>=4.4,<5`; current release queried live at implementation time is 4.4.0 | Actively released (4.4.0, 2026-07-31), no post-install hook | Yes |
| 16 | `langcodes` | interface (`[api]` extra) | https://pypi.org/project/langcodes/ | github.com/rspeer/langcodes | pins the range `>=3.5,<4`; current release queried live at implementation time is 3.5.1 | MIT license, long-standing CLDR wrapper, no post-install hook | Yes |
| 17 | `language_data` | interface (`[api]` extra) | https://pypi.org/project/language_data/ | github.com/georgkrause/language_data | pins the range `>=1.4,<2`; current release queried live at implementation time is 1.4.0 | Companion data package to `langcodes`, same maintainer lineage, no post-install hook | Yes |

**What to check for these four:** the same three checks as the table above,
against these four pypi.org links instead.

## Verification date and signature (packages 14-17)

| Field | Value |
|---|---|
| Verification date | 2026-09-09 |
| Signature | Owner confirmed: "packages approved" — all four pypi.org pages opened and checked by hand before the image carrying them was published; no package raised any doubt |

**Verification complete.** The owner personally opened and checked all four
pypi.org pages for `anitopy`, `guessit`, `langcodes` and `language_data` —
the gate is closed. The image carrying them was built, pushed and rolled
after this confirmation, not before.
