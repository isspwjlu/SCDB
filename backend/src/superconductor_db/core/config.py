"""Application settings, loaded from env / .env file."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, field_validator
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
    jwt_secret: str = Field(default="")
    jwt_expiry_hours: int = Field(default=24)
    allow_open_registration: bool = Field(default=False)
    cookie_secure: bool = Field(default=False)

    # Placeholder-looking strings that must NEVER be used as a real jwt_secret.
    # The list is intentionally a tuple (frozen) so it cannot be mutated at runtime.
    _FORBIDDEN_JWT_SECRET_SUBSTRINGS: tuple[str, ...] = (
        "change_me",
        "test-secret-please-change",
        "placeholder",
        "please-change",
        "replace_me",
        "your-secret",
    )

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, v: str) -> str:
        """Reject weak / placeholder JWT secrets so the app refuses to boot.

        - Empty string is rejected.
        - Length must be >= 32 bytes.
        - Must not contain any well-known placeholder substring.
        - In production (DEBUG != "1") the canonical ``.env.example`` placeholder
          string is also rejected so a copy-pasted ``.env`` cannot ship.
        """
        if not v:
            raise ValueError(
                "JWT_SECRET must be set to a non-empty value "
                "(see .env.example for the placeholder)."
            )
        if len(v.encode("utf-8")) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 bytes long; "
                "use `python -c \"import secrets; print(secrets.token_urlsafe(48))\"` "
                "to generate one."
            )
        lowered = v.lower()
        for needle in cls._FORBIDDEN_JWT_SECRET_SUBSTRINGS:
            if needle in lowered:
                raise ValueError(
                    f"JWT_SECRET looks like a placeholder (contains '{needle}'). "
                    "Replace it with a real random secret before starting the app."
                )
        # Production-only extra check: the exact .env.example placeholder must be replaced.
        _PROD_PLACEHOLDER = (
            "change_me_to_random_64_chars_string_for_production_use_only"
        )
        debug_flag = os.environ.get("DEBUG", "1")
        if debug_flag == "0" and v == _PROD_PLACEHOLDER:
            raise ValueError(
                "JWT_SECRET is the .env.example placeholder; production "
                "(DEBUG=0) refuses to boot until it is replaced."
            )
        return v

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