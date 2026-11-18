"""The extraction's list of papers the LLM refuses for good.

A paper whose text the provider's content filter rejects fails with a
422, and the failure leaves no row behind -- so the run's re-read finds
it new again and queues it for ever. One paper was attempted 1906 times
while the rest of the queue waited its turn.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from run_extraction import (  # noqa: E402
    _pending_dois,
    is_permanent_refusal,
    load_poisoned,
    save_poisoned,
)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.minimaxi.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.unit
def test_a_content_filter_refusal_is_permanent() -> None:
    """422 is the provider refusing the input, not the service failing."""
    assert is_permanent_refusal(_status_error(422)) is True


@pytest.mark.unit
def test_a_refusal_wrapped_in_a_runtime_error_is_still_permanent() -> None:
    """The client re-raises the status error as a RuntimeError."""
    try:
        try:
            raise _status_error(422)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"LLM call failed after 4 attempts: {exc}") from exc
    except RuntimeError as exc:
        assert is_permanent_refusal(exc) is True


@pytest.mark.unit
def test_a_rate_limit_is_not_permanent() -> None:
    """429 means the quota is full for now, not that the paper is bad."""
    assert is_permanent_refusal(_status_error(429)) is False


@pytest.mark.unit
def test_a_server_error_is_not_permanent() -> None:
    assert is_permanent_refusal(_status_error(503)) is False


@pytest.mark.unit
def test_an_unrelated_error_is_not_permanent() -> None:
    assert is_permanent_refusal(ConnectionResetError("boom")) is False


@pytest.mark.unit
def test_a_poisoned_paper_is_not_queued_again() -> None:
    queue = ["10.1/a", "10.1/b", "10.1/c"]
    done = {"10.1/a"}
    ready = set(queue)

    assert _pending_dois(queue, done, ready, set()) == ["10.1/b", "10.1/c"]
    assert _pending_dois(queue, done, ready, {"10.1/b"}) == ["10.1/c"]


@pytest.mark.unit
def test_the_poison_list_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "extract_poisoned.json"
    save_poisoned({"10.1/b": "422"}, path)
    assert load_poisoned(path) == {"10.1/b": "422"}


@pytest.mark.unit
def test_a_missing_poison_list_is_not_an_error(tmp_path: Path) -> None:
    assert load_poisoned(tmp_path / "absent.json") == {}
