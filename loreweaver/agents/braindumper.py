"""Braindumper — turns a freeform brain-dump into canon lore + character sheets.

A human types (or pastes) unstructured notes about their story — fragments of
world, vibes, half-formed characters, plot scraps — and this agent extracts
structured canon from it: lore fields for the world bible and full character
sheets. It MERGES into any existing world bible for the series rather than
overwriting it, so a writer can keep dumping ideas over time and watch the bible
grow.

It is used two ways:
  * as a standalone call from the web UI  -> process_braindump(series_id, text)
  * (optionally) as a graph node          -> run(state)
"""
from __future__ import annotations

import json

from ..state import SeriesState
from ..store import continuity, files
from ..tools import llm
from ..tools.util import log

# Lore fields the model may fill / extend on the world bible.
_LORE_FIELDS = ("premise", "tone", "geography", "magic_system",
                "central_conflict", "visual_identity")


def _extract(text: str, bible: dict) -> dict:
    """Ask the model to mine the brain-dump for new/updated lore + characters."""
    known = sorted({(c.get("name") or "").strip()
                    for c in bible.get("characters", []) if c.get("name")})
    prompt = (
        "You are a story-bible keeper processing a writer's freeform BRAIN-DUMP of "
        "notes about their story. Extract canon from it.\n\n"
        "Return JSON with two keys:\n"
        '  "lore": an object that may contain any of these keys when the dump '
        "implies them: title, premise, tone, geography, magic_system, "
        "central_conflict, visual_identity, factions (array of {name, goal}). "
        "Only include a key when the dump gives you real material for it; for an "
        "EXISTING world, return refined/expanded text that stays consistent with "
        "the current bible.\n"
        '  "characters": array of character objects, each with: name, role, '
        "personality, physical_description, backstory, quirks, speaking_style, "
        "voice_brief (age, gender, timbre, accent for casting), and an RPG stat "
        "block: level (integer, start at 1 unless the dump implies a veteran), "
        "char_class (a short fantasy class fitting the role), stats (integer 3-18 "
        "for strength, wisdom, intelligence, dexterity, constitution, charisma, "
        "luck), and skills (array of 2-5 short skill names). Include BOTH "
        "brand-new characters and meaningful updates to known ones. Invent vivid, "
        "specific detail to fill gaps the writer left, but never contradict the "
        "existing bible.\n\n"
        "If the dump has nothing usable for a section, return an empty object/array "
        "for it.\n\n"
        f"KNOWN CHARACTERS: {known}\n"
        f"CURRENT WORLD BIBLE:\n{json.dumps(bible) if bible else '(none yet — this is a new world)'}\n\n"
        f"BRAIN-DUMP:\n{text}"
    )
    result = llm.generate_json(prompt)
    return result if isinstance(result, dict) else {}


