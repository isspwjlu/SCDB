"""LLM client (spec §6).

A thin OpenAI-compatible HTTP client over :mod:`httpx`. We deliberately do NOT
use the ``openai`` SDK so the same code works against Ollama, DeepSeek,
MiniMax, vLLM, and any other OpenAI-spec server.

Key behaviour (spec §6.3 + §11):

- ``async chat(messages, temperature=0.1, max_tokens=8192) -> str``
  POSTs to ``{base_url}/chat/completions`` and returns the assistant text.
- 3-attempt exponential backoff (1s / 2s / 4s) on connection errors and 429.
- 120s timeout per attempt.
- ``async extract_json(messages) -> list[dict]`` wraps :meth:`chat` and runs
  the JSON recovery chain:

    1. Strip ``<think>...</think>`` reasoning blocks (DeepSeek / Qwen style).
    2. Strip ``\`\`\`json ... \`\`\`` fences.
    3. :func:`_fix_json_syntax` -- Python literals → JSON, quote flipping,
       trailing-comma strip.
    4. :func:`_recover_truncated_json` -- when JSON.parse still fails, close
       any unmatched ``[``, ``{``, ``"`` at the end.
"""

from __future__ import annotations

import asyncio
import time
import random
import json
import logging
import re
from typing import Any

import httpx

from superconductor_db.core.config import Settings, get_settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# JSON recovery helpers                                                       #
# --------------------------------------------------------------------------- #


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

# Sentinels only Python uses; we coerce to their JSON form when we see them
# outside of a string literal.
_PYTHON_LITERAL_RE = re.compile(r"\b(None|True|False)\b")


def _strip_wrappers(text: str) -> str:
    """Drop ``<think>`` blocks and ``\`\`\`json\`\`\`` fences.

    Returns the first complete fenced block's contents if one is present;
    otherwise returns the text with think blocks removed.
    """
    text = _THINK_RE.sub("", text)
    # Unwrap the first complete fenced block (e.g. ```json\n[...]\n```).
    match = _CODE_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _fix_json_syntax(text: str) -> str:
    """Best-effort repair of LLM-emitted JSON.

    - ``None`` / ``True`` / ``False`` (outside string literals) → JSON tokens.
    - Single-quoted strings / dict keys → double-quoted.
    - Trailing commas removed.

    The function is intentionally conservative: it only touches characters
    outside single-quoted strings so we don't corrupt content like
    ``"O'Reilly"`` (rare in SC papers but possible).
    """
    out_chars: list[str] = []
    in_single = False
    in_double = False
    escape = False

    i = 0
    while i < len(text):
        ch = text[i]

        # --- inside a double-quoted string, just copy ---------------
        if in_double:
            out_chars.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            i += 1
            continue

        # --- inside a single-quoted string --------------------------
        if in_single:
            if escape:
                out_chars.append(ch)
                escape = False
                i += 1
                continue
            if ch == "\\":
                out_chars.append(ch)
                escape = True
                i += 1
                continue
            if ch == "'":
                # End of single-quoted run -- flip to double.
                out_chars.append('"')
                in_single = False
                i += 1
                continue
            # inside single quotes, double-quote must be escaped.
            if ch == '"':
                out_chars.append('\\"')
                i += 1
                continue
            out_chars.append(ch)
            i += 1
            continue

        # --- top-level ----------------------------------------------
        if ch == "'":
            # Begin a single-quoted string. Emit opening double quote.
            out_chars.append('"')
            in_single = True
            i += 1
            continue
        if ch == '"':
            out_chars.append(ch)
            in_double = True
            i += 1
            continue
        # Look for Python literal tokens at word boundaries.
        if ch.isalpha() and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            tail = text[i : i + 5]
            if tail.startswith("None"):
                out_chars.append("null")
                i += 4
                continue
            if tail.startswith("True"):
                out_chars.append("true")
                i += 4
                continue
            if tail.startswith("False"):
                out_chars.append("false")
                i += 5
                continue
        out_chars.append(ch)
        i += 1

    repaired = "".join(out_chars)

    # Strip trailing commas inside objects / arrays (e.g. ``[1,2,]``).
    repaired = re.sub(r",(\s*[\]\}])", r"\1", repaired)
    return repaired


