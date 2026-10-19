"""Unit tests for the technical backfill (`orchestrator.media.backfill`)
and its command (`orchestrator.cli.backfill_technical`).

No database and no share: the rows are hand-built dicts shaped like what
`queries.editions_missing_technical` returns, the "share" is a tmp
directory with an empty file standing in for a source, and `ffprobe` is
replaced by a function returning the JSON it would have produced. What is
under test is what the routine decides -- which values it derives, what it
asks the writer to store, and what it does when the source is not there --
not ffprobe's own behaviour, which `tests/test_compat.py` already covers.
"""

from pathlib import Path

import asyncpg
import pytest

from mytube_media import compat
from orchestrator.cli import backfill_technical as cli
from orchestrator.db import queries
from orchestrator.media import backfill


def _probe_json(*, audio_codec="aac", with_video=True, audio_index=1):
    streams = []
    if with_video:
        streams.append(
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "level": 41,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "24000/1001",
                "width": 1920,
                "height": 1080,
            }
        )
    streams.append(
        {
            "index": audio_index,
            "codec_type": "audio",
            "codec_name": audio_codec,
            "channels": 2,
            "sample_rate": "48000",
            "channel_layout": "stereo",
            "tags": {"language": "jpn", "title": "Japanese"},
        }
    )
    return {
        "streams": streams,
        "format": {
            "format_name": "matroska,webm",
            "duration": "1440.032",
            "size": "6846897084",
        },
    }


def _row(**overrides):
    row = {
        "edition_id": "11111111-1111-1111-1111-111111111111",
        "title_name": "Grand Blue",
        "source_path": "tv/clip.mkv",
        "audio_stream_index": 1,
        "audio_channels": 2,
        "audio_reencoded": False,
        "size_bytes": 4321,
        "duration_seconds": 1440.032,
        "width": 1920,
        "height": 1080,
        "audio_language": "jpn",
        "audio_title": "Japanese",
    }
    row.update(overrides)
    return row


@pytest.fixture
def share(tmp_path):
    (tmp_path / "tv").mkdir()
    (tmp_path / "tv" / "clip.mkv").write_bytes(b"")
    return tmp_path


class _RecordingConn:
    """Stands in for a connection; records every write the backfill makes."""

    def __init__(self):
        self.writes = []

    async def close(self):
        pass


@pytest.fixture
def recorded(monkeypatch):
    conn = _RecordingConn()

    async def _set_edition_technical(_conn, edition_id, **kwargs):
        conn.writes.append((edition_id, kwargs))

    monkeypatch.setattr(queries, "set_edition_technical", _set_edition_technical)
    return conn


def _stub_probe(monkeypatch, result):
    def _probe(path):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(compat, "probe", _probe)


def _stub_rows(monkeypatch, rows):
    async def _editions_missing_technical(_conn):
        return rows

    monkeypatch.setattr(
        queries, "editions_missing_technical", _editions_missing_technical
    )


# --- measure_edition ---------------------------------------------------


def test_a_readable_source_comes_back_with_all_seven_parameters(monkeypatch, share):
    _stub_probe(monkeypatch, _probe_json())

    result = backfill.measure_edition(_row(), share)

    assert result.measured
    assert result.reason is None
    assert round(result.fps, 3) == 23.976
    assert result.video_profile == "High"
    assert result.video_level == 41
    assert result.pix_fmt == "yuv420p"
    assert result.audio_codec == "aac"
    assert result.audio_sample_rate == 48000
    assert result.audio_channel_layout == "stereo"


def test_a_reencoded_track_reports_the_stored_codec_not_the_probed_one(
    monkeypatch, share
):
    """The failure this prevents: two clips that are byte-for-byte the same
    kind of file in storage -- both with converted audio -- would record
    their two different *source* codecs, look like a mismatch to the
    joinability check, and get the operator refused a list that would have
    joined perfectly.
    """
    _stub_probe(monkeypatch, _probe_json(audio_codec="ac3"))

    result = backfill.measure_edition(_row(audio_reencoded=True), share)

    assert result.measured
    assert result.audio_codec == "aac"


def test_the_reencode_flag_comes_from_the_row_not_from_the_probe(monkeypatch, share):
    # The stored flag is the only record of what the remux actually did.
    # A source that is already the target codec was copied, so its codec
    # is the stored one and stays as probed.
    _stub_probe(monkeypatch, _probe_json(audio_codec="aac"))

    result = backfill.measure_edition(_row(audio_reencoded=False), share)

    assert result.audio_codec == "aac"


def test_a_source_missing_from_the_share_comes_back_unmeasured_and_named(share):
    result = backfill.measure_edition(
        _row(source_path="tv/gone.mkv", title_name="Control Clip"), share
    )

    assert not result.measured
    assert result.title_name == "Control Clip"
    assert result.reason
    assert result.fps is None


