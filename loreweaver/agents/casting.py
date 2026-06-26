"""Casting Director — script parsing + voice mapping (Gemini + ElevenLabs).

Parses prose into an ordered performance script (speaker attribution + emotion
per line), then maps each speaker to an ElevenLabs voice — reusing existing
voices for known characters and designing new ones from the Loremaster's voice
brief. The voice map is persisted so a character sounds identical every chapter.
"""
from __future__ import annotations

import json

from ..state import SeriesState
from ..tools import elevenlabs, gemini
from ..tools.util import log


def run(state: SeriesState) -> dict:
    draft = state["chapter_draft"]
    bible = state.get("world_bible") or {}
    characters = {c["name"]: c for c in bible.get("characters", [])}

    log("casting", "parsing prose into a performance script")
    prompt = (
        "Convert this prose into an audiobook PERFORMANCE SCRIPT. Split into ordered lines. "
        "Attribute each line to a speaker ('Narrator' for narration, else the character name). "
        "Resolve 'he said'/'she said' attribution. Tag each line with a one-word emotion. "
        'Return JSON: {"lines":[{"idx":0,"speaker":"...","emotion":"...","text":"..."}]}.\n\n'
        f"KNOWN CHARACTERS: {list(characters)}\n\nPROSE:\n{draft}"
    )
    parsed = gemini.generate_json(prompt)
    lines = parsed.get("lines", []) if isinstance(parsed, dict) else []
    if not lines:  # fallback: narrate the whole thing
        lines = [{"idx": 0, "speaker": "Narrator", "emotion": "calm", "text": draft}]

    voice_map = dict(state.get("voice_map") or {})
    for line in lines:
        spk = line.get("speaker", "Narrator")
        if spk not in voice_map:
            brief = characters.get(spk, {}).get(
                "voice_brief", "neutral audiobook narrator, clear and warm")
            voice_map[spk] = elevenlabs.design_voice(spk, brief)
        line["voice_id"] = voice_map[spk]

    log("casting", f"{len(lines)} lines across {len(set(l['speaker'] for l in lines))} voices")
    return {"performance_script": lines, "voice_map": voice_map}
