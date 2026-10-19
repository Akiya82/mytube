"""The converter's own storage client is built from its own credential --
never the service's, even when both pairs sit in the same environment
file.

Every assertion below names the variable rather than the object it
produces, on purpose: a test that only checked "a client was built"
would still pass under the substitution this file exists to catch --
a converter that quietly uploads with the service's own credential
because a shared environment file happened to define it, which is not a
hypothetical, it is what a single-machine setup does by default when the
names collide.
"""
from unittest.mock import MagicMock

import pytest

import worker.storage as storage
from worker.settings import WorkerSettings


def _set_worker_env(monkeypatch, *, bucket: str = "mytube") -> None:
    monkeypatch.setenv("MYTUBE_ORCHESTRATOR_URL", "http://orchestrator.local")
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", "shared-token-do-not-use")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_BUCKET", bucket)
    monkeypatch.setenv("MYTUBE_WORKER_S3_KEY", "worker-key")
    monkeypatch.setenv("MYTUBE_WORKER_S3_SECRET", "worker-secret")


def test_build_client_uses_the_workers_own_credential_not_the_services(monkeypatch):
    """Both pairs present, deliberately different -- the constructed
    client must carry the value from MYTUBE_WORKER_S3_KEY/
    MYTUBE_WORKER_S3_SECRET, not from MYTUBE_S3_KEY/MYTUBE_S3_SECRET.
    """
    captured = {}

    def fake_boto3_client(service_name, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(storage.boto3, "client", fake_boto3_client)

    monkeypatch.setenv("MYTUBE_ORCHESTRATOR_URL", "http://orchestrator.local")
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", "shared-token-do-not-use")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_BUCKET", "mytube")
    # The service's own pair -- present, and deliberately different from
    # the worker's own pair below, so a substitution would be visible.
    monkeypatch.setenv("MYTUBE_S3_KEY", "service-key-DO-NOT-USE")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "service-secret-DO-NOT-USE")
    monkeypatch.setenv("MYTUBE_WORKER_S3_KEY", "worker-key")
    monkeypatch.setenv("MYTUBE_WORKER_S3_SECRET", "worker-secret")

    settings = WorkerSettings()
    storage.build_client(settings)

    assert captured["service_name"] == "s3"
    assert captured["aws_access_key_id"] == "worker-key"
    assert captured["aws_secret_access_key"] == "worker-secret"


def test_absent_worker_credential_pair_fails_naming_a_worker_variable(monkeypatch):
    """With MYTUBE_S3_KEY/MYTUBE_S3_SECRET present and the worker pair
    entirely absent, building the settings fails -- not a fallback, not
    a default, not an empty string.
    """
    monkeypatch.setenv("MYTUBE_ORCHESTRATOR_URL", "http://orchestrator.local")
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", "shared-token-do-not-use")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_BUCKET", "mytube")
    monkeypatch.setenv("MYTUBE_S3_KEY", "service-key-DO-NOT-USE")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "service-secret-DO-NOT-USE")
    monkeypatch.delenv("MYTUBE_WORKER_S3_KEY", raising=False)
    monkeypatch.delenv("MYTUBE_WORKER_S3_SECRET", raising=False)

    with pytest.raises(Exception) as exc_info:
        WorkerSettings()

    message = str(exc_info.value).lower()
    assert "worker_s3_key" in message


def test_settings_and_client_need_none_of_the_services_own_required_vars(monkeypatch):
    """Constructing the client requires none of the service's other
    required settings to be present, and importing the settings module
    raises nothing when every service variable is absent from the
    environment.
    """
    for var in (
        "MYTUBE_SECURE_LINK_SECRET",
        "MYTUBE_DATABASE_URL",
        "MYTUBE_ADMIN_FQDN",
        "MYTUBE_PUBLIC_BASE_URL",
        "MYTUBE_S3_KEY",
        "MYTUBE_S3_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    _set_worker_env(monkeypatch)

    import importlib

    import worker.settings as worker_settings_module

    importlib.reload(worker_settings_module)  # must not raise

    settings = worker_settings_module.WorkerSettings()

    import worker.storage as storage_module

    client = storage_module.build_client(settings)
    assert client is not None


def test_the_converter_defaults_to_the_same_storage_region_as_the_service():
    """The region signs every request, so the two processes talking to one
    MinIO have to agree on it. They disagree the moment one default drifts
    from the other -- and the symptom is SignatureDoesNotMatch on the
    upload, at the end of a conversion the card has already paid for, not
    at start-up where a misconfiguration belongs.

    The converter cannot import the service's settings class to read the
    value (that is the whole point of `worker/__init__.py`'s rule), so
    this test is what actually holds the two literals together.
    """
    from orchestrator.config import Settings

    assert (
        WorkerSettings.model_fields["s3_region"].default
        == Settings.model_fields["s3_region"].default
    )


def test_upload_calls_against_the_configured_bucket(monkeypatch):
    _set_worker_env(monkeypatch, bucket="mytube")
    settings = WorkerSettings()
    client = MagicMock()

    storage.upload_file(client, settings, "/scratch/x.mp4", "abc.mp4")

    args, kwargs = client.upload_file.call_args
    assert args[1] == "mytube"
    assert args[2] == "abc.mp4"
    assert kwargs["ExtraArgs"]["ContentType"] == "video/mp4"


def test_abort_multipart_calls_against_the_configured_bucket(monkeypatch):
    _set_worker_env(monkeypatch, bucket="mytube")
    settings = WorkerSettings()
    client = MagicMock()
    client.list_multipart_uploads.return_value = {
        "Uploads": [{"Key": "abc.mp4", "UploadId": "u1"}]
    }

    storage.abort_multipart(client, settings, "abc.mp4")

    client.list_multipart_uploads.assert_called_once_with(
        Bucket="mytube", Prefix="abc.mp4"
    )
    client.abort_multipart_upload.assert_called_once_with(
        Bucket="mytube", Key="abc.mp4", UploadId="u1"
    )


def test_abort_multipart_no_uploads_makes_no_call_and_does_not_raise(monkeypatch):
    _set_worker_env(monkeypatch, bucket="mytube")
    settings = WorkerSettings()
    client = MagicMock()
    client.list_multipart_uploads.return_value = {"Uploads": []}

    storage.abort_multipart(client, settings, "abc.mp4")  # must not raise

    client.abort_multipart_upload.assert_not_called()
