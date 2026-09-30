"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from superconductor_db.core.config import reset_settings_cache


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Clear the lru_cached settings between tests so env overrides take effect."""
    reset_settings_cache()
    yield
    reset_settings_cache()