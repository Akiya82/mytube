"""Path containment (safe_join) and directory listing (list_dir) for the
mounted network share. `tmp_path` stands in for the CIFS/NFS mount --
`orchestrator.media.share` cannot tell the difference, which is the point.
"""
import pytest

from orchestrator.media.share import ShareEntry, ShareEscape, ShareUnavailable, list_dir, safe_join


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
