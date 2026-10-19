"""Integration tests for the credential CLI commands
(`orchestrator.cli.auth`). Storage is not involved -- these commands only
ever touch Postgres. `_create_operator`/`_reset_password` are called
directly, bypassing typer's argument parsing, which has nothing to do with
the behaviour under test here (mirrors `test_cli_add.py`).

RED state: `orchestrator.cli.auth` does not exist yet. This module fails to
import until a later plan creates it, which is the expected Wave 0 shape --
the module docstring in `test_auth.py` explains the same thing for the
gate.
"""

import pytest
import typer
from conftest import WORKER_TOKEN

from orchestrator.cli import auth as cli_auth  # RED: created by a later plan
from orchestrator.config import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "a password nobody should see printed anywhere"


@pytest.fixture(autouse=True)
def _cli_env(migrated_db_url, monkeypatch):
    monkeypatch.setenv("MYTUBE_DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", "admin.mytube.example")
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_create_operator_writes_a_credential_row_and_prints_the_confirmation(
    capsys, raw_conn
):
    await cli_auth._create_operator("op-one", PASSWORD)

    row = await raw_conn.fetchrow(
        "SELECT username, password_hash FROM operator_credential WHERE username = $1",
        "op-one",
    )
    assert row is not None
    assert row["password_hash"] != PASSWORD
    assert row["password_hash"].startswith("$argon2")

    out = capsys.readouterr().out
    assert "op-one" in out
    # English confirmation -- no Cyrillic character range.
    assert not any("Ѐ" <= ch <= "ӿ" for ch in out)


async def test_create_operator_refuses_when_an_operator_already_exists(
    capsys, raw_conn
):
    await cli_auth._create_operator("op-first", PASSWORD)
    capsys.readouterr()

    with pytest.raises(typer.Exit):
        await cli_auth._create_operator("op-second", "a different password entirely")

    rows = await raw_conn.fetch("SELECT username FROM operator_credential")
    assert [r["username"] for r in rows] == ["op-first"]


async def test_reset_password_replaces_the_hash(raw_conn):
    await cli_auth._create_operator("op-reset", PASSWORD)
    before = await raw_conn.fetchval(
        "SELECT password_hash FROM operator_credential WHERE username = $1", "op-reset"
    )

    new_password = "a completely different passphrase"
    await cli_auth._reset_password("op-reset", new_password)

    after = await raw_conn.fetchval(
        "SELECT password_hash FROM operator_credential WHERE username = $1", "op-reset"
    )
    assert after != before

    from orchestrator.auth import verify_password  # same RED module as above

    assert await verify_password(after, new_password)


async def test_reset_password_kills_every_live_session(raw_conn):
    await cli_auth._create_operator("op-sessions", PASSWORD)
    operator_id = await raw_conn.fetchval(
        "SELECT id FROM operator_credential WHERE username = $1", "op-sessions"
    )

    await raw_conn.execute(
        "INSERT INTO sessions (token_hash, operator_id, expires_at) "
        "VALUES ('cli-test-hash-a', $1, now() + interval '1 day')",
        operator_id,
    )
    await raw_conn.execute(
        "INSERT INTO sessions (token_hash, operator_id, expires_at) "
        "VALUES ('cli-test-hash-b', $1, now() + interval '1 day')",
        operator_id,
    )

    await cli_auth._reset_password("op-sessions", "yet another new password")

    remaining = await raw_conn.fetchval(
        "SELECT count(*) FROM sessions WHERE operator_id = $1", operator_id
    )
    assert remaining == 0


async def test_no_command_prints_the_password_back(capsys, raw_conn):
    await cli_auth._create_operator("op-quiet", PASSWORD)
    create_out = capsys.readouterr().out
    assert PASSWORD not in create_out

    await cli_auth._reset_password("op-quiet", "a second secret value entirely")
    reset_out = capsys.readouterr().out
    assert "a second secret value entirely" not in reset_out
    assert PASSWORD not in reset_out


async def test_reset_password_with_no_username_prints_the_username_it_reset(
    capsys, raw_conn
):
    await cli_auth._create_operator("the-only-operator", PASSWORD)
    capsys.readouterr()

    await cli_auth._reset_password(None, "a brand new password value")

    out = capsys.readouterr().out
    assert "the-only-operator" in out
