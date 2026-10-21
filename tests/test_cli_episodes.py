"""Integration tests for the CLI's episode and edition options on `add`,
plus the shared address grammar `add`/`rename`/`remove` all read
through -- the command line's own repair path for a series once the web
interface will not come up. Same shape as `test_cli_add.py`: storage is
mocked, `_add`/`_rename`/`_remove` are called directly against a real
migrated Postgres, bypassing typer's own argument parsing.
"""

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

    def fake_delete_object(key):
        if key in uploaded:
            uploaded.remove(key)

    monkeypatch.setattr(cli, "put_object_from_file", fake_put_object_from_file)
    monkeypatch.setattr(cli, "delete_object", fake_delete_object)
    return uploaded


@pytest.fixture
def source_file(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"not a real mp4, just bytes for the test")
    return path


def _unique(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


async def _add_standalone_title(source_file, name: str) -> str:
    """Creates a plain title through the CLI's own unchanged path and
    returns its slug, read back off the printed `/v/{slug}` line.
    """
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        await cli._add(source_file, name, None, False)
    lines = [line for line in buf.getvalue().splitlines() if line.startswith("/v/")]
    return lines[0].removeprefix("/v/")


# --- Standalone behaviour is unchanged when no new option is given ---------


async def test_add_standalone_unchanged_when_no_new_option_given(
    source_file, uploaded_objects, capsys
):
    await cli._add(source_file, "CLI Episodes Standalone", None, False)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "/v/cli-episodes-standalone" in lines
    # No numbered-address line: unchanged for a standalone title.
    assert not any("/01" in line for line in lines)


# --- An episode number and no title: the new title's own single episode ----


async def test_add_episode_only_gives_the_new_titles_single_episode_that_number(
    source_file, uploaded_objects, capsys, raw_conn
):
    name = _unique("CLI New Title With Number")
    await cli._add(source_file, name, None, False, None, 7, None, False)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert any(line.startswith("/v/") and "/07" not in line for line in lines)
    assert any(line.endswith("/07 -- this episode's own address") for line in lines)

    slug = next(
        line for line in lines if line.startswith("/v/") and " " not in line
    ).removeprefix("/v/")
    row = await queries.resolve_episode_slug(raw_conn, slug, 7)
    assert row is not None


async def test_add_episode_only_number_one_prints_no_extra_line(
    source_file, uploaded_objects, capsys
):
    name = _unique("CLI New Title Number One")
    await cli._add(source_file, name, None, False, None, 1, None, False)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert not any("this episode's own address" in line for line in lines)


# --- A title and an episode number: attach as a new episode ----------------


async def test_add_attach_creates_a_new_episode_of_an_existing_title(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(source_file, _unique("CLI Attach Base"))

    await cli._add(source_file, None, None, False, title_slug, 5, None, False)
    out = capsys.readouterr().out
    assert f"/v/{title_slug}/05" in out.splitlines()

    row = await queries.resolve_episode_slug(raw_conn, title_slug, 5)
    assert row is not None
    assert row["episode_status"] is None or row["episode_status"] == "active"


async def test_add_attach_refuses_when_title_does_not_exist(
    source_file, uploaded_objects
):
    with pytest.raises(typer.Exit):
        await cli._add(
            source_file, None, None, False, _unique("no-such-title"), 1, None, False
        )
    assert len(uploaded_objects) == 0


async def test_add_attach_refuses_when_the_number_is_already_held(
    source_file, uploaded_objects, capsys
):
    title_slug = await _add_standalone_title(source_file, _unique("CLI Attach Taken"))
    before = len(uploaded_objects)
    # The standalone title already claims number 1.
    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, title_slug, 1, None, False)

    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--edition" in err
    assert len(uploaded_objects) == before


# --- A title, an episode number and an edition slug: add an edition -------


async def test_add_edition_adds_an_alternate_edition_of_an_existing_episode(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(source_file, _unique("CLI Edition Base"))

    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", False)
    out = capsys.readouterr().out
    assert f"/v/{title_slug}/01/dub-ru" in "\n".join(out.splitlines())

    row = await queries.resolve_episode_slug(
        raw_conn, title_slug, 1, edition_slug="dub-ru"
    )
    assert row is not None


async def test_add_edition_refuses_when_the_episode_does_not_exist(
    source_file, uploaded_objects, capsys
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Edition No Episode")
    )
    before = len(uploaded_objects)

    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, title_slug, 99, "dub-ru", False)

    err = capsys.readouterr().err
    assert "No episode numbered 99" in err
    assert len(uploaded_objects) == before


async def test_add_edition_refuses_a_taken_slug(source_file, uploaded_objects, capsys):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Edition Dup Slug")
    )
    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", False)
    capsys.readouterr()
    before = len(uploaded_objects)

    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", False)
    assert len(uploaded_objects) == before


