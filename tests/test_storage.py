"""Uploads must set a Content-Type.

Regression: `upload_file()` with no `ExtraArgs` sends no type, S3 stores
`application/octet-stream`, and that reaches the client. A browser downloads
such a response instead of playing it. The delivery path still looks healthy
while this is broken — 302, 200, bytes match — which is why no end-to-end
test in the phase caught it.
"""

from unittest.mock import MagicMock

import pytest

import orchestrator.storage as storage


@pytest.fixture(autouse=True)
def _reset_usage_cache():
    """No test in this file inherits another's cache -- the module-level
    dict is process-global and several tests below deliberately prime it.
    """
    storage.invalidate_usage_cache()
    yield
    storage.invalidate_usage_cache()


def test_upload_sets_video_mp4_content_type(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    monkeypatch.setattr(storage, "get_settings", lambda: MagicMock(s3_bucket="mytube"))

    storage.put_object_from_file("/ingest/x.mp4", "abc.mp4")

    _, kwargs = client.upload_file.call_args
    assert kwargs["ExtraArgs"]["ContentType"] == "video/mp4"


def test_upload_accepts_a_custom_content_type_for_the_poster(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    monkeypatch.setattr(storage, "get_settings", lambda: MagicMock(s3_bucket="mytube"))

    storage.put_object_from_file("/work/x.jpg", "abc.jpg", content_type="image/jpeg")

    _, kwargs = client.upload_file.call_args
    assert kwargs["ExtraArgs"]["ContentType"] == "image/jpeg"


def test_abort_dangling_multipart_aborts_each_upload_found(monkeypatch):
    client = MagicMock()
    client.list_multipart_uploads.return_value = {
        "Uploads": [{"Key": "abc.mp4", "UploadId": "u1"}, {"Key": "abc.mp4", "UploadId": "u2"}]
    }
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    monkeypatch.setattr(storage, "get_settings", lambda: MagicMock(s3_bucket="mytube"))

    storage.abort_dangling_multipart("abc.mp4")

    assert client.abort_multipart_upload.call_count == 2
    client.abort_multipart_upload.assert_any_call(Bucket="mytube", Key="abc.mp4", UploadId="u1")
    client.abort_multipart_upload.assert_any_call(Bucket="mytube", Key="abc.mp4", UploadId="u2")


def test_abort_dangling_multipart_no_uploads_makes_no_call_and_does_not_raise(monkeypatch):
    client = MagicMock()
    client.list_multipart_uploads.return_value = {"Uploads": []}
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    monkeypatch.setattr(storage, "get_settings", lambda: MagicMock(s3_bucket="mytube"))

    storage.abort_dangling_multipart("abc.mp4")  # must not raise

    client.abort_multipart_upload.assert_not_called()


def test_ensure_multipart_lifecycle_rule_swallows_unsupported_operation(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.put_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "NotImplemented", "Message": "nope"}},
        "PutBucketLifecycleConfiguration",
    )
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: MagicMock(s3_bucket="mytube", s3_endpoint="http://storage:8333"),
    )

    storage.ensure_multipart_lifecycle_rule()  # must not raise


def test_get_object_bytes_returns_the_body(monkeypatch):
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = b"jpeg-bytes"
    client.get_object.return_value = {"Body": body}
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    monkeypatch.setattr(storage, "get_settings", lambda: MagicMock(s3_bucket="mytube"))

    assert storage.get_object_bytes("abc.jpg") == b"jpeg-bytes"


def test_reconcile_reports_unreferenced_object_with_its_size():
    listing = {"a.mp4": 100, "b.mp4": 200}
    referenced = [{"key": "a.mp4", "kind": "video", "title_name": "A", "slug": "a"}]

    result = storage.reconcile(listing, referenced)

    assert len(result.unreferenced) == 1
    assert result.unreferenced[0].key == "b.mp4"
    assert result.unreferenced[0].size_bytes == 200
    assert result.missing == []


def test_reconcile_reports_missing_object_with_its_slug_carried_through():
    listing = {"a.mp4": 100}
    referenced = [
        {"key": "a.mp4", "kind": "video", "title_name": "A", "slug": "a"},
        {"key": "gone.mp4", "kind": "video", "title_name": "Gone", "slug": "gone-slug"},
    ]

    result = storage.reconcile(listing, referenced)

    assert result.unreferenced == []
    assert len(result.missing) == 1
    assert result.missing[0].key == "gone.mp4"
    assert result.missing[0].slug == "gone-slug"
    assert result.missing[0].title_name == "Gone"