def _recover_truncated_json(text: str) -> str | None:
    """Close any unmatched ``[``, ``{``, ``"`` at the end of ``text``.

    Returns the repaired text, or ``None`` if the unbalance can't be sensibly
    repaired (e.g. mid-string truncation with no closing quote).
    """
    # Strip trailing whitespace + trailing comma so the closer lines up.
    text = text.rstrip().rstrip(",")

    stack: list[str] = []
    in_string = False
    escape = False
    last_unterm_start: int | None = None  # position of last opened quote

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            last_unterm_start = i
            continue
        if ch in "[{":
            stack.append(ch)
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()

    if not stack and not in_string:
        return text  # balanced; nothing to do.

    # If we're inside a string at EOF, close it.
    suffix = ""
    if in_string:
        # Drop a trailing partial escape (e.g. ``"`` with no close).
        suffix += '"'

    # Close nested containers in reverse order.
    for opener in reversed(stack):
        suffix += "]" if opener == "[" else "}"
    return text + suffix


# --------------------------------------------------------------------------- #
# Client                                                                      #
# --------------------------------------------------------------------------- #


# A 429 is a quota signal, not a blip. The account allows a sustained rate
# that the loop's concurrency exceeds, so backoff has to be long enough for
# the window to refill — and shared, so every concurrent worker waits out the
# same cooldown instead of each discovering the limit independently.
_RATE_LIMIT_BACKOFF_S = 12.0
_RATE_LIMIT_PARK_S = 25.0
_MAX_BACKOFF_S = 60.0


class _EmptyAnswerError(ValueError):
    """The model returned no answer -- only its reasoning, if any.

    Separate from the other failures because retrying the same call does
    not help: a reasoning model that spent its whole budget thinking will
    do the same thing again. The caller raises the budget instead.
    """


