"""The only module that talks to object storage.

Base S3 API only (boto3): put_object/upload_file, head_object, head_bucket,
create_bucket. No vendor-specific admin calls — swapping the storage
candidate must not break on one particular call (Pitfall F, 01-RESEARCH.md).
Client parameters (signature_version, addressing_style) are the ones
confirmed working against chrislusf/seaweedfs:3.97 in
src/docs/experiments/07-object-storage.md.
"""
import logging

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from orchestrator.config import get_settings

logger = logging.getLogger(__name__)


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


def put_object_from_file(path: str, key: str) -> None:
    """Upload a file under object key `key`.

    ContentType is set explicitly. Without it boto3 sends no type at all, S3
    stores `application/octet-stream`, and that reaches the client verbatim:
    a browser downloads such a response instead of playing it, and a player
    has to guess the format. The type is a constant rather than a guess from
    the file extension because the service serves progressive MP4 only.
    """
    settings = get_settings()
    get_s3_client().upload_file(
        path, settings.s3_bucket, key, ExtraArgs={"ContentType": "video/mp4"}
    )


def head_object(key: str) -> dict:
    settings = get_settings()
    return get_s3_client().head_object(Bucket=settings.s3_bucket, Key=key)


def delete_object(key: str) -> None:
    settings = get_settings()
    get_s3_client().delete_object(Bucket=settings.s3_bucket, Key=key)


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
