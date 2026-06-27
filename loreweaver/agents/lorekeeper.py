"""Lorekeeper — character-continuity agent (Gemini).

After a chapter is written and passes QA, this agent reads the prose and looks
for any NEW named characters that aren't already in the roster. For each, it
generates a full character sheet (physical description, backstory, quirks,
speaking style, voice brief), adds them to the world bible, and saves a sheet to
data/<story>/characters so casting can give them a distinct voice and later
chapters stay consistent.
"""
from __future__ import annotations

import json

from .. import rag
from ..state import SeriesState
from ..store import files
from ..tools import gemini
from ..tools.util import log


def run(state: SeriesState) -> dict:
    story = state["series_id"]
    chapter = state.get("current_chapter", 1)
    draft = state.get("chapter_draft", "")
    bible = state.get("world_bible") or {}
    roster = bible.setdefault("characters", [])
    known = {c.get("name", "").strip().lower() for c in roster}

    prompt = (
        "You are a story-bible keeper. Read this chapter and identify any NEW named characters "
        "that are NOT already in the known roster. For each genuinely new character, write a full "
        "character sheet. Return JSON: {\"characters\":[{\"name\",\"role\",\"personality\","
        "\"physical_description\",\"backstory\",\"quirks\",\"speaking_style\",\"voice_brief\"}]}. "
        "voice_brief must note age, gender, timbre and accent for casting. If there are no new "
        "characters, return {\"characters\":[]}.\n\n"
        f"KNOWN ROSTER: {sorted(known)}\n\nCHAPTER:\n{draft}"
    )
    result = gemini.generate_json(prompt)
    found = result.get("characters", []) if isinstance(result, dict) else []

    added = []
    new_characters = []
    for ch in found:
        name = (ch.get("name") or "").strip()
        if not name or name.lower() in known:
            continue
        ch["first_seen_chapter"] = chapter
        roster.append(ch)
        known.add(name.lower())
        files.save_character_sheet(story, ch)
        added.append(name)
        new_characters.append(ch)

    if added:
        # Add the new character sheets to the vector DB so future chapters can
        # retrieve them just like the founding cast.
        rag.index_characters(story, new_characters)
        log("lorekeeper", f"new character(s) introduced in ch{chapter}: {', '.join(added)}")
    else:
        log("lorekeeper", f"no new characters in ch{chapter}")
    return {"world_bible": bible}