def process_braindump(series_id: str, text: str, *, chapter: int | None = None) -> dict:
    """Mine `text` into lore + characters, merge into the series bible, persist.

    Returns {"lore_updated": [...fields...], "characters_added": [...names...],
             "characters_updated": [...names...], "world_bible": {...}}.
    """
    text = (text or "").strip()
    if not text:
        return {"lore_updated": [], "characters_added": [],
                "characters_updated": [], "world_bible": {}}

    continuity.init()
    existing = continuity.load_series(series_id)
    bible: dict = (existing or {}).get("world_bible") or {}
    bible.setdefault("characters", [])

    log("braindumper", f"processing {len(text)} chars of notes for '{series_id}'")
    extracted = _extract(text, bible)

    # ---- merge lore -------------------------------------------------------
    lore_in = extracted.get("lore") or {}
    lore_updated: list[str] = []
    if isinstance(lore_in, dict):
        if lore_in.get("title") and not bible.get("title"):
            bible["title"] = lore_in["title"]
            lore_updated.append("title")
        for field in _LORE_FIELDS:
            val = lore_in.get(field)
            if isinstance(val, str) and val.strip():
                bible[field] = val.strip()
                lore_updated.append(field)
        # factions: merge by name, append new ones
        new_factions = lore_in.get("factions")
        if isinstance(new_factions, list) and new_factions:
            roster = {f.get("name", "").strip().lower(): f
                      for f in bible.get("factions", []) if isinstance(f, dict)}
            for f in new_factions:
                if isinstance(f, dict) and f.get("name"):
                    roster[f["name"].strip().lower()] = f
            bible["factions"] = list(roster.values())
            lore_updated.append("factions")

    # ---- merge characters -------------------------------------------------
    roster = bible.setdefault("characters", [])
    by_name = {c.get("name", "").strip().lower(): c for c in roster if c.get("name")}
    added: list[str] = []
    updated: list[str] = []
    for ch in extracted.get("characters", []) or []:
        name = (ch.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in by_name:  # merge: fill blanks + overwrite with richer detail
            target = by_name[key]
            for k, v in ch.items():
                if isinstance(v, str) and v.strip():
                    target[k] = v.strip()
            files.save_character_sheet(series_id, target)
            updated.append(name)
        else:
            ch.setdefault("first_seen_chapter", chapter or 1)
            roster.append(ch)
            by_name[key] = ch
            files.save_character_sheet(series_id, ch)
            added.append(name)

    # ---- persist ----------------------------------------------------------
    files.save_lore(series_id, bible)
    continuity.save_series(
        series_id,
        title=bible.get("title", (existing or {}).get("title", series_id)),
        world_bible=bible,
        chapter_outline=(existing or {}).get("chapter_outline", []),
        voice_map=(existing or {}).get("voice_map", {}),
        rolling_summary=(existing or {}).get("rolling_summary", ""),
        current_chapter=(existing or {}).get("current_chapter", 0),
        cover_url=(existing or {}).get("cover_url", ""),
    )

    log("braindumper",
        f"lore+: {lore_updated or '—'}; characters +{added or '—'} ~{updated or '—'}")
    return {"lore_updated": lore_updated, "characters_added": added,
            "characters_updated": updated, "world_bible": bible}


def build_framework(series_id: str, *, count: int = 6) -> dict:
    """Generate a chapter FRAMEWORK (season outline) from the series' world bible
    and persist it onto the series row as `chapter_outline`.

    Returns {"chapter_outline": [...], "title": "...", "series": series_id}.
    Each outline entry is {index, title, beat, synopsis} — `index/title/beat` are
    what the Author consumes; `synopsis` is a one-line teaser for the UI.
    """
    count = max(1, min(int(count or 6), 24))
    continuity.init()
    existing = continuity.load_series(series_id)
    if not existing:
        return {"chapter_outline": [], "title": series_id, "series": series_id,
                "error": "no world bible yet — run a braindump first"}
    bible: dict = existing.get("world_bible") or {}

    log("braindumper", f"building {count}-chapter framework for '{series_id}'")
    prompt = (
        f"You are a story architect. Using this WORLD BIBLE, design a {count}-chapter "
        "season FRAMEWORK that builds a satisfying arc — rising tension, midpoint turn, "
        "and a climax/resolution. Each chapter must advance the central conflict and use "
        "the established characters and factions.\n\n"
        'Return JSON: {"chapters":[{"index":1,"title":"short evocative title",'
        '"beat":"the concrete events/turning point of this chapter (1-2 sentences)",'
        '"synopsis":"a one-line teaser"}, ...]}. '
        f"Produce exactly {count} chapters, indexed 1..{count}.\n\n"
        f"WORLD BIBLE:\n{json.dumps(bible)}"
    )
    raw = llm.generate_json(prompt)
    chapters = raw.get("chapters", []) if isinstance(raw, dict) else []

    outline: list[dict] = []
    for i in range(1, count + 1):
        src = next((c for c in chapters
                    if isinstance(c, dict) and c.get("index") == i), None)
        if src is None and len(chapters) >= i and isinstance(chapters[i - 1], dict):
            src = chapters[i - 1]
        src = src or {}
        outline.append({
            "index": i,
            "title": (src.get("title") or f"Chapter {i}").strip(),
            "beat": (src.get("beat") or "advance the central conflict").strip(),
            "synopsis": (src.get("synopsis") or "").strip(),
        })

    continuity.save_series(
        series_id,
        title=existing.get("title", bible.get("title", series_id)),
        world_bible=bible,
        chapter_outline=outline,
        voice_map=existing.get("voice_map", {}),
        rolling_summary=existing.get("rolling_summary", ""),
        current_chapter=existing.get("current_chapter", 0),
        cover_url=existing.get("cover_url", ""),
    )
    log("braindumper", f"framework saved: {len(outline)} chapters")
    return {"chapter_outline": outline,
            "title": existing.get("title", bible.get("title", series_id)),
            "series": series_id}


def run(state: SeriesState) -> dict:
    """Graph-node form: read a brain-dump off the state and fold it into canon."""
    text = state.get("braindump", "")  # type: ignore[arg-type]
    result = process_braindump(state["series_id"], text,
                               chapter=state.get("current_chapter", 1))
    return {"world_bible": result["world_bible"]}
