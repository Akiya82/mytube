"""The converter's own settings -- a strict subset of the service's, read
from the same `MYTUBE_` prefix so an operator running both on one machine
has one environment file, but never importing `orchestrator.config`
itself (see `worker/__init__.py`).

**The storage credential carries its own prefix, with no fallback.**
The converter gets a narrower storage account -- put and
abort-multipart within one bucket, nothing else -- and the whole point of
a narrower account is defeated if the process silently picks up the
service's wider one. `MYTUBE_S3_KEY`/`MYTUBE_S3_SECRET` are the service's
own pair; this class never reads them. `MYTUBE_WORKER_S3_KEY`/
`MYTUBE_WORKER_S3_SECRET` are the converter's own, required with no
default -- an absent worker key is an error naming the worker variable,
never a silent promotion to the service's key. Sharing a variable name
with the service would make that substitution not merely possible but
the *default outcome* on a single machine with one environment file.

The storage endpoint, bucket and region *do* share names with the
service, because they are the same endpoint and the same bucket -- only
the credential is the converter's own.
"""

import socket

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mytube_media.transcode import MAX_OUTPUT_WIDTH as _SHARED_MAX_OUTPUT_WIDTH

# The free-memory floor below which the converter reports "waiting for
# GPU" on every poll instead of claiming. No measured VRAM
# figure exists for this project yet -- experiment 10's end-to-end
# 1080p NVENC+NVDEC run on the real card recorded encoder/decoder
# *utilisation* (74%/48% peak) but not a VRAM figure -- so this default
# is a margin, not a measurement: generous enough to leave headroom for
# the embedder, reranker and speech-to-text containers already sharing
# the card's 8 GiB, and
# comfortably above what one 1080p NVENC session plus its NVDEC decode
# buffers need. Override with MYTUBE_WORKER_VRAM_MIN_MIB if a real job
# is ever refused with room actually free, or claims and then fails for
# memory anyway -- either direction is a measurement this default should
# then be replaced by.
DEFAULT_VRAM_MIN_MIB = 1536


class WorkerSettings(BaseSettings):
    """Typed settings loaded from the environment (`MYTUBE_` prefix),
    read once by `worker.__main__` at start.
    """

    # env_ignore_empty=True: an empty MYTUBE_WORKER_NAME must be read as
    # unset, not as the empty string, or worker_name's own hostname default
    # below never runs and every check-in is recorded under "".
    model_config = SettingsConfigDict(env_prefix="MYTUBE_", env_ignore_empty=True)

    # --- required, no default -------------------------------------------
    # The service's address, the shared credential, the storage endpoint
    # and bucket, and the converter's own storage key and secret. Not
    # even an empty string counts as a value -- see the module docstring
    # for why the last two carry the converter's own prefix.
    orchestrator_url: str
    worker_token: str
    s3_endpoint: str
    s3_bucket: str
    worker_s3_key: str
    worker_s3_secret: str

    # --- shared names with the service, same defaults --------------------
    # The region is part of every signature this process produces, so it
    # is not cosmetic the way an arbitrary bucket label is: give the
    # converter a different region from the one the storage was set up
    # with and every upload comes back SignatureDoesNotMatch -- after the
    # whole encode has already been paid for on the card. This default has
    # to stay equal to `orchestrator.config.Settings.s3_region`'s (a test
    # in `tests/test_worker_storage.py` holds the two together, since this
    # package cannot import that class to read it), and a deployment that
    # overrides one has to set MYTUBE_S3_REGION for the other as well.
    s3_region: str = "mytube"
    share_root: str = "/share"
    work_dir: str = "/work"

    # --- the converter's own, defaulted -----------------------------------
    worker_name: str = Field(default_factory=socket.gethostname)
    worker_vram_min_mib: int = DEFAULT_VRAM_MIN_MIB
    worker_poll_seconds: float = 5.0
    # A lever, not a resolution setting somebody is invited to fiddle
    # with: if the five-minute measurement on the real card comes back
    # long, dropping this is the one adjustment that changes the
    # encode's cost substantially, and mytube_media.transcode's
    # bitrate-by-area formula follows it down with no other edit.
    worker_max_output_width: int = _SHARED_MAX_OUTPUT_WIDTH

    @field_validator("orchestrator_url")
    @classmethod
    def _validate_orchestrator_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(
                f"MYTUBE_ORCHESTRATOR_URL must start with http:// or https:// "
                f"(got {value!r})"
            )
        return value.rstrip("/")

    @field_validator("worker_token")
    @classmethod
    def _validate_worker_token(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("MYTUBE_WORKER_TOKEN must not be empty.")
        return value

    @field_validator("s3_endpoint")
    @classmethod
    def _validate_s3_endpoint(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(
                f"MYTUBE_S3_ENDPOINT must start with http:// or https:// (got {value!r})"
            )
        return value

    @field_validator("s3_bucket")
    @classmethod
    def _validate_s3_bucket(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("MYTUBE_S3_BUCKET must not be empty.")
        return value

    @field_validator("worker_s3_key")
    @classmethod
    def _validate_worker_s3_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "MYTUBE_WORKER_S3_KEY must not be empty -- there is no fallback "
                "to MYTUBE_S3_KEY; the converter's storage credential is "
                "deliberately its own, narrower one."
            )
        return value

    @field_validator("worker_s3_secret")
    @classmethod
    def _validate_worker_s3_secret(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "MYTUBE_WORKER_S3_SECRET must not be empty -- there is no "
                "fallback to MYTUBE_S3_SECRET; the converter's storage "
                "credential is deliberately its own, narrower one."
            )
        return value
