"""Typed settings loaded from the environment (MYTUBE_ prefix).

See src/.env.example for every field this class expects.
"""
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
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
    # D-77: the hostname the interface answers on. Required, no default --
    # redirects and the session cookie are built against a name the
    # application knows rather than one a caller supplies, and the service
    # refuses to start without it for the same reason it already refuses
    # without a public base address.
    admin_fqdn: str
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

    @field_validator("admin_fqdn")
    @classmethod
    def _validate_admin_fqdn(cls, value: str) -> str:
        value = value.strip().rstrip(".").lower()
        if not value:
            raise ValueError("MYTUBE_ADMIN_FQDN must not be empty.")
        if "://" in value or "/" in value:
            raise ValueError(
                f"MYTUBE_ADMIN_FQDN must be a bare hostname, not a URL "
                f"(got {value!r}) -- a scheme or path here means somebody "
                "pasted a URL where a hostname belongs."
            )
        if ":" in value:
            raise ValueError(
                f"MYTUBE_ADMIN_FQDN must not carry a port (got {value!r}) -- "
                "the port is stripped from the incoming X-Forwarded-Host "
                "header before comparison, so carrying one here would only "
                "ever produce a mismatch."
            )
        return value

    @model_validator(mode="after")
    def _admin_fqdn_must_differ_from_public_base_url(self) -> "Settings":
        # D-77's fourth case: the two hostnames must not be the same name.
        # nginx picks a server block by matching server_name exactly before
        # it falls back to the catch-all, regardless of the order the
        # blocks appear in the file. Two blocks claiming one name means the
        # exact match wins for every request on that name -- including
        # /blob/, which the interface block does not serve at all. Every
        # viewer in VRChat would then receive a redirect to a login form
        # their player cannot render, produced by a typo in an environment
        # file. Refusing to start with a self-defeating value costs one
        # comparison and turns a silent outage into a startup message.
        public_host = (urlsplit(self.public_base_url).hostname or "").lower()
        if self.admin_fqdn == public_host:
            raise ValueError(
                f"MYTUBE_ADMIN_FQDN ({self.admin_fqdn!r}) must not equal the "
                f"host of MYTUBE_PUBLIC_BASE_URL ({self.public_base_url!r}) "
                "-- the two must be different names, or nginx will route "
                "every request for that name into whichever server block "
                "wins the exact match, breaking the other one silently."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
