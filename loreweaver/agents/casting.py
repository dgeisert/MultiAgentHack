"""Casting Director — script parsing + voice mapping (Gemini + ElevenLabs).

Parses prose into an ordered performance script (speaker attribution + emotion
per line), then assigns each speaker an APPROPRIATE, UNIQUE premade ElevenLabs
voice (matched on gender/age/accent from the Loremaster's voice brief). No
custom voices are trained. The voice map is persisted so a character keeps the
same voice across every chapter, and so no two characters share a voice (until
the catalog is exhausted).
"""
from __future__ import annotations

import json
import re

from ..state import SeriesState
from ..tools import elevenlabs, gemini
from ..tools.util import log

# Heuristic synonyms so a free-text brief maps onto catalog labels. Matched on
# whole words (not substrings) so e.g. "woman" never triggers the "man" cue.
_MALE = {"man", "male", "masculine", "he", "boy", "tenor", "baritone", "bass", "gruff"}
_FEMALE = {"woman", "female", "feminine", "she", "girl", "alto", "soprano", "matron"}
_OLD = {"old", "elder", "elderly", "aged", "ancient", "weathered", "grizzled", "middle"}
_YOUNG = {"young", "youth", "youthful", "boyish", "girlish", "teen", "teenage", "child"}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (text or "").lower()))


def _score(brief: str, voice: dict, want_narrator: bool) -> int:
    toks = _tokens(brief)
    score = 0
    g = voice.get("gender", "")
    if g == "male" and ("male" in toks or toks & _MALE):
        score += 4
    if g == "female" and ("female" in toks or toks & _FEMALE):
        score += 4
    age = voice.get("age", "")
    if age:
        if "young" in age and toks & _YOUNG:
            score += 2
        if ("middle" in age or "old" in age) and toks & _OLD:
            score += 2
    accent = voice.get("accent", "").split("-")[0]
    if accent and accent in toks:
        score += 2
    desc_toks = _tokens(voice.get("description", ""))
    if want_narrator and (desc_toks & {"narration", "narrate", "story", "storytelling"}):
        score += 5
    # reward overlapping descriptive words (e.g. "calm", "soft", "deep")
    score += len({w for w in desc_toks if len(w) > 3} & toks)
    return score


def _assign_unique_voices(speakers, characters, voice_map, catalog) -> None:
    used = set(voice_map.values())
    for spk in speakers:
        if spk in voice_map:
            continue
        brief = characters.get(spk, {}).get("voice_brief", "") or ""
        want_narrator = spk.lower().startswith("narrator") or "narrat" in brief.lower()

        free = [v for v in catalog if v["voice_id"] not in used]
        pool = free or catalog  # only reuse once the catalog is exhausted
        best = max(pool, key=lambda v: _score(brief, v, want_narrator))

        voice_map[spk] = best["voice_id"]
        used.add(best["voice_id"])
        log("casting", f"cast {spk} -> {best['name']} "
                       f"({best.get('gender','?')}/{best.get('accent','?')})")


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

    # Distinct speakers in first-appearance order.
    speakers, seen = [], set()
    for ln in lines:
        s = ln.get("speaker", "Narrator")
        if s not in seen:
            seen.add(s)
            speakers.append(s)

    voice_map = dict(state.get("voice_map") or {})
    catalog = elevenlabs.list_voices()
    _assign_unique_voices(speakers, characters, voice_map, catalog)

    for ln in lines:
        ln["voice_id"] = voice_map.get(ln.get("speaker", "Narrator"))

    log("casting", f"{len(lines)} lines across {len(speakers)} unique voices")
    return {"performance_script": lines, "voice_map": voice_map}
