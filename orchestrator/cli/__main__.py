"""CLI entrypoint: `docker compose exec api python -m orchestrator.cli <command>`.

Runs only inside the already-running `api` container (D-36) -- nothing is
installed on the host, credentials and environment are the same ones the
orchestrator itself uses. `add` is the only way the first title/slug gets
created (D-35): the point of routing it through here instead of `mc cp` +
a hand-written `INSERT` is that the very first addresses that leave for
VRChat worlds go through normalize_slug (ADDR-06) and get a slug_history
row (ADDR-05) -- skip this and the first links are the ones a hand-rolled
INSERT would get wrong.
"""
import asyncio
import uuid
from pathlib import Path

import asyncpg
import typer

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.slugs import SlugError, normalize_slug, pick_unique_slug
from orchestrator.storage import put_object_from_file

app = typer.Typer(add_completion=False, no_args_is_help=True)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(get_settings().database_url)


def _candidate_slug(explicit: str | None, name: str) -> str:
    try:
        return normalize_slug(explicit) if explicit else normalize_slug(name)
    except SlugError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@app.command()
def add(
    file: Path = typer.Option(
        ..., "--file", exists=True, dir_okay=False, readable=True,
        help="Путь к файлу внутри примонтированного /ingest",
    ),
    name: str = typer.Option(..., "--name", help="Человекочитаемое название тайтла"),
    slug: str = typer.Option(None, "--slug", help="Явный слаг вместо производного от названия"),
    reuse_slug: bool = typer.Option(
        False, "--reuse-slug",
        help="Подтвердить переиспользование слага удалённого тайтла (D-10)",
    ),
) -> None:
    asyncio.run(_add(file, name, slug, reuse_slug))


async def _add(file: Path, name: str, explicit_slug: str | None, reuse: bool) -> None:
    conn = await _connect()
    try:
        candidate = _candidate_slug(explicit_slug, name)

        # The tombstone check MUST run on the raw candidate before any
        # auto-suffixing: taken_slugs() (used below by pick_unique_slug)
        # includes tombstoned slugs on purpose, so if this check ran after
        # suffixing, a tombstoned candidate would just get quietly bumped
        # to "-2" and --reuse-slug's confirmation gate would never fire
        # (found by test_add_on_tombstoned_slug_without_flag_refuses,
        # Rule 1).
        existing = await queries.resolve_slug(conn, candidate)
        is_tombstone = existing is not None and existing["status"] != "active"

        if is_tombstone and not reuse:
            typer.echo(
                f"Слаг {candidate!r} принадлежит удалённому тайтлу -- адрес "
                "мог разойтись по мирам. Повторите с --reuse-slug для "
                "явного подтверждения.",
                err=True,
            )
            raise typer.Exit(1)

        if is_tombstone:
            # --reuse-slug: use the exact requested slug, no suffixing --
            # the whole point is to get this specific address back (D-10).
            chosen, similar = candidate, []
            typer.echo(
                f"Внимание: слаг {chosen!r} переиспользован -- прежний адрес "
                "мог разойтись по мирам с другим видео (D-10)."
            )
        else:
            taken = await queries.taken_slugs(conn)
            chosen, similar = pick_unique_slug(candidate, taken)
            if chosen != candidate:
                typer.echo(f"Слаг {candidate!r} занят, использован {chosen!r}")

        if similar:
            typer.echo(f"Предупреждение: похожий слаг уже занят: {', '.join(similar)}")

        edition_id = uuid.uuid4()
        object_key = f"{edition_id}.mp4"
        put_object_from_file(str(file), object_key)

        await queries.create_title_with_edition(conn, name, chosen, edition_id, object_key)

        typer.echo(f"/v/{chosen}")
    finally:
        await conn.close()


@app.command()
def rename(
    slug: str = typer.Argument(..., help="Текущий слаг тайтла"),
    to: str = typer.Option(..., "--to", help="Новое название или слаг"),
) -> None:
    asyncio.run(_rename(slug, to))


async def _rename(old_slug: str, new_name: str) -> None:
    conn = await _connect()
    try:
        candidate = _candidate_slug(None, new_name)
        taken = await queries.taken_slugs(conn)
        chosen, similar = pick_unique_slug(candidate, taken)

        if similar:
            typer.echo(f"Предупреждение: похожий слаг уже занят: {', '.join(similar)}")

        try:
            await queries.rename_slug(conn, old_slug, chosen)
        except LookupError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

        typer.echo(f"/v/{old_slug} по-прежнему работает (ADDR-05)")
        typer.echo(f"/v/{chosen} -- новый адрес")
    finally:
        await conn.close()


@app.command()
def remove(slug: str = typer.Argument(..., help="Слаг тайтла для удаления")) -> None:
    asyncio.run(_remove(slug))


async def _remove(slug: str) -> None:
    settings = get_settings()
    conn = await _connect()
    try:
        row = await queries.resolve_slug(conn, slug)
        if row is None or row["status"] != "active":
            typer.echo(f"Слаг {slug!r} не найден среди активных тайтлов", err=True)
            raise typer.Exit(1)

        await queries.mark_for_delete(conn, row["title_id"], settings.delete_grace_seconds)

        # M9: no cancel promise -- there is no fourth CLI command to undo
        # this in phase 1. The grace window is honestly described as time
        # until the space is freed, not a chance to change your mind; the
        # only way to stop it is to bring the stack down before the reaper
        # runs.
        typer.echo(
            f"/v/{slug} уже отдаёт 410. Через {settings.delete_grace_seconds} "
            "секунд объект будет физически удалён и место освободится -- "
            "это не окно отмены, команды отмены в фазе 1 нет."
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    app()
