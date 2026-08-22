# Легитимность тринадцати PyPI-пакетов фазы 1 (T-01-SC)

**English abstract:** Verified that all thirteen PyPI packages this phase
installs are the real, legitimate packages, not typosquats or hallucinated
names. An automated checker flagged all seven originally-audited packages as
suspicious for a reason unrelated to legitimacy (recency of the latest
release, not package age), so every package was instead confirmed by hand
against its real pypi.org page. Verdict: all thirteen cleared.

Гейт перед первой установкой зависимостей (план 01-03). Автоматический чекер
(`gsd-tools query package-legitimacy check --ecosystem pypi`) уже прогнан по
исходной семёрке в исследовании (`01-RESEARCH.md`, раздел «Package Legitimacy
Audit») и дал `SUS` по причинам `too-new`/`unknown-downloads` на всех семи —
это ложное срабатывание (сигнал `too-new` смотрит на дату **последнего**
релиза, а не на возраст пакета). Ни один пакет не получил `SLOP`. Шесть
пакетов сверх исходной семёрки (`typer`, `httpx`, `sqlalchemy`,
`pytest`, `pytest-asyncio`, `testcontainers`) автоматический чекер вообще не
видел — они добавлены по вердикту m5, потому что реально ставятся планом 03.
Повторный прогон автоматического чекера бессмыслен — он уже дал ложное
срабатывание на известных, безопасных пакетах; для всех тринадцати нужна
только ручная проверка человеком по ссылке на pypi.org.

**Таблица ниже — автоматизируемая часть подготовки.** Открыть тринадцать
страниц и сверить репозиторий с колонкой «ожидаемый репозиторий» — шаг,
который выполняет владелец; исполнитель эту колонку не заполняет по
предположению. Заполненные значения в колонке «Подтверждено» проставлены
по отчёту владельца от 2026-08-21 — см. раздел «Дата сверки и подпись».

## Таблица сверки