def test_a_source_path_that_escapes_the_share_comes_back_unmeasured(share):
    result = backfill.measure_edition(_row(source_path="../outside.mkv"), share)

    assert not result.measured
    assert result.reason


def test_a_probe_that_fails_comes_back_unmeasured_without_raising(monkeypatch, share):
    _stub_probe(monkeypatch, compat.ProbeError("ffprobe exited 1"))

    result = backfill.measure_edition(_row(), share)

    assert not result.measured
    assert result.reason


def test_a_file_with_no_video_stream_comes_back_unmeasured(monkeypatch, share):
    _stub_probe(monkeypatch, _probe_json(with_video=False))

    result = backfill.measure_edition(_row(), share)

    assert not result.measured
    assert result.reason


def test_an_audio_index_that_no_longer_matches_comes_back_unmeasured(
    monkeypatch, share
):
    _stub_probe(monkeypatch, _probe_json(audio_index=1))

    result = backfill.measure_edition(_row(audio_stream_index=7), share)

    assert not result.measured
    assert result.reason


# --- backfill_editions -------------------------------------------------


async def test_a_measured_edition_is_written_and_an_unmeasured_one_is_not(
    monkeypatch, share, recorded
):
    _stub_probe(monkeypatch, _probe_json())
    _stub_rows(
        monkeypatch,
        [
            _row(),
            _row(
                edition_id="22222222-2222-2222-2222-222222222222",
                title_name="Control Clip",
                source_path="tv/gone.mkv",
            ),
        ],
    )

    measured, unmeasured = await backfill.backfill_editions(recorded, share)

    assert [m.title_name for m in measured] == ["Grand Blue"]
    assert [u.title_name for u in unmeasured] == ["Control Clip"]
    assert [edition_id for edition_id, _ in recorded.writes] == [
        "11111111-1111-1111-1111-111111111111"
    ]


async def test_the_values_that_were_already_stored_are_written_back_unchanged(
    monkeypatch, share, recorded
):
    # The backfill fills what is missing; the size recorded here describes
    # the object in storage, which is not the source this routine probed.
    _stub_probe(monkeypatch, _probe_json())
    _stub_rows(monkeypatch, [_row(size_bytes=4321)])

    await backfill.backfill_editions(recorded, share)

    _, written = recorded.writes[0]
    assert written["size_bytes"] == 4321
    assert written["duration_seconds"] == 1440.032
    assert written["width"] == 1920
    assert written["height"] == 1080
    assert written["audio_reencoded"] is False
    assert written["pix_fmt"] == "yuv420p"


async def test_a_second_run_with_nothing_left_to_measure_writes_nothing(
    monkeypatch, share, recorded
):
    _stub_rows(monkeypatch, [])

    measured, unmeasured = await backfill.backfill_editions(recorded, share)

    assert measured == []
    assert unmeasured == []
    assert recorded.writes == []


# --- the command -------------------------------------------------------


@pytest.fixture
def _no_real_connect(monkeypatch):
    async def _fake_connect(*args, **kwargs):
        return _RecordingConn()

    monkeypatch.setattr(asyncpg, "connect", _fake_connect)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from conftest import ADMIN_FQDN, WORKER_TOKEN

    from orchestrator.config import get_settings

    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    # The share fixture builds its tree under the same tmp_path, so the
    # command reads the same directory the routine tests use.
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_the_command_exits_zero_when_nothing_was_left_unmeasured(
    monkeypatch, share, recorded, _no_real_connect, capsys
):
    _stub_probe(monkeypatch, _probe_json())
    _stub_rows(monkeypatch, [_row()])

    code = await cli.backfill_technical()

    assert code == 0
    assert "Grand Blue" in capsys.readouterr().out


async def test_the_command_exits_non_zero_and_names_what_it_could_not_measure(
    monkeypatch, share, recorded, _no_real_connect, capsys
):
    _stub_rows(
        monkeypatch, [_row(title_name="Control Clip", source_path="tv/gone.mkv")]
    )

    code = await cli.backfill_technical()

    assert code != 0
    assert "Control Clip" in capsys.readouterr().out


async def test_the_command_reports_an_already_measured_library_and_exits_zero(
    monkeypatch, share, recorded, _no_real_connect, capsys
):
    _stub_rows(monkeypatch, [])

    code = await cli.backfill_technical()

    assert code == 0
    assert capsys.readouterr().out.strip()


def test_the_probe_is_not_reinvoked_here():
    # The measurement goes through the module that already runs ffprobe,
    # never a second command line of its own.
    source = Path(backfill.__file__).read_text(encoding="utf-8")
    assert "ffprobe" not in source
