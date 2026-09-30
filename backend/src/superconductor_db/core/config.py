"""Application settings, loaded from env / .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend settings. Env vars are read by exact field name (no prefix)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Postgres
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_user: str = Field(default="scdb")
    postgres_password: str = Field(default="scdb_dev_pw")
    postgres_db: str = Field(default="superconductor")
    postgres_dsn: str | None = Field(default=None)

    # MinIO
    minio_endpoint: str = Field(default="localhost:9000")
    minio_access_key: str = Field(default="scdb")
    minio_secret_key: str = Field(default="scdb_dev_pw")
    minio_bucket: str = Field(default="scdb-cache")

    # Auth
    jwt_secret: str = Field(default="change_me")
    jwt_expiry_hours: int = Field(default=24)
    allow_open_registration: bool = Field(default=False)

    # LLM (OpenAI-compatible)
    llm_api_key: str = Field(default="ollama")
    llm_base_url: str = Field(default="http://localhost:11434/v1")
    llm_model: str = Field(default="gemma4:e4b")

    # Caches (mounted volume in containers)
    pdf_cache_dir: str = Field(default="/data/cache/pdfs")
    markdown_cache_dir: str = Field(default="/data/cache/markdown")

    # Scansci-pdf subprocess binary
    scansci_pdf_bin: str = Field(default="scansci-pdf")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the lru_cache (used in tests when env changes)."""
    get_settings.cache_clear()