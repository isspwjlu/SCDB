"""Network-outage handling for the ingest pipeline.

The host is a laptop on a flaky link. A paper that fails because the machine
went offline must be retried, not discarded — the download itself was fine.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from superconductor_db.ingest.netresilience import (
    DEFAULT_MAX_BACKOFF_S,
    with_network_retry,
)


@pytest.mark.unit
async def test_transport_error_is_retried_until_it_succeeds() -> None:
    """A connect error is an outage, not a verdict on the paper."""
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("network is unreachable")
        return "pdf"

    result = await with_network_retry(
        flaky, description="test", initial_backoff_s=0.01, max_backoff_s=0.05
    )
    assert result == "pdf"
    assert calls["n"] == 3


@pytest.mark.unit
async def test_persistent_outage_gives_up_and_returns_none() -> None:
    """The deadline bounds the wait so a loop cannot hang forever."""
    async def always_down() -> str:
        raise httpx.ConnectError("down")

    result = await with_network_retry(
        always_down, description="test", deadline_s=0.05,
        initial_backoff_s=0.01, max_backoff_s=0.02,
    )
    assert result is None


@pytest.mark.unit
async def test_non_transport_errors_are_not_retried() -> None:
    """A bad request fails identically on retry; retrying it wastes the budget."""
    calls = {"n": 0}

    async def bad_request() -> str:
        calls["n"] += 1
        raise ValueError("prompt is malformed")

    with pytest.raises(ValueError):
        await with_network_retry(
            bad_request, description="test", deadline_s=1.0, initial_backoff_s=0.01
        )
    assert calls["n"] == 1


@pytest.mark.unit
def test_backoff_is_capped() -> None:
    """An hours-long outage must not push the next retry days out."""
    assert DEFAULT_MAX_BACKOFF_S == 60.0


@pytest.mark.unit
async def test_network_wait_can_be_forever() -> None:
    """``max_wait_s <= 0`` waits indefinitely, for unattended operation.

    A bounded wait meant an outage longer than the cap ended ingestion until a
    person noticed; the collector is expected to survive a night offline.
    """
    import inspect

    from superconductor_db.ingest import netresilience

    src = inspect.getsource(netresilience.wait_for_network)
    assert "max_wait_s <= 0" in src, "wait_for_network must support an open-ended wait"


@pytest.mark.unit
def test_loops_wait_for_network_without_a_deadline() -> None:
    """Both ingest loops must call wait_for_network with no cap."""
    import pathlib

    from superconductor_db.ingest import curated_loop, paper_search_loop

    root = pathlib.Path(curated_loop.__file__).parent
    for module in (curated_loop, paper_search_loop):
        text = (root / f"{module.__name__.rsplit('.', 1)[-1]}.py").read_text(
            encoding="utf-8"
        )
        assert "wait_for_network(max_wait_s=0.0)" in text, module.__name__
