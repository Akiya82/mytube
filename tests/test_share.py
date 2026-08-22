"""Path containment (safe_join) and directory listing (list_dir) for the
mounted network share. `tmp_path` stands in for the CIFS/NFS mount --
`orchestrator.media.share` cannot tell the difference, which is the point.

The route tests at the bottom of this file cover GET /, GET /add/{path},
and GET /lang/{code} -- the containment and escaping proven at the unit
level above, wired into HTTP responses.
"""
import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.main import app
from orchestrator.media.share import ShareEntry, ShareEscape, ShareUnavailable, list_dir, safe_join
from orchestrator.web import routes_share


def test_safe_join_empty_rel_returns_resolved_root(share_root):
    assert safe_join(share_root, "") == share_root.resolve()


def test_safe_join_nested_rel_returns_path_under_root(share_root):
    result = safe_join(share_root, "Movies")
    assert result == (share_root / "Movies").resolve()


def test_safe_join_rejects_dot_dot_segment(share_root):
    with pytest.raises(ShareEscape):
        safe_join(share_root, "../etc/passwd")


def test_safe_join_rejects_absolute_segment(share_root):
    with pytest.raises(ShareEscape):
        safe_join(share_root, "/etc/passwd")


def test_safe_join_rejects_dot_dot_buried_in_the_middle(share_root):
    with pytest.raises(ShareEscape):
        safe_join(share_root, "Movies/../../etc")


def test_safe_join_rejects_symlink_escape(share_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")

    link = share_root / "escape-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host has no permission to create symlinks")

    with pytest.raises(ShareEscape):
        safe_join(share_root, "escape-link/secret.txt")


def test_list_dir_returns_directories_before_files_case_insensitive(share_root):
    entries = list_dir(share_root, share_root)

    names = [e.name for e in entries]
    is_dir_flags = [e.is_dir for e in entries]

    # Every True (directory) entry precedes every False (file) entry.
    assert is_dir_flags == sorted(is_dir_flags, reverse=True)
    dirs = [n for n, is_dir in zip(names, is_dir_flags) if is_dir]
    files = [n for n, is_dir in zip(names, is_dir_flags) if not is_dir]
    assert dirs == sorted(dirs, key=str.casefold)
    assert files == sorted(files, key=str.casefold)
    assert set(dirs) == {"Movies", "Zzz Folder"}
    assert set(files) == {
        "a-video.mp4",
        "notes.txt",
        "Director's Cut & Extra (2024).mkv",
    }


def test_list_dir_entries_carry_expected_fields(share_root):
    entries = list_dir(share_root, share_root)
    by_name = {e.name: e for e in entries}

    video = by_name["a-video.mp4"]
    assert isinstance(video, ShareEntry)
    assert video.is_dir is False
    assert video.size == len(b"stub video bytes")
    assert video.mtime > 0
    assert video.rel_path == "a-video.mp4"

    folder = by_name["Movies"]
    assert folder.is_dir is True
    assert folder.rel_path == "Movies"


def test_list_dir_markup_name_comes_back_unchanged(share_root):
    entries = list_dir(share_root, share_root)
    names = {e.name for e in entries}
    assert "Director's Cut & Extra (2024).mkv" in names


def test_list_dir_missing_path_raises_share_unavailable(share_root):
    with pytest.raises(ShareUnavailable):
        list_dir(share_root / "does-not-exist", share_root)


def test_list_dir_unreadable_file_raises_share_unavailable(share_root):
    # Passing a plain file (not a directory) to scandir raises OSError,
    # exactly the "cannot be read" shape a broken mount produces.
    with pytest.raises(ShareUnavailable):
        list_dir(share_root / "a-video.mp4", share_root)


# --- Route tests: GET /, GET /add/{path}, GET /lang/{code} -----------------


@pytest.fixture
def _web_env(share_root, monkeypatch):
    """Env vars Settings() needs to construct at all, plus MYTUBE_SHARE_ROOT
    pointed at the fixture share -- get_settings is @lru_cache'd, so clear
    it before and after (same idiom as test_resolve.py's `_app_env`).
    """
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(share_root))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _DummyConn:
    pass


class _DummyAcquireCtx:
    async def __aenter__(self):
        return _DummyConn()

    async def __aexit__(self, *exc_info):
        return False


class _DummyPool:
    def acquire(self):
        return _DummyAcquireCtx()

    async def close(self):
        pass


@pytest.fixture
def web_client(_web_env, monkeypatch):
    """Since plan 03 the directory-listing branch also calls
    `queries.editions_by_source_paths` (the already-added chip, D-48), so
    `request.app.state.db_pool` now needs to exist -- same `_DummyPool`
    shape test_resolve.py already uses. The query itself is monkeypatched
    to "nothing already added", matching this fixture share's fixture data
    (which was never added through any of these tests).
    """
    from orchestrator.db import queries

    async def _no_editions_already_added(conn, paths):
        return []

    monkeypatch.setattr(queries, "editions_by_source_paths", _no_editions_already_added)
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_root_redirects_to_add(web_client):
    async with web_client:
        resp = await web_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/add"


async def test_root_follow_redirect_lists_mount_root(web_client, share_root):
    async with web_client:
        resp = await web_client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert "a-video.mp4" in resp.text
    assert "Movies" in resp.text


async def test_subfolder_listing_has_root_plus_one_breadcrumb_segment(web_client):
    async with web_client:
        resp = await web_client.get("/add/Movies")
    assert resp.status_code == 200
    # One separator between the root crumb and "Movies" -- zero would mean
    # only the root rendered, two would mean an extra segment leaked in.
    assert resp.text.count("breadcrumb__sep") == 1
    assert "Movies" in resp.text


async def test_percent_encoded_traversal_returns_400_and_renders_unreachable_state(
    web_client,
):
    async with web_client:
        resp = await web_client.get("/add/%2E%2E/etc")
    assert resp.status_code == 400
    assert "Cannot reach the share" in resp.text
    assert "Traceback" not in resp.text


async def test_markup_filename_renders_escaped_in_listing(web_client, monkeypatch):
    """Companion to test_safe_join_rejects_dot_dot_segment/-buried_in_the_middle
    above (the unit-level proof that safe_join itself rejects a decoded
    parent-directory segment) -- this is the route-level proof that a
    dangerous *filename* comes back inert. The literal `<script>...</script>`
    name cannot exist as a real file on this host (NTFS rejects `<`/`>`,
    see conftest.py's `share_root`), so list_dir is monkeypatched to hand
    the route exactly that name instead of fighting the filesystem for it.
    """
    dangerous = ShareEntry(
        name="<script>alert(1)</script>.mkv",
        is_dir=False,
        size=123,
        mtime=0.0,
        rel_path="<script>alert(1)</script>.mkv",
    )
    monkeypatch.setattr(routes_share, "list_dir", lambda path, root: [dangerous])

    async with web_client:
        resp = await web_client.get("/add")

    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


async def test_unknown_lang_cookie_falls_back_to_english(web_client):
    async with web_client:
        web_client.cookies.set("mytube_lang", "xx")
        resp = await web_client.get("/add")
    assert resp.status_code == 200
    assert "Add from share" in resp.text


async def test_lang_route_sets_cookie_and_redirects_to_next(web_client):
    async with web_client:
        resp = await web_client.get("/lang/ru?next=/add/Movies", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/add/Movies"
    assert resp.cookies.get("mytube_lang") == "ru"


async def test_lang_route_rejects_external_next(web_client):
    async with web_client:
        resp = await web_client.get(
            "/lang/ru?next=https://evil.example", follow_redirects=False
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
