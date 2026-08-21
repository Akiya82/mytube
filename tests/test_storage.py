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
