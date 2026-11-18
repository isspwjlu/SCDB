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

@pytest.mark.unit
async def test_extract_json_lifts_json_out_of_prose() -> None:
    """A model that prefixes its answer with prose must still be readable.

    A reasoning model states its plan before answering, so the JSON sits
    inside the text rather than replacing it.
    """
    client = LLMClient(settings=_settings())
    raw = 'We need answer strict JSON. Here it is: [{"a": 1}] Hope that helps.'
    payload = {"choices": [{"message": {"role": "assistant", "content": raw}}]}
    response = _mock_response(200, payload)
    async_mock = AsyncMock()
    async_mock.__aenter__.return_value.post = AsyncMock(return_value=response)
    with patch("httpx.AsyncClient", return_value=async_mock):
        parsed = await client.extract_json([{"role": "user", "content": "x"}])
    assert parsed == [{"a": 1}]


@pytest.mark.unit
async def test_extract_json_retries_when_the_answer_is_not_json() -> None:
    """A reasoning-only answer is retried rather than raised.

    When the token budget runs out mid-thought the model returns its
    reasoning and no answer. The second attempt usually returns the JSON.
    """
    client = LLMClient(settings=_settings())
    prose = _mock_response(
        200,
        {"choices": [{"message": {"role": "assistant", "content": "We need to think about it."}}]},
    )
    good = _mock_response(
        200,
        {"choices": [{"message": {"role": "assistant", "content": '[{"a": 1}]'}}]},
    )
    post = AsyncMock(side_effect=[prose, good])
    async_mock = AsyncMock()
    async_mock.__aenter__.return_value.post = post
    with patch("httpx.AsyncClient", return_value=async_mock):
        parsed = await client.extract_json([{"role": "user", "content": "x"}])
    assert parsed == [{"a": 1}]
    assert post.await_count == 2


@pytest.mark.unit
async def test_extract_json_raises_the_budget_when_the_answer_is_starved() -> None:
    """A reasoning model that ran out of budget must get a bigger budget.

    MiniMax-M3.1-Flash always reasons, and ``max_tokens`` covers the
    reasoning as well as the answer. When the thinking eats the whole
    budget the answer comes back empty, so the retry has to ask for more
    rather than repeat the same call.
    """
    client = LLMClient(settings=_settings(llm_max_tokens=1000))
    empty = _mock_response(200, {"choices": [{"message": {"content": "", "reasoning_content": "thinking..."}}]})
    good = _mock_response(200, {"choices": [{"message": {"content": '[{"a": 1}]'}}]})
    post = AsyncMock(side_effect=[empty, good])
    async_mock = AsyncMock()
    async_mock.__aenter__.return_value.post = post
    with patch("httpx.AsyncClient", return_value=async_mock):
        parsed = await client.extract_json([{"role": "user", "content": "x"}])
    assert parsed == [{"a": 1}]
    budgets = [c.kwargs["json"]["max_tokens"] for c in post.await_args_list]
    assert budgets[1] > budgets[0]


@pytest.mark.unit
async def test_extract_json_does_not_take_the_reasoning_as_the_answer() -> None:
    """Prose reasoning is not an answer, even when it contains a JSON array.

    The model's thinking block may hold a draft of the records. Treating it
    as the answer would store a draft the model never committed to, so the
    reasoning is skipped and the call retried instead.
    """
    client = LLMClient(settings=_settings())
    thinking_only = _mock_response(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": 'I should answer [{"draft": true}]',
                    }
                }
            ]
        },
    )
    good = _mock_response(200, {"choices": [{"message": {"content": '[{"a": 1}]'}}]})
    post = AsyncMock(side_effect=[thinking_only, good])
    async_mock = AsyncMock()
    async_mock.__aenter__.return_value.post = post
    with patch("httpx.AsyncClient", return_value=async_mock):
        parsed = await client.extract_json([{"role": "user", "content": "x"}])
    assert parsed == [{"a": 1}]


