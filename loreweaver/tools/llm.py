"""Provider-agnostic LLM facade.

Agents call ``llm.generate_text`` / ``llm.generate_json`` and this module routes
to the backend selected by ``settings.LLM_PROVIDER`` (``gemini`` or ``claude``).
Both backends share the same signature and the same deterministic mock content,
so switching providers changes nothing about the graph's behaviour offline.

Image generation is Gemini-only (Claude has no image endpoint), so
``generate_image`` always delegates to ``tools.gemini``.
"""
from __future__ import annotations

from .. import settings
from . import gemini


def _backend():
    """Return the text-generation backend module for the active provider."""
    if settings.llm_provider() == "claude":
        from . import claude

        return claude
    return gemini


def generate_text(prompt: str, *, json_mode: bool = False, system: str | None = None) -> str:
    return _backend().generate_text(prompt, json_mode=json_mode, system=system)


def generate_json(prompt: str, *, system: str | None = None) -> object:
    return _backend().generate_json(prompt, system=system)


def generate_image(prompt: str, out_path: str) -> str:
    # Image generation always uses Gemini/Imagen regardless of LLM_PROVIDER.
    return gemini.generate_image(prompt, out_path)
