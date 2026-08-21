"""Заливка обязана проставлять Content-Type.

Регрессия: `upload_file()` без `ExtraArgs` не передаёт тип, S3 подставляет
`application/octet-stream`, и это доезжает до клиента. Браузер такой ответ
скачивает вместо воспроизведения. Тракт при этом выглядит рабочим — 302,
200, байты совпадают, — поэтому ни один сквозной тест фазы дефект не поймал.
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