@pytest.mark.unit
async def test_rate_limit_parks_every_subsequent_call() -> None:
    """A 429 must slow the whole pipeline, not just the call that hit it.

    The account allows a sustained request rate the loop's concurrency
    exceeds, so a 429 means the quota window is full. Each worker discovering
    that independently keeps the quota saturated; one shared cooldown lets it
    refill.
    """
    import time

    client = object.__new__(LLMClient)
    client._parked_until = 0.0

    client._park(seconds=30.0)
    remaining = client._parked_until - time.monotonic()
    # The pause is jittered, so it lands within a band around the request.
    assert 20 < remaining <= 39

    # A later, shorter park must not shorten an existing cooldown.
    client._park(seconds=1.0)
    assert client._parked_until - time.monotonic() > 20


@pytest.mark.unit
def test_park_jitters_the_wait() -> None:
    """Waiting workers must not all wake at the same moment.

    They would walk straight back into the limit that just refused them.
    """
    import time

    waits = set()
    for _ in range(6):
        client = object.__new__(LLMClient)
        client._parked_until = 0.0
        client._park(seconds=10.0)
        waits.add(round(client._parked_until - time.monotonic(), 3))
    assert len(waits) > 1


@pytest.mark.unit
async def test_respect_park_returns_immediately_when_not_parked() -> None:
    """The common case must not add latency to every request."""
    import time

    client = object.__new__(LLMClient)
    client._parked_until = 0.0
    started = time.monotonic()
    await client._respect_park()
    assert time.monotonic() - started < 0.5


@pytest.mark.unit
async def test_chat_falls_back_to_reasoning_content() -> None:
    """Reasoning models may exhaust the budget in ``reasoning_content``.

    GLM on llama.cpp answers with an empty ``content`` and the thinking in
    ``reasoning_content`` when max_tokens runs out mid-reasoning. The call
    must surface that text instead of raising "empty assistant message" —
    the JSON-recovery chain downstream can still find the answer.
    """
    client = LLMClient(settings=_settings())
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "thinking ... [{'Tc': 80}]",
                }
            }
        ]
    }
    response = _mock_response(200, payload)
    async_mock = AsyncMock()
    async_post = AsyncMock(return_value=response)
    async_mock.__aenter__.return_value.post = async_post
    with patch("httpx.AsyncClient", return_value=async_mock):
        text = await client.chat(
            [{"role": "user", "content": "hi"}], temperature=0.0, max_tokens=10
        )
    assert "Tc" in text


@pytest.mark.unit
def test_concurrency_limit_caps_requests_in_flight() -> None:
    """MiniMax refuses a burst of parallel requests.

    The dimension prompts of one paper go out together, four to seven of
    them, and the endpoint answers that burst with "Token Plan 速率超限，
    请降低并发量" (HTTP 429, code 2056). The client therefore keeps only
    ``llm_max_concurrency`` requests in flight.
    """
    import asyncio

    in_flight = 0
    peak = 0

    async def fake_post(self, url, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _mock_response(
            200,
            {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    client = LLMClient(_settings(llm_max_concurrency=3))

    async def run():
        with patch.object(httpx.AsyncClient, "post", fake_post):
            await asyncio.gather(
                *(client.chat([{"role": "user", "content": "hi"}]) for _ in range(9))
            )

    asyncio.run(run())
    assert peak <= 3


@pytest.mark.unit
def test_default_settings_build_a_client(monkeypatch) -> None:
    """The client is normally built without arguments.

    ``get_settings()`` is the default, so the concurrency limit has to be
    read from the resolved settings rather than from the constructor
    argument, which is None in that case.
    """
    from superconductor_db.ingest import llm_client as module

    monkeypatch.setattr(module, "get_settings", lambda: _settings())
    client = LLMClient()
    assert client._concurrency._value == 1
