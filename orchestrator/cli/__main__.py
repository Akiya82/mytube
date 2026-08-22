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

from orchestrator.cli.cache_report import cache_report as _cache_report
from orchestrator.cli.cache_warm import DEFAULT_BASE_URL, cache_warm as _cache_warm
from orchestrator.cli.storage_audit import run_storage_audit as _storage_audit
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import plan_slug
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
        # plan_slug (orchestrator/media/add.py) is the extracted version of
        # what this function used to compute inline -- the web add form
        # calls the exact same sequence, in the exact same order, so the
        # two never drift. The tombstone-then-reuse decision below and its
        # printed messages are unchanged from before the extraction.
        try:
            slug_plan = await plan_slug(conn, name, explicit_slug)
        except SlugError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

        if slug_plan.is_tombstone and not reuse:
            typer.echo(
                f"Слаг {slug_plan.candidate!r} принадлежит удалённому тайтлу -- адрес "
                "мог разойтись по мирам. Повторите с --reuse-slug для "
                "явного подтверждения.",
                err=True,
            )
            raise typer.Exit(1)

        if slug_plan.is_tombstone:
            # --reuse-slug: use the exact requested slug, no suffixing --
            # the whole point is to get this specific address back (D-10).
            typer.echo(
                f"Внимание: слаг {slug_plan.chosen!r} переиспользован -- прежний адрес "
                "мог разойтись по мирам с другим видео (D-10)."
            )
        elif slug_plan.chosen != slug_plan.candidate:
            typer.echo(f"Слаг {slug_plan.candidate!r} занят, использован {slug_plan.chosen!r}")

        if slug_plan.similar:
            typer.echo(
                f"Предупреждение: похожий слаг уже занят: {', '.join(slug_plan.similar)}"
            )

        edition_id = uuid.uuid4()
        object_key = f"{edition_id}.mp4"
        put_object_from_file(str(file), object_key)

        await queries.create_title_with_edition(
            conn, name, slug_plan.chosen, edition_id, object_key
        )

        typer.echo(f"/v/{slug_plan.chosen}")
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

        typer.echo(f"/v/{old_slug} по-прежнему работает")
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

        # The grace window is now reachable from both ends through the web
        # interface's library screen: undo restores the title, and delete
        # now skips the wait. The CLI itself still offers neither -- both
        # live in the interface, not as a fifth CLI command.
        typer.echo(
            f"/v/{slug} уже отдаёт 410. Через {settings.delete_grace_seconds} "
            "секунд объект будет физически удалён и место освободится. "
            "Отменить удаление или удалить сразу же можно в веб-интерфейсе, "
            "на странице библиотеки."
        )
    finally:
        await conn.close()


@app.command("cache-warm")
def cache_warm_command(
    slug: str = typer.Argument(..., help="Слаг тайтла, который прогреваем перед сеансом (D-27)"),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL, "--base-url", help="Адрес edge, обычно внутренний (D-36)"
    ),
    chunk_size: int = typer.Option(
        None, "--chunk-size", help="Размер шага в байтах; по умолчанию MYTUBE_SLICE_SIZE"
    ),
    bytes: int = typer.Option(
        None, "--bytes",
        help="Предел прогрева в байтах (вердикт M5); по умолчанию MYTUBE_WARM_BYTES "
        "или половина зоны кэша",
    ),
) -> None:
    code = _cache_warm(slug, base_url=base_url, chunk_size=chunk_size, warm_bytes=bytes)
    raise typer.Exit(code)


@app.command("cache-report")
def cache_report_command(
    log_path: str = typer.Argument(
        None, help="Путь к blob_access.log; по умолчанию MYTUBE_BLOB_ACCESS_LOG из настроек"
    ),
    since: str = typer.Option(
        None, "--since", help="Учитывать только строки не старше этого момента (ISO 8601)"
    ),
) -> None:
    path = log_path or get_settings().blob_access_log
    code = _cache_report(path, since=since)
    raise typer.Exit(code)


@app.command(
    "storage-audit",
    help="Сверить каталог с реальным содержимым хранилища в обе стороны. "
    "Только читает — ничего не удаляет и не меняет.",
)
def storage_audit_command() -> None:
    raise typer.Exit(_storage_audit())


if __name__ == "__main__":
    app()
