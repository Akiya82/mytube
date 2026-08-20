"""Typed settings loaded from the environment (MYTUBE_ prefix).

See src/.env.example for every field this class expects.
"""
from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
