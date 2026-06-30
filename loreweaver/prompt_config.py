"""Editable configuration for the main writing (Author) prompt.

The chapter-writing prompt is assembled from a handful of named TEXT SECTIONS.
Each section is plain text that may embed ``{placeholder}`` tokens; at generation
time the tokens are substituted with data loaded from elsewhere (character
states, plot points, the world bible, etc.). The static text of each section is
editable from the Settings page and persisted to disk as overrides on top of the
defaults defined here.

Two helpers back the Settings UI:
  * ``get_sections()``  — the effective sections (defaults + saved overrides).
  * ``save_sections()`` — persist edited section text.
And one helper backs generation:
  * ``render_writing_prompt(values)`` — assemble the final prompt, substituting
    the ``{placeholder}`` tokens from ``values``.
"""
from __future__ import annotations

import json
import re

from . import settings

# --------------------------------------------------------------------------- #
# Placeholders: the {tokens} filled in from elsewhere at generation time.
# Shown (with these descriptions) on the Settings page so an editor knows what
# each curly-bracket section loads.
# --------------------------------------------------------------------------- #
PLACEHOLDERS: dict[str, str] = {
    "chapter": "The chapter number being written.",
    "title": "The chapter's title, from the outline beat.",
    "min_words": "Minimum target word count (from settings).",
    "max_words": "Maximum target word count (from settings).",
    "states_block": "Each selected character's current state at the start of the chapter.",
    "plot_block": "The chapter's plot points, one per line — every one must happen, in order.",
    "notes_block": "Optional special notes for the chapter (blank when none are set).",
    "retrieved_block": "Relevant lore passages retrieved for this chapter (blank when none).",
    "world_bible": "The world bible lore as JSON, excluding the base character roster.",
    "char_block": "The Studio character section for THIS chapter (the per-chapter character records).",
    "rolling_summary": "The running 'story so far' summary of the prior chapters.",
    "prev_block": "The full text of the previous chapter (or a note if this is the first).",
}

# --------------------------------------------------------------------------- #
# Default text sections, in assembly order. Joined with a blank line between
# each to build the final prompt.
# --------------------------------------------------------------------------- #
_DEFAULT_SECTIONS: list[dict] = [
    {
        "key": "instructions",
        "label": "Instructions",
        "description": "Opening directive: what to write, the target length, the prose "
                       "style, and the demand to honour canon.",
        "text": (
            "Write chapter {chapter} ('{title}') of an audiobook. "
            "Target {min_words}-{max_words} words. "
            "Write immersive prose with clear, attributable dialogue (use quotation marks "
            "and speaker tags). Keep each character's established voice. Honour the canon "
            "exactly."
        ),
    },
    {
        "key": "punctuation",
        "label": "Punctuation rules",
        "description": "Hard punctuation constraints applied to the generated prose.",
        "text": (
            "PUNCTUATION: do not use em dashes (—) or en dashes (–) anywhere. Use commas, "
            "periods, colons, or parentheses instead, and a hyphen only inside hyphenated "
            "words."
        ),
    },
    {
        "key": "priority",
        "label": "Highest-priority requirements",
        "description": "The must-deliver block: the character states and plot points the "
                       "chapter is required to hit, plus any special notes.",
        "text": (
            "==== HIGHEST PRIORITY — THE CHAPTER MUST DELIVER THESE ====\n"
            "CURRENT CHARACTER STATES (start of chapter):\n{states_block}\n\n"
            "PLOT POINTS (every one must happen, in order):\n{plot_block}\n"
            "{notes_block}"
            "=========================================================="
        ),
    },
    {
        "key": "context",
        "label": "Lore & story context",
        "description": "Reference material the writer draws on: retrieved lore, the world "
                       "bible, this chapter's characters, the story so far, and the previous "
                       "chapter's full text.",
        "text": (
            "{retrieved_block}"
            "WORLD BIBLE (lore — characters are listed separately below):\n{world_bible}\n\n"
            "CHARACTERS IN THIS CHAPTER:\n{char_block}\n\n"
            "STORY SO FAR:\n{rolling_summary}\n\n"
            "PREVIOUS CHAPTER (full text):\n{prev_block}"
        ),
    },
]

_SECTION_SEPARATOR = "\n\n"
_VALID_KEYS = {s["key"] for s in _DEFAULT_SECTIONS}
_TOKEN = re.compile(r"\{(\w+)\}")


def _settings_path():
    return settings.DATA_DIR / "prompt_settings.json"


def _load_overrides() -> dict:
    """Read saved section overrides ({key: text}) for the writing prompt."""
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    writing = data.get("writing") if isinstance(data, dict) else None
    return writing if isinstance(writing, dict) else {}


def _save_overrides(overrides: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"writing": overrides}, indent=2), encoding="utf-8")


def get_sections() -> list[dict]:
    """The effective writing-prompt sections: defaults with any saved text
    overrides applied. Each entry carries key/label/description/text plus a
    `customized` flag so the UI can show what's been edited."""
    overrides = _load_overrides()
    out = []
    for sec in _DEFAULT_SECTIONS:
        text = overrides.get(sec["key"], sec["text"])
        out.append({
            "key": sec["key"],
            "label": sec["label"],
            "description": sec["description"],
            "text": text,
            "default": sec["text"],
            "customized": sec["key"] in overrides and overrides[sec["key"]] != sec["text"],
        })
    return out


def placeholders() -> dict[str, str]:
    """The {placeholder} legend: token name -> description."""
    return dict(PLACEHOLDERS)


def save_sections(updates: dict) -> list[dict]:
    """Persist edited section text. `updates` is {section_key: text}. A value
    equal to the built-in default clears the override (so the section tracks the
    default again). Unknown keys are ignored. Returns the refreshed sections."""
    overrides = _load_overrides()
    defaults = {s["key"]: s["text"] for s in _DEFAULT_SECTIONS}
    for key, text in (updates or {}).items():
        if key not in _VALID_KEYS or not isinstance(text, str):
            continue
        if text == defaults[key]:
            overrides.pop(key, None)   # back to default — drop the override
        else:
            overrides[key] = text
    _save_overrides(overrides)
    return get_sections()


def render_writing_prompt(values: dict) -> str:
    """Assemble the final writing prompt from the effective sections, replacing
    each known ``{placeholder}`` with its value. Unknown tokens are left intact,
    and substituted values are NOT re-scanned (so JSON braces in, e.g., the world
    bible can't trigger further substitution)."""
    def sub(text: str) -> str:
        return _TOKEN.sub(
            lambda m: str(values.get(m.group(1), m.group(0))), text)

    return _SECTION_SEPARATOR.join(sub(s["text"]) for s in get_sections())


def assembled_template() -> str:
    """The full prompt structure with placeholders left intact — i.e. exactly how
    the prompt is constructed, for display on the Settings page."""
    return _SECTION_SEPARATOR.join(s["text"] for s in get_sections())