class LLMClient:
    """Async OpenAI-compatible chat client with JSON extraction helpers."""
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float | None = None,
        max_attempts: int = 4,
    ) -> None:
        self.settings = settings or get_settings()
        self.timeout = timeout if timeout is not None else self.settings.llm_timeout
        self.max_attempts = max_attempts
        # Shared cooldown timestamp. Every call consults it before issuing a
        # request, so one rate-limit response slows the whole pipeline rather
        # than only the call that hit it.
        self._parked_until: float = 0.0
        # The dimension prompts of one paper go out together, four to seven
        # of them, and MiniMax rejects that burst with "Token Plan 速率超限,
        # 请降低并发量" (HTTP 429, code 2056). The requests queue here instead:
        # the paper is still extracted as a unit, but the endpoint sees a
        # request rate it accepts.
        self._concurrency = asyncio.Semaphore(
            max(1, self.settings.llm_max_concurrency)
        )
        # Token accounting for the calls this client made. The pipeline
        # logs it per paper: the difference between a reasoning budget that
        # fits and one that starves the answer is only visible in the
        # usage numbers.
        self.usage: dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
        }
        # Normalise base_url: strip trailing slashes; ensure ``/chat/completions``
        # is appended at call time (some servers use ``/v1`` prefix already).
        self.base_url = self.settings.llm_base_url.rstrip("/")

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        allow_reasoning_fallback: bool = True,
    ) -> str:
        """Call the LLM and return the assistant message text.

        Retries up to ``max_attempts`` times on connection errors and HTTP 429
        with exponential backoff (1s, 2s, 4s).

        ``allow_reasoning_fallback`` decides what happens when the model
        returns no answer but does return its thinking. A local llama.cpp
        build can put the answer inside the thinking block, so the fallback
        is on by default; a reasoning model that ran out of budget did not
        answer at all, and its prose has to be treated as a failure rather
        than as the answer.
        """
        if not messages:
            raise ValueError("messages must be a non-empty list")
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(
                max_tokens if max_tokens is not None else self.settings.llm_max_tokens
            ),
            "stream": False,
        }
        effort = (self.settings.llm_reasoning_effort or "").strip()
        if effort:
            # MiniMax's reasoning models accept this and honour it: the
            # thinking is capped, which is the difference between an answer
            # and an empty response on a long paper.
            payload["reasoning_effort"] = effort
        thinking = (self.settings.llm_thinking or "").strip()
        if thinking:
            # "disabled" turns the thinking off on the models that allow it.
            payload["thinking"] = {"type": thinking}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.llm_api_key}",
        }
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                # The slot is taken before the park is honoured, so that no
                # further request goes out while the endpoint is refusing.
                async with self._concurrency:
                    await self._respect_park()
                    # ``trust_env=False`` keeps this call off the machine's HTTP
                    # proxy. The host runs a local VPN client that exports
                    # HTTP_PROXY/ALL_PROXY, and it intermittently answers 502 for
                    # the LLM endpoint: three identical calls returned
                    # 200, 502, 200 through the proxy and 200, 200, 200 with it
                    # bypassed. Those 502s were the whole reason the pipeline
                    # stalled, and they looked exactly like the model being down.
                    async with httpx.AsyncClient(
                        timeout=self.timeout, trust_env=False
                    ) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"LLM transient status {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    resp.raise_for_status()
                    data = resp.json()
                    self._record_usage(data)
                    text = _extract_assistant_text(
                        data, allow_reasoning_fallback=allow_reasoning_fallback
                    )
                    if not text:
                        # Not a transport problem, so it is not retried here:
                        # repeating the same call with the same budget would
                        # starve the answer the same way. The caller raises the
                        # budget instead.
                        raise _EmptyAnswerError("LLM returned empty assistant message")
                    return text
            except _EmptyAnswerError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                rate_limited = isinstance(
                    exc, httpx.HTTPStatusError
                ) and exc.response.status_code == 429
                log.warning(
                    "LLM call attempt %d/%d failed: %s",
                    attempt,
                    self.max_attempts,
                    _describe_error(exc),
                )
                if attempt >= self.max_attempts:
                    # A 429 that survived every retry means the account quota
                    # is saturated, not that this call was unlucky. Parking
                    # every worker for a while is what lets the quota refill;
                    # retrying into it keeps it saturated.
                    if rate_limited:
                        self._park(seconds=_RATE_LIMIT_PARK_S)
                    break
                # Rate limiting needs a much longer pause than a transient
                # error, and jitter keeps concurrent workers from waking
                # together and re-triggering the limit.
                base = _RATE_LIMIT_BACKOFF_S if rate_limited else 1.0
                delay = min(base * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
                delay *= 0.7 + 0.6 * random.random()
                await asyncio.sleep(delay)
        raise RuntimeError(
            f"LLM call failed after {self.max_attempts} attempts: {last_exc}"
        )

    def _park(self, *, seconds: float) -> None:
        """Hold every subsequent call until ``seconds`` have passed.

        The pause is jittered: the requests that are waiting all wake at
        the same moment otherwise, and they walk straight back into the
        limit that just refused them.
        """
        wait = seconds * (0.7 + 0.6 * random.random())
        self._parked_until = max(self._parked_until, time.monotonic() + wait)
        log.warning(
            "rate limited — pausing all LLM calls for %.0fs", wait
        )

    def _record_usage(self, data: dict[str, Any]) -> None:
        """Accumulate the token usage the endpoint reported."""
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            self.usage["cached_tokens"] += int(details.get("cached_tokens") or 0)
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            self.usage["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0)

    async def _respect_park(self) -> None:
        """Sleep out an active cooldown, if one is in force."""
        remaining = self._parked_until - time.monotonic()
        while remaining > 0:
            await asyncio.sleep(min(remaining, 5.0))
            remaining = self._parked_until - time.monotonic()

    async def extract_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        attempts: int = 3,
    ) -> list[dict[str, Any]]:
        """Call the LLM and return ``list[dict]`` parsed from the response.

        Applies the JSON-recovery chain (wrappers → syntax → truncation →
        substring extraction).

        A reasoning model spends part of its token budget thinking before it
        answers, and ``max_tokens`` covers both. When the budget runs out
        mid-thought the answer comes back empty and only the reasoning
        survives -- prose, not JSON. So the retry raises the budget instead
        of repeating the same call: the thinking for a long paper runs to
        13-20 k tokens, so the first attempt asks for the configured
        budget and the next ones ask for half again and twice that.

        The reasoning block is never taken as the answer here. A local
        llama.cpp build can put the answer inside the thinking, but a
        reasoning model that ran out of budget did not answer at all.
        """
        base = max_tokens if max_tokens is not None else self.settings.llm_max_tokens
        budgets = [base, base * 3 // 2, base * 2]
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            budget = budgets[min(attempt, len(budgets)) - 1]
            try:
                raw = await self.chat(
                    messages,
                    max_tokens=budget,
                    allow_reasoning_fallback=False,
                )
            except Exception as exc:  # noqa: BLE001 - transport failures retry
                last_exc = exc
                log.warning(
                    "LLM call failed (attempt %d/%d, max_tokens=%d): %s",
                    attempt,
                    attempts,
                    budget,
                    _describe_error(exc),
                )
                continue
            try:
                return self._parse_json_response(raw)
            except ValueError as exc:
                last_exc = exc
                log.warning(
                    "LLM response was not JSON (attempt %d/%d, max_tokens=%d): %s",
                    attempt,
                    attempts,
                    budget,
                    str(exc)[:160],
                )
        raise last_exc if last_exc else ValueError("empty LLM response")

    @staticmethod
    def _parse_json_response(raw: str) -> list[dict[str, Any]]:
        """Apply the wrapper-stripping + repair chain to ``raw`` and parse."""
        stripped = _strip_wrappers(raw)
        # First pass: literal JSON.
        try:
            return _coerce_to_list(json.loads(stripped))
        except json.JSONDecodeError:
            pass
        # Second pass: Python-literal repair.
        repaired = _fix_json_syntax(stripped)
        try:
            return _coerce_to_list(json.loads(repaired))
        except json.JSONDecodeError:
            pass
        # Third pass: truncation recovery on the repaired text.
        truncated = _recover_truncated_json(repaired)
        if truncated is not None:
            try:
                return _coerce_to_list(json.loads(truncated))
            except json.JSONDecodeError:
                pass
        # Fourth pass: truncation recovery on the raw text (no repair).
        truncated_raw = _recover_truncated_json(stripped)
        if truncated_raw is not None:
            try:
                return _coerce_to_list(json.loads(truncated_raw))
            except json.JSONDecodeError:
                pass
        # Fifth pass: the answer is embedded in surrounding prose. A
        # reasoning model that states its plan before answering puts the
        # JSON inside the text, and so does any model that prefixes its
        # reply with "Here is the JSON:".
        embedded = _extract_json_substring(stripped)
        if embedded is not None:
            try:
                return _coerce_to_list(json.loads(embedded))
            except json.JSONDecodeError:
                pass
            repaired_embedded = _fix_json_syntax(embedded)
            try:
                return _coerce_to_list(json.loads(repaired_embedded))
            except json.JSONDecodeError:
                pass
            truncated_embedded = _recover_truncated_json(repaired_embedded)
            if truncated_embedded is not None:
                try:
                    return _coerce_to_list(json.loads(truncated_embedded))
                except json.JSONDecodeError:
                    pass
        raise ValueError(f"could not parse LLM JSON output: {raw[:200]}...")


def _extract_assistant_text(
    data: dict[str, Any], *, allow_reasoning_fallback: bool = True
) -> str:
    """Pull the assistant text from an OpenAI-compatible chat completion."""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message") or {}
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        # Some providers return ``content`` as a list of parts.
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            if parts:
                return "".join(parts)
        # Reasoning models (GLM / DeepSeek-R1 on llama.cpp, vLLM) spend the
        # token budget on ``reasoning_content`` first; a truncated response
        # can leave ``content`` empty while the thinking block still carries
        # the answer. Falling back keeps the JSON-recovery chain in the
        # caller working instead of failing the call outright.
        #
        # This is wrong for a reasoning model that simply ran out of budget:
        # its thinking is prose about the answer, not the answer. Callers
        # that need the answer itself turn the fallback off.
        reasoning = message.get("reasoning_content")
        if allow_reasoning_fallback and isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _coerce_to_list(obj: Any) -> list[dict[str, Any]]:
    """LLM extraction callers expect a list; accept dicts / scalars too."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        return [obj]
    return []


def _extract_json_substring(text: str) -> str | None:
    """Return the outermost JSON array or object embedded in ``text``.

    Used when the model wraps its answer in prose. The array bracket wins
    over the object bracket: the extractor asks for a list of records, and
    an array inside an object would be the wrong thing to lift out.
    """
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start == -1:
            continue
        end = text.rfind(closer)
        if end > start:
            return text[start : end + 1]
    return None


def _describe_error(exc: BaseException) -> str:
    """Describe an LLM call failure in a way that is worth reading.

    ``httpx.HTTPStatusError`` carries no message of its own, so a rejected
    request used to log as "LLM call attempt 1/3 failed: " and nothing
    else. The status code and the server's own error text are what
    actually explain the failure.
    """
    detail = str(exc).strip()
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None:
            detail = f"{detail or type(exc).__name__} (HTTP {status})"
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - the body may not be JSON
            body = None
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = f"{detail}: {error['message']}"
    return detail or type(exc).__name__