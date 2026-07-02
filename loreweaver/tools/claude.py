"""Claude (Anthropic) text wrapper: text generation with optional JSON mode.

Mirrors the public contract of ``tools.gemini`` for text/JSON so the two are
drop-in interchangeable behind ``tools.llm``. The live path talks to the
Anthropic Messages API directly over HTTP via ``httpx`` (already a transitive
dependency of the stack), so no extra SDK needs to be installed. The mock path
reuses gemini's deterministic stub content so offline runs are identical
regardless of the selected provider.

Claude has no image endpoint, so image generation stays in ``tools.gemini``.
"""
from __future__ import annotations

import json as _json
import os

from .. import settings
from . import gemini  # reuse the deterministic mock content
from .util import log, retry

# Anthropic Messages API endpoint + required version header.
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
# No client-side timeout: a full chapter generation can run arbitrarily long and
# must never be cut off. Streaming keeps the connection alive regardless.
_TIMEOUT = None
# Output cap. The Anthropic API *requires* a max_tokens value, so it cannot be
# omitted entirely, but we set it high enough that a chapter is never truncated.
# Overridable via env for models with different output ceilings.
_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "32000"))


# Retry transient connection drops with exponential backoff.
@retry(times=4, base_delay=2.0)
def _live_text(prompt: str, json_mode: bool, system: str | None) -> str:
    import httpx

    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    # In JSON mode, steer Claude to emit raw JSON only. generate_json() below
    # is already tolerant of code fences, so this is belt-and-suspenders.
    sys_parts: list[str] = []
    if system:
        sys_parts.append(system)
    if json_mode:
        sys_parts.append(
            "Respond with a single valid JSON value and nothing else. "
            "Do not wrap it in Markdown code fences or add commentary."
        )
    system_prompt = "\n\n".join(sys_parts) if sys_parts else None

    body: dict = {
        "model": settings.claude_text_model(),
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
        # STREAM the response: large generations can otherwise hold one idle
        # connection open long enough for an intermediary to close it. Streaming
        # keeps bytes flowing; we reassemble the text into a single string.
        "stream": True,
    }
    if system_prompt:
        body["system"] = system_prompt

    headers = {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": _API_VERSION,
        "content-type": "application/json",
    }

    parts: list[str] = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        with client.stream("POST", _API_URL, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                # Body must be read before it can be inspected on a streamed response.
                detail = resp.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {resp.status_code}: {detail[:500]}")
            # Server-Sent Events: each event carries a `data: {json}` line.
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    evt = _json.loads(data)
                except ValueError:
                    continue
                # Text arrives as content_block_delta events with a text_delta.
                if evt.get("type") == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        parts.append(delta.get("text", ""))
    return "".join(parts)


def generate_text(prompt: str, *, json_mode: bool = False, system: str | None = None) -> str:
    if settings.mock_mode():
        return gemini._mock_text(prompt, json_mode)
    log("claude", f"generate_text (json={json_mode}, {len(prompt)} chars)")
    try:
        return _live_text(prompt, json_mode, system)
    except Exception as e:  # noqa: BLE001
        if settings.fallback_mock():
            log("claude", f"live failed ({type(e).__name__}); falling back to mock content")
            return gemini._mock_text(prompt, json_mode)
        raise RuntimeError(
            f"Claude call failed after retries ({type(e).__name__}: {e}). "
            f"Check the selected Claude model ('{settings.claude_text_model()}') on the "
            "Prompt Settings page is valid and that ANTHROPIC_API_KEY is set in .env "
            "(usually starts with 'sk-ant-'). Set LOREWEAVER_MOCK=1 to run fully "
            "offline, or LOREWEAVER_FALLBACK_MOCK=1 to auto-degrade to mock on live errors."
        ) from e


def generate_json(prompt: str, *, system: str | None = None) -> object:
    raw = generate_text(prompt, json_mode=True, system=system)
    try:
        return _json.loads(raw)
    except Exception:  # tolerate code-fenced JSON from the live model
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        return _json.loads(raw)
