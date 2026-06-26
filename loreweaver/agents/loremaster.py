"""Loremaster — worldbuilding agent (Gemini).

Expands the chosen concept into a canon World Bible and a season-length chapter
outline, including per-character voice briefs that the Casting Director uses to
design consistent voices.
"""
from __future__ import annotations

import json

from ..state import SeriesState
from ..tools import gemini
from ..tools.util import log


def run(state: SeriesState) -> dict:
    concept = state["world_concept"]
    log("loremaster", "building world bible + season outline")

    bible_prompt = (
        "Build a canonical fantasy WORLD BIBLE from this concept. Return JSON with keys: "
        "title, premise, tone, geography, magic_system (with hard rules and costs), "
        "factions (array of {name, goal}), characters (array of {name, role, personality, "
        "voice_brief}), central_conflict, visual_identity (palette + motifs). "
        "Always include a 'Narrator' character with a voice_brief.\n\n"
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

    log("loremaster", f"world '{bible.get('title','?')}' with {len(outline)} chapters planned")
    return {"world_bible": bible, "chapter_outline": outline}
