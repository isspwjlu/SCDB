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


class LLMClient:
    """Async OpenAI-compatible chat client with JSON extraction helpers."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = 120.0,
        max_attempts: int = 3,
    ) -> None:
        self.settings = settings or get_settings()
        self.timeout = timeout
        self.max_attempts = max_attempts
        # Normalise base_url: strip trailing slashes; ensure ``/chat/completions``
        # is appended at call time (some servers use ``/v1`` prefix already).
        self.base_url = self.settings.llm_base_url.rstrip("/")

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> str:
        """Call the LLM and return the assistant message text.

        Retries up to ``max_attempts`` times on connection errors and HTTP 429
        with exponential backoff (1s, 2s, 4s).
        """
        if not messages:
            raise ValueError("messages must be a non-empty list")
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.llm_api_key}",
        }
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"LLM transient status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                data = resp.json()
                text = _extract_assistant_text(data)
                if not text:
                    raise ValueError("LLM returned empty assistant message")
                return text
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                log.warning(
                    "LLM call attempt %d/%d failed: %s",
                    attempt,
                    self.max_attempts,
                    exc,
                )
                if attempt >= self.max_attempts:
                    break
                await asyncio.sleep(backoff)
                backoff *= 2.0
        raise RuntimeError(
            f"LLM call failed after {self.max_attempts} attempts: {last_exc}"
        )

    async def extract_json(self, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Call the LLM and return ``list[dict]`` parsed from the response.

        Applies the JSON-recovery chain (wrappers → syntax → truncation).
        """
        raw = await self.chat(messages)
        return self._parse_json_response(raw)

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
        raise ValueError(f"could not parse LLM JSON output: {raw[:200]}...")


def _extract_assistant_text(data: dict[str, Any]) -> str:
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
        if isinstance(content, str):
            return content
        # Some providers return ``content`` as a list of parts.
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            return "".join(parts)
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _coerce_to_list(obj: Any) -> list[dict[str, Any]]:
    """LLM extraction callers expect a list; accept dicts / scalars too."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        return [obj]
    return []