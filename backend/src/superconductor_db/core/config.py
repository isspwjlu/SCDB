"""Application settings, loaded from env / .env file."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

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

    # Unified database URL (DATABASE_URL). If set (e.g. sqlite:///./_dev.db
    # for local dev), overrides the postgres_* fields. Production leaves
    # this empty and uses the Postgres connection.
    database_url: str = Field(default="")

    # LLM (OpenAI-compatible)
    llm_api_key: str = Field(default="ollama")
    llm_base_url: str = Field(default="http://localhost:11434/v1")
    llm_model: str = Field(default="gemma4:e4b")
    # A paper's markdown runs to tens of thousands of characters, and the
    # model needs a while to answer over that much text. The old 120 s
    # default timed out on the longer papers.
    llm_timeout: float = Field(default=300.0)
    # Output budget per call, reasoning included. MiniMax-M3.1-Flash always
    # reasons -- ``thinking: disabled`` is rejected -- and it thinks for
    # 13-20 k tokens before it writes the records of a long paper, so the
    # budget has to cover the thinking as well or the answer comes back
    # empty.
    llm_max_tokens: int = Field(default=32768)
    # Reasoning effort for models that always think. "low" caps the thinking
    # at a few hundred tokens: the answer then fits in the budget instead of
    # being starved by it, and the output cost drops by about a third. Empty
    # means "do not send the parameter" and the model thinks as much as it
    # wants -- which on a long paper means the whole budget.
    llm_reasoning_effort: str = Field(default="low")
    # Some MiniMax models can be told not to think at all
    # (``thinking: {"type": "disabled"}``); others reject the parameter
    # outright. Empty means "do not send it".
    llm_thinking: str = Field(default="")
    # How many requests may be in flight at once. One: the LLM calls are
    # serial. The dimension prompts of a paper (four to seven of them) are
    # sent one after another, and MiniMax answers a parallel burst with
    # "Token Plan 速率超限，请降低并发量" (HTTP 429, code 2056) -- which used
    # to cost a 60-second global pause and, one paper in seven, the whole
    # paper. Serial trades wall clock for rows that actually land.
    llm_max_concurrency: int = Field(default=1)
    # OpenAlex: the contact address identifies the polite pool, and an API key
    # raises the daily request allowance by two orders of magnitude. Without a
    # key the free pool was exhausted inside a day of continuous ingestion.
    openalex_mailto: str = Field(default="")
    openalex_api_key: str = Field(default="")
    # Elsevier: the API key plus the institute's own address is enough to
    # get the full text -- no instoken. Without it the Elsevier papers were
    # unreachable: the PDF endpoint answers 403 with a Cloudflare challenge
    # page, and only two thirds of them have a preprint to fall back on.
    elsevier_api_key: str = Field(default="")

    # Caches (mounted volume in containers)
    pdf_cache_dir: str = Field(default="/data/cache/pdfs")
    markdown_cache_dir: str = Field(default="/data/cache/markdown")

    # The NIMS SuperCon dataset: five CSV files of measured transition
    # temperatures. It is static and small, so it goes into a database of
    # its own rather than into the corpus the extraction fills. The API
    # builds that database from the CSV files when it is missing.
    supercon_csv_dir: str = Field(
        default=str(Path(__file__).resolve().parents[4] / "SuperCon数据")
    )
    supercon_db_path: str = Field(
        default=str(Path(__file__).resolve().parents[3] / "_dev_supercon.db")
    )

    # Scansci-pdf subprocess binary
    scansci_pdf_bin: str = Field(default="scansci-pdf")

    # HTTP proxy for the PDF fetchers (HTTP_PROXY in .env or the
    # environment). The browser picks the machine's proxy up on its own;
    # httpx does not, so the fetchers need to be told.
    http_proxy: str = Field(default="")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the lru_cache (used in tests when env changes)."""
    get_settings.cache_clear()


def _windows_system_proxy() -> str:
    """The proxy the machine's browser uses, read from the WinINET settings.

    Chromium picks this up on its own; httpx does not. Following the same
    proxy keeps the two routes consistent -- the browser finds the PDF
    link through it and the replay has to fetch the file through it too,
    or the publisher answers with the challenge page instead.

    The explicit ``HTTP_PROXY`` setting wins over this, and an empty
    environment variable does not shadow it: the shell that starts the
    loop exports ``HTTP_PROXY=`` (empty), and a value that is present but
    blank would otherwise count as "configured".
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - not Windows
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    server = str(server).strip()
    if not server:
        return ""
    if "://" not in server:
        server = f"http://{server}"
    return server


def pdf_fetch_proxies() -> list[str | None]:
    """The routes a PDF fetch should try, in order.

    The proxy is not a nicety: it is the only route that works for Nature
    (a direct request dies with an SSL EOF) and it is eight times faster
    for the APS journals -- a Physical Review PDF takes 3 s through it and
    27 s direct. But it is not perfect either, some tunnels fail with
    ``ERR_TUNNEL_CONNECTION_FAILED``, so every fetch tries the proxy
    first and then goes direct. ``None`` means direct egress.
    """
    proxy = get_settings().http_proxy.strip() or _windows_system_proxy()
    if not proxy:
        return [None]
    return [proxy, None]