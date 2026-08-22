"""What the operator is told when an add fails.

`_reason_key_for` is pure and reachable without a database, a bucket or
ffmpeg -- the mapping is worth its own quick-loop test because a wrong
reason costs more than a vague one: it sends the operator to look at the
wrong subsystem.
"""
from orchestrator.media import queue as queue_module
from orchestrator.media.compat import ProbeError
from orchestrator.media.remux import RemuxError
from orchestrator.media.share import ShareEscape

_reason_key_for = queue_module._reason_key_for


def test_the_space_precheck_reports_storage_full():
    exc = queue_module._InsufficientSpaceError("0 free bytes < 1 required")
    assert _reason_key_for(exc) == "reason.storage_full"


def test_an_unreadable_or_rejected_source_reports_unreadable():
    assert _reason_key_for(ProbeError("no")) == "reason.unreadable"
    assert _reason_key_for(OSError("gone")) == "reason.unreadable"
    assert _reason_key_for(ShareEscape("outside")) == "reason.unreadable"


def test_ffmpeg_failing_is_reported_as_a_remux_failure_not_an_upload_one():
    """The most likely real failure in this pipeline is ffmpeg exiting
    non-zero or hitting its hour timeout. Reporting that as "the upload to
    storage did not finish" describes a file that never reached storage.
    """
    assert _reason_key_for(RemuxError("ffmpeg exited 1")) == "failed.reason.remux"


def test_only_a_failure_at_an_upload_call_site_reports_an_upload_failure():
    assert _reason_key_for(queue_module._UploadError("x.mp4: boom")) == (
        "failed.reason.upload"
    )


def test_an_unexpected_failure_gets_a_neutral_reason_rather_than_a_wrong_one():
    assert _reason_key_for(RuntimeError("db write exploded")) == (
        "failed.reason.unknown"
    )