| # | Пакет | Группа | URL на pypi.org | Ожидаемый репозиторий (со страницы PyPI) | Версия / пин | Возраст, сигналы легитимности | Подтверждено |
|---|-------|--------|------------------|-------------------------------------------|--------------|-------------------------------|---------------|
| 1 | `fastapi` | runtime | https://pypi.org/project/fastapi/ | github.com/fastapi/fastapi | pyproject.toml пинует диапазон `>=0.141,<0.150`; на дату исследования (2026-08-20) актуальна 0.141.1 (29 июля 2026) | 6+ лет истории, 250+ релизов с 0.1.0 | Да |
| 2 | `uvicorn` | runtime (`[standard]` экстра) | https://pypi.org/project/uvicorn/ | github.com/Kludex/uvicorn (проект переехал из `encode/uvicorn` — ожидаемо, не признак подмены) | не пинуется в pyproject.toml, ставится `latest` | 8+ лет истории релизов | Да |
| 3 | `asyncpg` | runtime | https://pypi.org/project/asyncpg/ | github.com/MagicStack/asyncpg | не пинуется в pyproject.toml; STACK.md ориентируется на `^0.31.0` (0.31.0, 24 ноября 2025 — новее не было на дату исследования) | 10+ лет, 39 релизов с 0.5.0 | Да |
| 4 | `alembic` | runtime | https://pypi.org/project/alembic/ | github.com/sqlalchemy/alembic | не пинуется; на дату исследования актуальна 1.19.1 (8 августа 2026) | 10+ лет истории релизов | Да |
| 5 | `sqlalchemy` | runtime | https://pypi.org/project/sqlalchemy/ | github.com/sqlalchemy/sqlalchemy | не пинуется; на дату исследования актуальна 2.0.52 (11 августа 2026) | Добавлена по вердикту m5 — `migrations/env.py` (план 04) импортирует её напрямую, поэтому объявляется явной зависимостью, а не тянется транзитивно через `alembic`. Автоматический чекер её не проверял — сверяется только вручную здесь | Да |
| 6 | `boto3` | runtime | https://pypi.org/project/boto3/ | github.com/boto/boto3 | не пинуется; на дату исследования актуальна 1.43.75 (19 августа 2026) | 10+ лет, тысячи релизов (микро-версии еженедельно) | Да |
| 7 | `pydantic-settings` | runtime | https://pypi.org/project/pydantic-settings/ | github.com/pydantic/pydantic-settings | не пинуется, ставится `latest` (линейка 2.x) | Активный проект, релизы с 2023 | Да |
| 8 | `python-slugify` | runtime | https://pypi.org/project/python-slugify/ | github.com/un33k/python-slugify | не пинуется; ставится **базовый пакет без экстры** — см. предупреждение ниже | 12+ лет, 46 релизов с 0.0.1 | Да |
| 9 | `typer` | runtime | https://pypi.org/project/typer/ | github.com/fastapi/typer | не пинуется, ставится `latest` | Добавлен по вердикту m5 — CLI фазы (`add`, `cache-warm`, `cache-report`), план 03. Автоматический чекер его не видел | Да |
| 10 | `httpx` | **runtime** (не dev) | https://pypi.org/project/httpx/ | github.com/encode/httpx | не пинуется, ставится `latest` | Добавлен по вердикту m5. Используется командой `cache-warm` (план 05) — она работает в бою, а не только в тестах, поэтому пакет объявлен runtime-зависимостью, а не dev. Автоматический чекер его не видел | Да |
| 11 | `pytest` | dev | https://pypi.org/project/pytest/ | github.com/pytest-dev/pytest | не пинуется, ставится `latest` | Стандартный тест-раннер, добавлен по вердикту m5 в dev-группу | Да |
| 12 | `pytest-asyncio` | dev | https://pypi.org/project/pytest-asyncio/ | github.com/pytest-dev/pytest-asyncio | STACK.md ориентируется на `^1.4` (1.4.0, 26 мая 2026) | Добавлен по вердикту m5 | Да |
| 13 | `testcontainers` | dev (`[postgres]` экстра) | https://pypi.org/project/testcontainers/ | github.com/testcontainers/testcontainers-python | STACK.md ориентируется на `^4.15` (4.15.0, 24 июля 2026) | Добавлен по вердикту m5. Используется для `PostgresContainer` в интеграционных тестах | Да |

## На что смотреть при сверке (для владельца)

Открыть каждую из тринадцати ссылок на pypi.org и убедиться:
- есть длинная история релизов (годы, не дни);
- ссылка «Homepage»/«Source» ведёт на репозиторий из колонки «Ожидаемый репозиторий»;
- в поиске pypi.org рядом нет опечаточного одноимённого пакета с похожим названием
  (typosquat), который можно перепутать при установке.

## Отдельное предупреждение: транслитерация

Зависимость транслитерации для `python-slugify` — это `text-unidecode`
(Artistic License), устанавливаемая по умолчанию вместе с базовым пакетом.
`src/pyproject.toml` (план 03) объявляет **именно базовый `python-slugify`,
без экстры `[unidecode]`** — та экстра тянет отдельный пакет `Unidecode`
(GPL), который конфликтует с MIT-лицензией репозитория. Владельцу стоит
сверить на странице `python-slugify`, что экстра `unidecode` не подмешана
случайно нигде в дереве зависимостей.

## Дата сверки и подпись

| Поле | Значение |
|---|---|
| Дата сверки | 2026-08-21 |
| Подпись | Владелец подтвердил: «пакеты сверены» — все тринадцать страниц на pypi.org открыты и проверены вручную перед первой установкой зависимостей, ни один пакет сомнений не вызвал |

**Сверка завершена.** Владелец лично открыл и сверил все тринадцать страниц на
pypi.org (отчёт владельца, не автоматическая или агентская проверка) — гейт
T-01-SC закрыт. Установка зависимостей (план 03) может начинаться.
