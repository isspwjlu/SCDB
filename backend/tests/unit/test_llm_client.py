"""Unit tests for the LLM client (Task 25).

Tests use ``respx`` (already pulled in via httpx ecosystem) is not a hard
dependency, so we patch ``httpx.AsyncClient`` via ``unittest.mock`` for
deterministic test behaviour.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from superconductor_db.core.config import Settings
from superconductor_db.ingest.llm_client import (
    LLMClient,
    _fix_json_syntax,
    _recover_truncated_json,
    _strip_wrappers,
)


def _settings(**overrides) -> Settings:
    base = {
        "llm_api_key": "test-key",
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "test-model",
    }
    base.update(overrides)
    return Settings(**base)


def _mock_response(status: int, payload: dict | str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.unit
def test_strip_wrappers_drops_think_and_fences() -> None:
    raw = "<think>reasoning</think>```json\n[{\"a\":1}]\n```"
    out = _strip_wrappers(raw)
    assert out == '[{"a":1}]'


@pytest.mark.unit
def test_fix_json_syntax_python_literals() -> None:
    repaired = _fix_json_syntax("{'a': None, 'b': True, 'c': False,}")
    assert json.loads(repaired) == {"a": None, "b": True, "c": False}


@pytest.mark.unit
def test_fix_json_syntax_trailing_comma() -> None:
    assert json.loads(_fix_json_syntax('{"a": 1,}')) == {"a": 1}
    assert json.loads(_fix_json_syntax('[1, 2, 3,]')) == [1, 2, 3]


@pytest.mark.unit
def test_recover_truncated_json_closes_array() -> None:
    truncated = '[{"a":1},{"a":2'
    out = _recover_truncated_json(truncated)
    assert out is not None
    assert json.loads(out) == [{"a": 1}, {"a": 2}]


@pytest.mark.unit
def test_recover_truncated_json_closes_object() -> None:
    out = _recover_truncated_json('{"a": 1, "b": [2,')
    assert out is not None
    parsed = json.loads(out)
    assert parsed["a"] == 1
    assert parsed["b"] == [2]


@pytest.mark.unit
async def test_chat_returns_assistant_text() -> None:
    client = LLMClient(settings=_settings())
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "hello"}}]
    }
    response = _mock_response(200, payload)
    async_mock = AsyncMock()
    async_post = AsyncMock(return_value=response)
    async_mock.__aenter__.return_value.post = async_post
    with patch("httpx.AsyncClient", return_value=async_mock):
        text = await client.chat(
            [{"role": "user", "content": "hi"}], temperature=0.0, max_tokens=10
        )
    assert text == "hello"


@pytest.mark.unit
async def test_chat_retries_429_three_times() -> None:
    client = LLMClient(settings=_settings(), max_attempts=3, timeout=5.0)
    rate_limited = _mock_response(429, {"error": "rate"})
    ok = _mock_response(200, {"choices": [{"message": {"content": "ok"}}]})

    call_count = {"n": 0}

    async def fake_post(self, url, **kwargs):  # noqa: ANN001
        call_count["n"] += 1
        if call_count["n"] < 3:
            return rate_limited
        return ok

    # Patch sleep so the test is fast.
    with (
        patch("httpx.AsyncClient.post", new=fake_post),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        text = await client.chat([{"role": "user", "content": "hi"}])
    assert text == "ok"
    assert call_count["n"] == 3


@pytest.mark.unit
async def test_extract_json_recovers_truncation() -> None:
    client = LLMClient(settings=_settings())
    truncated = '[{"a": 1},{"a": 2'  # missing close
    payload = {
        "choices": [{"message": {"role": "assistant", "content": truncated}}]
    }
    response = _mock_response(200, payload)
    async_mock = AsyncMock()
    async_mock.__aenter__.return_value.post = AsyncMock(return_value=response)
    with patch("httpx.AsyncClient", return_value=async_mock):
        parsed = await client.extract_json([{"role": "user", "content": "x"}])
    assert parsed == [{"a": 1}, {"a": 2}]


@pytest.mark.unit
async def test_extract_json_strips_think_and_fences() -> None:
    client = LLMClient(settings=_settings())
    raw = "<think>thinking</think>```json\n[{\"a\":1}]\n```"
    payload = {"choices": [{"message": {"role": "assistant", "content": raw}}]}
    response = _mock_response(200, payload)
    async_mock = AsyncMock()
    async_mock.__aenter__.return_value.post = AsyncMock(return_value=response)
    with patch("httpx.AsyncClient", return_value=async_mock):
        parsed = await client.extract_json([{"role": "user", "content": "x"}])
    assert parsed == [{"a": 1}]