async def test_add_edition_refuses_an_all_digit_slug(
    source_file, uploaded_objects, capsys
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Edition Digit Slug")
    )
    before = len(uploaded_objects)

    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, title_slug, 1, "2024", False)
    assert len(uploaded_objects) == before


async def test_add_edition_with_make_default_moves_the_pointer(
    source_file, uploaded_objects, raw_conn, capsys
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Edition Default")
    )
    row = await queries.resolve_episode_slug(raw_conn, title_slug, 1)
    episode_id = row["episode_id"]
    original_default = row["edition_id"]

    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", True)
    capsys.readouterr()

    new_default = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    assert new_default != original_default


async def test_add_edition_without_make_default_leaves_the_old_default(
    source_file, uploaded_objects, raw_conn, capsys
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Edition No Default")
    )
    row = await queries.resolve_episode_slug(raw_conn, title_slug, 1)
    episode_id = row["episode_id"]
    original_default = row["edition_id"]

    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", False)
    capsys.readouterr()

    unchanged_default = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    assert unchanged_default == original_default


# --- Every option combination that cannot mean anything is refused --------


async def test_add_refuses_edition_with_no_episode(
    source_file, uploaded_objects, capsys
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Refuse Edition No Ep")
    )
    before = len(uploaded_objects)

    with pytest.raises(typer.Exit):
        await cli._add(
            source_file, None, None, False, title_slug, None, "dub-ru", False
        )

    err = capsys.readouterr().err
    assert "--episode" in err
    assert len(uploaded_objects) == before


async def test_add_refuses_edition_with_no_title(source_file, uploaded_objects, capsys):
    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, None, 3, "dub-ru", False)

    err = capsys.readouterr().err
    assert "--title" in err
    assert len(uploaded_objects) == 0


async def test_add_refuses_make_default_with_no_edition(
    source_file, uploaded_objects, capsys
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Refuse Make Default")
    )
    before = len(uploaded_objects)

    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, title_slug, 3, None, True)

    err = capsys.readouterr().err
    assert "--make-default" in err
    assert len(uploaded_objects) == before


async def test_add_refuses_title_with_no_episode(source_file, uploaded_objects, capsys):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Refuse Title No Ep")
    )
    before = len(uploaded_objects)

    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, title_slug, None, None, False)

    err = capsys.readouterr().err
    assert "--episode" in err
    assert len(uploaded_objects) == before


async def test_add_refuses_a_new_title_with_no_name(
    source_file, uploaded_objects, capsys
):
    with pytest.raises(typer.Exit):
        await cli._add(source_file, None, None, False, None, None, None, False)

    err = capsys.readouterr().err
    assert "--name" in err
    assert len(uploaded_objects) == 0


# --- rename reads its target through the shared grammar --------------------


async def test_rename_still_renames_a_bare_title_address(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(source_file, _unique("CLI Rename Bare"))
    capsys.readouterr()

    await cli._rename(title_slug, "CLI Rename Bare Target")
    out = capsys.readouterr().out
    assert "still works" in out

    old = await queries.resolve_slug(raw_conn, title_slug)
    assert old is not None


async def test_rename_refuses_a_numbered_address(source_file, uploaded_objects, capsys):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Rename Numbered")
    )
    capsys.readouterr()

    with pytest.raises(typer.Exit):
        await cli._rename(f"{title_slug}/1", "Whatever")

    err = capsys.readouterr().err
    assert "Only a title address can be renamed" in err
    assert "renumber" in err


async def test_rename_refuses_an_edition_address(source_file, uploaded_objects, capsys):
    title_slug = await _add_standalone_title(source_file, _unique("CLI Rename Edition"))
    capsys.readouterr()
    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", False)
    capsys.readouterr()

    with pytest.raises(typer.Exit):
        await cli._rename(f"{title_slug}/1/dub-ru", "Whatever")

    err = capsys.readouterr().err
    assert "Only a title address can be renamed" in err
    assert "fixed once the edition exists" in err


# --- remove reads its target through the shared grammar ---------------------


