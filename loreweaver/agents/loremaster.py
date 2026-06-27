"""Loremaster — worldbuilding agent (Gemini).

Expands the chosen concept into a canon World Bible with fully fleshed
characters (physical description, backstory, quirks, speaking style, voice
brief) and a season-length chapter outline. Persists the lore to
data/<story>/lore and a character sheet per character to data/<story>/characters.
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
    concept = state["world_concept"]
    log("loremaster", "building world bible + season outline")

    bible_prompt = (
        "Build a canonical fantasy WORLD BIBLE from this concept. Return JSON with keys: "
        "title, premise, tone, geography, magic_system (with hard rules and costs), "
        "factions (array of {name, goal}), central_conflict, visual_identity "
        "(palette + motifs), and characters. Each character is an object with: name, role, "
        "personality, physical_description, backstory, quirks, speaking_style, and voice_brief "
        "(age, gender, timbre, accent for casting). Always include a 'Narrator' character. "
        "Make every character vivid and distinct.\n\n"
        f"CONCEPT:\n{json.dumps(concept)}"
    )
    bible = gemini.generate_json(bible_prompt)

    outline_prompt = (
        "Given this world bible, outline the first 6 chapters of a season. Return JSON: "
        '{"chapters":[{"index":1,"title":"...","beat":"what happens"}, ...]}.\n\n'
        f"WORLD BIBLE:\n{json.dumps(bible)}"
    )
    outline_raw = gemini.generate_json(outline_prompt)
    outline = outline_raw.get("chapters", []) if isinstance(outline_raw, dict) else []
    if not outline:  # robust default so the graph never stalls
        outline = [{"index": i, "title": f"Chapter {i}", "beat": "advance the central conflict"}
                   for i in range(1, 7)]

    # Persist lore + a character sheet per character.
    for ch in bible.get("characters", []):
        ch.setdefault("first_seen_chapter", 1)
        files.save_character_sheet(story, ch)
    files.save_lore(story, bible)

    # Build the vector index so the Author can retrieve relevant lore per chapter.
    rag.index_world_bible(story, bible, reset=True)

    log("loremaster", f"world '{bible.get('title','?')}': "
                      f"{len(bible.get('characters', []))} characters, {len(outline)} chapters; "
                      f"lore + sheets saved")
    return {"world_bible": bible, "chapter_outline": outline}
