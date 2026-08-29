"""The only module that talks to object storage.

Base S3 API only (boto3): put_object/upload_file, head_object, head_bucket,
create_bucket. No vendor-specific admin calls — swapping the storage
candidate must not break on one particular call (Pitfall F, 01-RESEARCH.md).
Client parameters (signature_version, addressing_style) are the ones
confirmed working against chrislusf/seaweedfs:3.97 in
src/docs/experiments/07-object-storage.md.
"""
import logging
import time
from dataclasses import dataclass

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from orchestrator.config import get_settings

logger = logging.getLogger(__name__)

# D-67: storage_usage() lists the whole bucket, which is too expensive to
# do on every page render. Sixty seconds is short enough that a fresh add
# shows up in the header within one reload, long enough that navigating
# the library never triggers a second full enumeration.
_USAGE_CACHE_SECONDS = 60.0
_usage_cache: dict = {"usage": None, "expires_at": 0.0}


def get_s3_client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_key,
        aws_secret_access_key=settings.s3_secret,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def put_object_from_file(path: str, key: str, content_type: str = "video/mp4") -> None:
    """Upload a file under object key `key`.

    ContentType is set explicitly. Without it boto3 sends no type at all, S3
    stores `application/octet-stream`, and that reaches the client verbatim:
    a browser downloads such a response instead of playing it, and a player
    has to guess the format. `video/mp4` stays the default so every existing
    caller is unchanged; the poster upload (D-72) passes `image/jpeg`.
    """
    settings = get_settings()
    get_s3_client().upload_file(
        path, settings.s3_bucket, key, ExtraArgs={"ContentType": content_type}
    )


def get_object_bytes(key: str) -> bytes:
    """Read a whole object into memory -- used to serve the poster frame
    through the orchestrator, since the edge only exposes the video object
    path (`/blob/{edition_id}.mp4`), not a second one for posters. Posters
    are a few tens of KB (D-72), so reading the whole body is fine; this
    is not the path a multi-gigabyte video ever goes through.
    """
    settings = get_settings()
    response = get_s3_client().get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read()


def download_object_to_file(key: str, destination: str) -> None:
    """Stream object `key` onto local disk, the mirror of
    `put_object_from_file` above and the managed transfer's own download
    half -- chunked, resumable in the same way the upload is, and it never
    holds the body in memory.

    It exists next to `get_object_bytes` rather than instead of it because
    the two answer different sizes. That function reads a whole body into
    the process, which is right for a poster measured in tens of kilobytes
    and says so in its own docstring; a playlist member is measured in
    gigabytes, and reading one that way would take the container's memory
    with it while also stalling every request this process is serving.
    """
    settings = get_settings()
    get_s3_client().download_file(settings.s3_bucket, key, destination)


def head_object(key: str) -> dict:
    settings = get_settings()
    return get_s3_client().head_object(Bucket=settings.s3_bucket, Key=key)


def delete_object(key: str) -> None:
    settings = get_settings()
    get_s3_client().delete_object(Bucket=settings.s3_bucket, Key=key)


def abort_dangling_multipart(key: str) -> None:
    """Best-effort cleanup after a failed `upload_file()` call for `key`
    (D-53). `upload_file`'s managed transfer does not hand back its
    `UploadId` on failure, so the only way to abort "explicitly" rather
    than waiting on the bucket lifecycle rule (`ensure_multipart_lifecycle_
    rule`) is to list uploads for this exact key and abort each one found.
    With none found this makes no abort call and never raises -- a clean
    upload (or one that never got as far as a multipart start) is not an
    error here.
    """
    settings = get_settings()
    client = get_s3_client()
    response = client.list_multipart_uploads(Bucket=settings.s3_bucket, Prefix=key)
    for upload in response.get("Uploads", []):
        if upload.get("Key") != key:
            # Prefix matching can surface a different key that merely
            # starts with this one -- only abort the exact key requested.
            continue
        client.abort_multipart_upload(
            Bucket=settings.s3_bucket, Key=key, UploadId=upload["UploadId"]
        )


def ensure_multipart_lifecycle_rule() -> None:
    """Idempotent bucket bootstrap, same shape as `ensure_bucket`: a
    lifecycle rule aborting any incomplete multipart upload one day after
    initiation, as the backstop behind the explicit
    `abort_dangling_multipart` call above (D-53). Standard S3 lifecycle
    API, so it works against the external endpoint and the bundled one
    alike -- no vendor-specific admin call.

    Never raises the container to a halt when the endpoint does not
    support this call: caught, logged with the manual equivalent, and
    execution continues. A storage backend without lifecycle support is
    degraded, not broken -- the explicit abort is the primary cleanup path
    regardless, and this rule is only ever the safety net behind it.
    """
    settings = get_settings()
    client = get_s3_client()
    try:
        client.put_bucket_lifecycle_configuration(
            Bucket=settings.s3_bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "abort-incomplete-multipart",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    }
                ]
            },
        )
        logger.info("multipart lifecycle rule ensured on bucket %s", settings.s3_bucket)
    except ClientError as exc:
        logger.warning(
            "could not set the multipart lifecycle rule on bucket %s (%s) -- "
            "the explicit abort_dangling_multipart call remains the primary "
            "cleanup path; apply the rule manually if this backend supports "
            "it, via 'aws --endpoint-url %s s3api put-bucket-lifecycle-"
            "configuration --bucket %s --lifecycle-configuration "
            "'{\"Rules\":[{\"ID\":\"abort-incomplete-multipart\",\"Status\":"
            "\"Enabled\",\"Filter\":{\"Prefix\":\"\"},\"AbortIncompleteMultipart"
            "Upload\":{\"DaysAfterInitiation\":1}}]}''",
            settings.s3_bucket,
            exc,
            settings.s3_endpoint,
            settings.s3_bucket,
        )