# --- The cached listing stops outliving the bucket -----------------------
#
# storage_usage() caches the raw bucket listing for _USAGE_CACHE_SECONDS.
# Before this plan, nothing ever cleared that cache early: a delete or an
# upload made from this same process left the next read comparing a
# listing from before the write against catalog rows from after it, for
# up to sixty seconds. These tests stub the S3 client's paginator (the
# actual call list_all_objects makes) and drive it through storage_usage,
# delete_object and put_object_from_file exactly as those functions call
# each other -- not through invalidate_usage_cache in isolation, since
# the regression this guards is "the two write functions still call it",
# not "the function itself clears two dict keys".


def _stub_listing_client(monkeypatch, *pages):
    """A MagicMock S3 client whose paginator answers `pages` in order --
    one call to `storage_usage()` (via `list_all_objects`) consumes one
    page-set. Settings carry no quota, matching every other test in this
    file that never sets `storage_quota_bytes`.
    """
    client = MagicMock()
    client.get_paginator.return_value.paginate.side_effect = list(pages)
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    monkeypatch.setattr(
        storage, "get_settings", lambda: MagicMock(s3_bucket="mytube", storage_quota_bytes=0)
    )
    return client


def test_delete_object_invalidates_the_cached_usage_listing(monkeypatch):
    client = _stub_listing_client(
        monkeypatch,
        [{"Contents": [{"Key": "a.mp4", "Size": 100}]}],
        [{"Contents": []}],
    )

    first = storage.storage_usage()
    assert "a.mp4" in first.listing
    assert client.get_paginator.return_value.paginate.call_count == 1

    storage.delete_object("a.mp4")

    second = storage.storage_usage()
    assert "a.mp4" not in second.listing
    assert client.get_paginator.return_value.paginate.call_count == 2


def test_put_object_from_file_invalidates_the_cached_usage_listing(monkeypatch):
    client = _stub_listing_client(
        monkeypatch,
        [{"Contents": []}],
        [{"Contents": [{"Key": "new.mp4", "Size": 500}]}],
    )

    first = storage.storage_usage()
    assert "new.mp4" not in first.listing
    assert client.get_paginator.return_value.paginate.call_count == 1

    storage.put_object_from_file("/ingest/new.mp4", "new.mp4")

    second = storage.storage_usage()
    assert "new.mp4" in second.listing
    assert client.get_paginator.return_value.paginate.call_count == 2


def test_a_freshly_deleted_key_is_not_reported_as_unreferenced_once_the_cache_is_invalidated(
    monkeypatch,
):
    """The exact regression the owner reported: a bucket listing cached
    before a delete, compared against catalog rows read after it, makes
    a just-deleted key look unreferenced for up to sixty seconds.
    """
    client = _stub_listing_client(
        monkeypatch,
        [{"Contents": [{"Key": "a.mp4", "Size": 100}]}],
        [{"Contents": []}],
    )
    referenced: list[dict] = []  # the catalog row for a.mp4 is already gone too

    storage.storage_usage()  # warms the cache with a.mp4 still present
    storage.delete_object("a.mp4")  # the write this process makes

    usage = storage.storage_usage()
    result = storage.reconcile(usage.listing, referenced)

    assert client.get_paginator.return_value.paginate.call_count == 2
    assert all(obj.key != "a.mp4" for obj in result.unreferenced)


def test_invalidate_usage_cache_clears_both_fields_directly():
    storage._usage_cache["usage"] = "not-none"
    storage._usage_cache["expires_at"] = 9_999_999_999.0

    storage.invalidate_usage_cache()

    assert storage._usage_cache["usage"] is None
    assert storage._usage_cache["expires_at"] == 0.0


def test_two_consecutive_usage_reads_with_no_write_share_one_cached_listing(monkeypatch):
    client = _stub_listing_client(monkeypatch, [{"Contents": []}])

    storage.storage_usage()
    storage.storage_usage()

    assert client.get_paginator.return_value.paginate.call_count == 1
