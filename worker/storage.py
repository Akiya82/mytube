"""The converter's own storage client, built from its own scoped
credential -- never `orchestrator.storage.get_s3_client()`, which
reads the service's own settings object this process does not have.

Three operations only: build the client, upload a file, and abort a
dangling multipart upload of its own. The call shapes mirror
`orchestrator/storage.py`'s, deliberately, because they are the same
operations against the same storage -- but the client here is always
constructed from `WorkerSettings`, so the credential is this program's
own.

**No delete, anywhere in this module.** The converter's storage account
carries put and abort-multipart within one bucket, nothing else.
Removing an object a superseded report left behind is the service's job:
it knows the key from the job row and holds the bucket's full credential
(`orchestrator/web/routes_worker.py`'s `_reject_superseded`). A delete
function here would be code that either fails against this account's own
policy or reads as an argument for widening that policy later -- the
obvious implementation is the one where the uploader cleans up after
itself, and that is exactly the implementation this module refuses to
write.
"""

import boto3
from botocore.client import Config

from worker.settings import WorkerSettings


def build_client(settings: WorkerSettings):
    """One boto3 S3 client, built from the converter's own credential.
    Same client parameters `orchestrator.storage.get_s3_client()` uses
    against the same storage backend (signature_version s3v4, path-style
    addressing) -- the two clients talk to the same bucket, just under
    different credentials.
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.worker_s3_key,
        aws_secret_access_key=settings.worker_s3_secret,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def upload_file(
    client,
    settings: WorkerSettings,
    path: str,
    key: str,
    content_type: str = "video/mp4",
) -> None:
    """Multipart-capable upload -- the same call shape as
    `orchestrator.storage.put_object_from_file`, for the same reason:
    `ContentType` set explicitly, since an unset type stores
    `application/octet-stream` and a browser downloads that instead of
    playing it.
    """
    client.upload_file(
        path, settings.s3_bucket, key, ExtraArgs={"ContentType": content_type}
    )


def abort_multipart(client, settings: WorkerSettings, key: str) -> None:
    """Best-effort cleanup after a failed `upload_file()` call, or after a
    refused report -- lists uploads for this exact key and aborts each one
    found. Mirrors `orchestrator.storage.abort_dangling_multipart` exactly
    (a managed transfer does not hand back its `UploadId` on failure, so
    listing-then-aborting is the only way to do this explicitly). With
    none found this makes no abort call and never raises.
    """
    response = client.list_multipart_uploads(Bucket=settings.s3_bucket, Prefix=key)
    for upload in response.get("Uploads", []):
        if upload.get("Key") != key:
            # Prefix matching can surface a different key that merely
            # starts with this one -- only abort the exact key requested.
            continue
        client.abort_multipart_upload(
            Bucket=settings.s3_bucket, Key=key, UploadId=upload["UploadId"]
        )