async def test_remove_bare_title_address_is_unchanged(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(source_file, _unique("CLI Remove Bare"))
    capsys.readouterr()

    await cli._remove(title_slug)
    out = capsys.readouterr().out
    assert "freed" in out

    row = await queries.resolve_slug(raw_conn, title_slug)
    assert row["status"] == "pending_delete"


async def test_remove_numbered_address_removes_that_episode_only(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Remove Numbered")
    )
    capsys.readouterr()
    await cli._add(source_file, None, None, False, title_slug, 2, None, False)
    capsys.readouterr()

    await cli._remove(f"{title_slug}/2")
    out = capsys.readouterr().out
    assert f"/v/{title_slug}/02" in out.splitlines()[0]

    episode_2 = await queries.episode_by_number(
        raw_conn, (await queries.resolve_slug(raw_conn, title_slug))["title_id"], 2
    )
    assert episode_2["episode_status"] == "pending_delete"
    # Episode 1 (the title's own original episode) is unaffected.
    row = await queries.resolve_slug(raw_conn, title_slug)
    assert row["status"] == "active"
    assert row["episode_status"] == "active"


async def test_remove_episode_accepts_a_leading_zero_numbered_address(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Remove Leading Zero")
    )
    capsys.readouterr()
    await cli._add(source_file, None, None, False, title_slug, 9, None, False)
    capsys.readouterr()

    await cli._remove(f"{title_slug}/09")

    episode_9 = await queries.episode_by_number(
        raw_conn, (await queries.resolve_slug(raw_conn, title_slug))["title_id"], 9
    )
    assert episode_9["episode_status"] == "pending_delete"


async def test_remove_episode_uses_the_same_countdown_as_title_level_remove(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Remove Countdown")
    )
    capsys.readouterr()
    await cli._add(source_file, None, None, False, title_slug, 4, None, False)
    capsys.readouterr()

    await cli._remove(f"{title_slug}/4")

    title_id = (await queries.resolve_slug(raw_conn, title_slug))["title_id"]
    episode_4 = await queries.episode_by_number(raw_conn, title_id, 4)
    delete_at = await raw_conn.fetchval(
        "SELECT delete_at FROM episodes WHERE id = $1", episode_4["episode_id"]
    )
    assert delete_at is not None

    # Undo works exactly the same way the title-level countdown does.
    await queries.cancel_delete_episode(raw_conn, episode_4["episode_id"])
    restored = await queries.episode_by_number(raw_conn, title_id, 4)
    assert restored["episode_status"] == "active"


async def test_remove_edition_refuses_when_it_is_the_default_and_a_sibling_exists(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Remove Real Default")
    )
    capsys.readouterr()
    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", True)
    capsys.readouterr()

    row = await queries.resolve_slug(raw_conn, title_slug)
    episode_id = row["episode_id"]
    default_edition_id = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    default_slug = await raw_conn.fetchval(
        "SELECT slug FROM editions WHERE id = $1", default_edition_id
    )
    assert default_slug == "dub-ru"

    with pytest.raises(typer.Exit):
        await cli._remove(f"{title_slug}/1/dub-ru")

    err = capsys.readouterr().err
    assert "default edition" in err
    assert "make another" in err.lower()


async def test_remove_edition_deletes_a_non_default_edition_with_a_sibling(
    source_file, uploaded_objects, capsys, raw_conn
):
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Remove Sibling Edition")
    )
    capsys.readouterr()
    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", False)
    capsys.readouterr()

    await cli._remove(f"{title_slug}/1/dub-ru")
    out = capsys.readouterr().out
    assert "deleted" in out

    row = await queries.resolve_episode_slug(
        raw_conn, title_slug, 1, edition_slug="dub-ru"
    )
    assert row is None or row["edition_id"] is None
    # The episode itself is unaffected.
    still_there = await queries.resolve_slug(raw_conn, title_slug)
    assert still_there["status"] == "active"


async def test_remove_edition_last_edition_removes_the_episode_too(
    source_file, uploaded_objects, capsys, raw_conn
):
    """The episode's own first edition is always slug-less, so no
    public address ever names it directly -- the only way this codebase
    reaches "one edition left, and it has a real slug" is the original
    slug-less edition already being gone by some other means. Simulated
    here with a direct row deletion (test-only) so `_remove_edition`'s own
    last-edition branch is exercised end to end through the real
    CLI entry point.
    """
    title_slug = await _add_standalone_title(
        source_file, _unique("CLI Remove Last Edition")
    )
    capsys.readouterr()
    await cli._add(source_file, None, None, False, title_slug, 1, "dub-ru", False)
    capsys.readouterr()

    row = await queries.resolve_slug(raw_conn, title_slug)
    episode_id = row["episode_id"]
    original_default_edition_id = row["default_edition_id"]

    await raw_conn.execute(
        """
        UPDATE episodes SET default_edition_id =
            (SELECT id FROM editions WHERE episode_id = $1 AND slug = 'dub-ru')
        WHERE id = $1
        """,
        episode_id,
    )
    await raw_conn.execute(
        "DELETE FROM editions WHERE id = $1", original_default_edition_id
    )

    await cli._remove(f"{title_slug}/1/dub-ru")
    out = capsys.readouterr().out
    assert "only edition" in out

    updated_status = await raw_conn.fetchval(
        "SELECT status FROM episodes WHERE id = $1", episode_id
    )
    assert updated_status == "pending_delete"
