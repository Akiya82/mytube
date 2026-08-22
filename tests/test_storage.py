"""Uploads must set a Content-Type.

Regression: `upload_file()` with no `ExtraArgs` sends no type, S3 stores
`application/octet-stream`, and that reaches the client. A browser downloads
such a response instead of playing it. The delivery path still looks healthy
while this is broken — 302, 200, bytes match — which is why no end-to-end
test in the phase caught it.
"""

from unittest.mock import MagicMock

import orchestrator.storage as storage


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