def list_all_objects() -> dict[str, int]:
    """Every object key and its size, paginated -- the raw listing
    `storage_usage` caches and `reconcile` compares against the catalog.
    No caching in this function itself; `storage_usage` owns the cache
    window so a caller that genuinely needs a fresh listing can still get
    one.
    """
    settings = get_settings()
    client = get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    objects: dict[str, int] = {}
    for page in paginator.paginate(Bucket=settings.s3_bucket):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = obj["Size"]
    return objects


@dataclass(frozen=True)
class StorageUsage:
    """What the interface's storage-usage line needs (D-67): total bytes
    and object count from the bucket listing, plus free bytes computed
    from the configured quota when one is set (`storage_quota_bytes > 0`)
    and `None` when no quota is enforced. `listing` carries the same raw
    key->size mapping `reconcile` needs, so a caller already holding a
    `StorageUsage` never has to re-list the bucket to reconcile it.
    """

    total_bytes: int
    object_count: int
    free_bytes: int | None
    listing: dict[str, int]


def storage_usage() -> StorageUsage:
    """Cached for sixty seconds (`_USAGE_CACHE_SECONDS`) in a module-level
    value -- drawing a page must not list the whole bucket on every
    navigation. The raw listing is cached alongside the totals, not just
    the totals, so `reconcile` can reuse it without a second full
    enumeration.
    """
    now = time.monotonic()
    cached: StorageUsage | None = _usage_cache["usage"]
    if cached is not None and _usage_cache["expires_at"] > now:
        return cached

    listing = list_all_objects()
    settings = get_settings()
    total_bytes = sum(listing.values())
    free_bytes = None
    if settings.storage_quota_bytes > 0:
        free_bytes = max(settings.storage_quota_bytes - total_bytes, 0)

    usage = StorageUsage(
        total_bytes=total_bytes,
        object_count=len(listing),
        free_bytes=free_bytes,
        listing=listing,
    )
    _usage_cache["usage"] = usage
    _usage_cache["expires_at"] = now + _USAGE_CACHE_SECONDS
    return usage


@dataclass(frozen=True)
class UnreferencedObject:
    """A key present in the bucket listing that no catalog row points at."""

    key: str
    size_bytes: int


@dataclass(frozen=True)
class MissingObject:
    """A catalog row whose key is absent from the bucket listing --
    carries the slug through, since the reconciliation report has to name
    the /v/{slug} address it breaks, not just the bare key.
    """

    key: str
    kind: str
    title_name: str
    slug: str | None


@dataclass(frozen=True)
class Reconciliation:
    """The two sets that matter for comparing what the catalog thinks
    exists against what the bucket actually holds."""

    unreferenced: list[UnreferencedObject]
    missing: list[MissingObject]


def reconcile(listing: dict[str, int], referenced) -> Reconciliation:
    """Pure function -- no network, no database. Takes a bucket listing
    (key -> size, from `list_all_objects`/`storage_usage().listing`) and
    the catalog rows a caller already fetched (each item indexable by
    'key', 'kind', 'title_name', 'slug' -- an `asyncpg.Record` from
    `queries.referenced_objects` or a plain dict both work), and returns
    the two sets that matter: objects nothing in the catalog points at,
    and catalog rows whose object is missing from the bucket.

    Lives here, as a function of two arguments, precisely so that two
    different callers in two different plans can use it without either
    importing the other: a library header can render the counts from it,
    an operator's audit command can print the detail from it. One
    definition of "unreferenced", not two that disagree at 3am.
    """
    referenced_keys = {row["key"] for row in referenced}
    unreferenced = [
        UnreferencedObject(key=key, size_bytes=size)
        for key, size in listing.items()
        if key not in referenced_keys
    ]
    missing = [
        MissingObject(
            key=row["key"],
            kind=row["kind"],
            title_name=row["title_name"],
            slug=row["slug"],
        )
        for row in referenced
        if row["key"] not in listing
    ]
    return Reconciliation(unreferenced=unreferenced, missing=missing)


def ensure_bucket() -> None:
    """Idempotent bucket bootstrap (verdict M1) — safe to call on every
    container start. head_bucket, and only create_bucket if it's actually
    missing; any other failure gets a message pointing at the exact manual
    command from src/docs/experiments/07-object-storage.md rather than a
    bare boto3 traceback.
    """
    settings = get_settings()
    client = get_s3_client()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
        logger.info("bucket %s already exists", settings.s3_bucket)
        return
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status != 404:
            raise RuntimeError(
                f"head_bucket on '{settings.s3_bucket}' failed unexpectedly "
                f"({exc}). Fall back to the manual bring-up sequence in "
                "src/docs/experiments/07-object-storage.md: "
                f"aws --endpoint-url {settings.s3_endpoint} s3api create-bucket "
                f"--bucket {settings.s3_bucket}"
            ) from exc

    try:
        client.create_bucket(Bucket=settings.s3_bucket)
        logger.info("created bucket %s", settings.s3_bucket)
    except ClientError as exc:
        raise RuntimeError(
            f"create_bucket for '{settings.s3_bucket}' failed ({exc}). "
            "Fall back to the manual bring-up sequence in "
            f"src/docs/experiments/07-object-storage.md: aws --endpoint-url "
            f"{settings.s3_endpoint} s3api create-bucket --bucket {settings.s3_bucket}"
        ) from exc
