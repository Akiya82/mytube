"""Integration tests for `orchestrator.cli`. Storage is mocked (no real S3
put), a real Postgres runs behind the CLI's own connection -- `_add`,
`_rename`, `_remove` are called directly (bypassing typer's argument
parsing, which has nothing to do with the behavior under test here).
"""

import asyncio
import uuid

import pytest
import typer
from conftest import ADMIN_FQDN, WORKER_TOKEN

from orchestrator.cli import __main__ as cli
from orchestrator.config import get_settings
from orchestrator.db import queries

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _cli_env(migrated_db_url, monkeypatch):
    monkeypatch.setenv("MYTUBE_DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def uploaded_objects(monkeypatch):
    uploaded = []

    def fake_put_object_from_file(path, key):
        uploaded.append(key)

    monkeypatch.setattr(cli, "put_object_from_file", fake_put_object_from_file)
    return uploaded


@pytest.fixture
def source_file(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"not a real mp4, just bytes for the test")
    return path


async def test_add_creates_title_and_prints_v_address(
    source_file, uploaded_objects, capsys
):
    await cli._add(source_file, "CLI Атака Титанов", None, False)

    out = capsys.readouterr().out
    assert "/v/cli-ataka-titanov" in out
    assert len(uploaded_objects) == 1


async def test_add_twice_same_file_gives_different_object_keys(
    source_file, uploaded_objects
):
    await cli._add(source_file, "CLI Dup Source A", None, False)
    await cli._add(source_file, "CLI Dup Source B", None, False)

    assert len(uploaded_objects) == 2
    assert uploaded_objects[0] != uploaded_objects[1]
    # Object keys are always {edition_id}.mp4, never reused.
    assert all(key.endswith(".mp4") for key in uploaded_objects)


async def test_add_collision_gets_deterministic_numeric_suffix(
    source_file, uploaded_objects, capsys
):
    await cli._add(source_file, "CLI Дубликат Слага", None, False)
    capsys.readouterr()

    await cli._add(source_file, "CLI Дубликат Слага", None, False)
    out = capsys.readouterr().out

    assert "cli-dublikat-slaga-2" in out


async def test_add_similar_slug_warns_before_saving(
    source_file, uploaded_objects, capsys
):
    await cli._add(source_file, "CliRezeroWarnCase", None, False)
    capsys.readouterr()

    await cli._add(source_file, "Cli:Rezero:Warn:Case", None, False)
    out = capsys.readouterr().out

    assert "Warning" in out
    assert "clirezerowarncase" in out


async def test_add_explicit_slug_overrides_name_derived_slug(
    source_file, uploaded_objects, capsys
):
    await cli._add(
        source_file, "Some Long Title Nobody Reads", "cli-custom-slug", False
    )

    out = capsys.readouterr().out
    assert "/v/cli-custom-slug" in out


async def test_add_on_tombstoned_slug_without_flag_refuses(
    source_file, uploaded_objects, capsys, raw_conn
):
    edition_id = uuid.uuid4()
    old_title_id = await queries.create_title_with_edition(
        raw_conn,
        "CLI Old Tombstone",
        "cli-tombstone-slug",
        edition_id,
        f"{edition_id}.mp4",
    )
    await queries.mark_for_delete(raw_conn, old_title_id, grace_seconds=0)
    await queries.finish_delete(raw_conn, old_title_id)

    with pytest.raises(typer.Exit):
        await cli._add(source_file, "CLI New Title", "cli-tombstone-slug", False)

    err = capsys.readouterr().err
    assert "spread across worlds" in err
    assert len(uploaded_objects) == 0


async def test_add_on_tombstoned_slug_with_flag_reuses_and_keeps_history(
    source_file, uploaded_objects, capsys, raw_conn
):
    edition_id = uuid.uuid4()
    old_title_id = await queries.create_title_with_edition(
        raw_conn,
        "CLI Old Tombstone Reuse",
        "cli-tombstone-reuse-slug",
        edition_id,
        f"{edition_id}.mp4",
    )
    await queries.mark_for_delete(raw_conn, old_title_id, grace_seconds=0)
    await queries.finish_delete(raw_conn, old_title_id)

    await cli._add(source_file, "CLI New Title Reuse", "cli-tombstone-reuse-slug", True)

    out = capsys.readouterr().out
    assert "Note: slug" in out
    assert "was reused" in out
    # Anchored to end of line, not a substring match: /v/x is a substring
    # of /v/x-2, so a plain "in out" check cannot tell a clean reuse from
    # the auto-suffixed collision the tombstone branch exists to prevent
    # (see plan_slug's own docstring on why this exact regression is easy
    # for an extraction to reintroduce silently).
    lines = out.splitlines()
    assert "/v/cli-tombstone-reuse-slug" in lines
    assert "Warning" not in out

    resolved = await queries.resolve_slug(raw_conn, "cli-tombstone-reuse-slug")
    assert resolved["title_id"] != old_title_id

    history_rows = await raw_conn.fetch(
        "SELECT title_id FROM slug_history WHERE slug = $1", "cli-tombstone-reuse-slug"
    )
    assert len(history_rows) == 2


async def test_rename_keeps_old_address_working(
    source_file, uploaded_objects, capsys, raw_conn
):
    await cli._add(source_file, "CLI Rename Source", "cli-rename-a", False)
    capsys.readouterr()

    await cli._rename("cli-rename-a", "CLI Rename Target")
    out = capsys.readouterr().out

    assert "cli-rename-a" in out
    assert "still works" in out

    old = await queries.resolve_slug(raw_conn, "cli-rename-a")
    new = await queries.resolve_slug(raw_conn, "cli-rename-target")
    assert old is not None and new is not None
    assert old["title_id"] == new["title_id"]


async def test_remove_output_names_undo_and_purge_now_in_the_interface(
    source_file, uploaded_objects, capsys
):
    await cli._add(source_file, "CLI Remove Wording", "cli-remove-wording", False)
    capsys.readouterr()

    await cli._remove("cli-remove-wording")
    out = capsys.readouterr().out

    assert "freed" in out
    # Inverted from an earlier wording: the grace window is now an undo
    # window, and the CLI has to say so rather than deny it exists.
    assert "not an undo window" not in out
    assert "Undo the deletion" in out
    assert "web interface" in out


async def test_two_cli_adds_racing_on_one_name_get_two_different_addresses(
    source_file, uploaded_objects
):
    """The CLI runs against the same live service the interface does
    (`docker compose exec api`), so a CLI add and a web add -- or two CLI
    adds -- really can read the free-slug set at the same moment. Without
    the read and the insert serialized, both claim the same address and
    the first title silently stops resolving at the address it printed.
    """
    await asyncio.gather(
        cli._add(source_file, "CLI Slug Race", None, False),
        cli._add(source_file, "CLI Slug Race", None, False),
    )

    conn = await cli._connect()
    try:
        rows = await conn.fetch(
            "SELECT slug FROM slug_history WHERE slug LIKE 'cli-slug-race%'"
        )
    finally:
        await conn.close()

    slugs = sorted(row["slug"] for row in rows)
    assert slugs == ["cli-slug-race", "cli-slug-race-2"]
