"""Typed settings loaded from the environment (MYTUBE_ prefix).

See src/.env.example for every field this class expects.
"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYTUBE_")

    secure_link_secret: str
    sign_ttl_seconds: int = 43200  # 12h, D-03
    database_url: str
    s3_endpoint: str
    s3_bucket: str = "mytube"
    s3_key: str
    s3_secret: str
    s3_region: str = "mytube"
    delete_grace_seconds: int = 300  # D-13
    blob_access_log: str = "/var/log/nginx/blob_access.log"

    # D-57: the address the copy button builds links against. Required, no
    # default -- the service refuses to start without it, the same way it
    # already refuses without secure_link_secret, because a bare hostname
    # here produces a copied link that silently does not work in a player.
    public_base_url: str
    # D-54: the read-only bind mount of the one CIFS/NFS share this phase browses.
    share_root: str = "/share"
    # D-52/D-53: ephemeral ffmpeg scratch space -- a plain container-layer
    # directory, wiped on recreate, never a named volume (see Dockerfile).
    work_dir: str = "/work"
    # D-51: the size-based time estimate shown next to the add button.
    add_estimate_mb_per_second: float = 20.0
    # D-67: 0 means "no quota enforced", used only for the usage display for now.
    storage_quota_bytes: int = 0

    @field_validator("public_base_url")
    @classmethod
    def _validate_public_base_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(
                "MYTUBE_PUBLIC_BASE_URL must start with http:// or https:// "
                f"(got {value!r}) -- a bare hostname produces a copied link "
                "that silently does not work in a player."
            )
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
