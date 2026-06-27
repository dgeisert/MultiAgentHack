"""Author — writing agent (Gemini).

Writes the current chapter using the FULL lore (the complete world bible and
every character sheet) plus the actual text of all previous chapters, so a new
chapter is grounded in everything that came before — not just a short summary.
"""
from __future__ import annotations

import json

from .. import rag, settings
from ..state import SeriesState
from ..store import files
from ..tools import gemini
from ..tools.util import log


def _character_digest(bible: dict) -> str:
    lines = []
    for c in bible.get("characters", []):
        lines.append(
            f"- {c.get('name','?')} ({c.get('role','')}): {c.get('personality','')}. "
            f"Looks: {c.get('physical_description','')}. Quirks: {c.get('quirks','')}. "
            f"Speaks: {c.get('speaking_style','')}."
        )
    return "\n".join(lines)


def _previous_chapters_block(story: str, chapter_no: int) -> str:
    prev = files.previous_chapters_text(story, chapter_no)
    if not prev:
        return "(this is the first chapter)"
    # Include earlier chapters in full so continuity is exact. Older chapters are
    # truncated only if extremely long, to keep the request within model limits.
    parts = []
    for c, text in prev:
        if len(text) > 8000 and c < chapter_no - 1:
            text = text[:8000] + "\n[... earlier passage truncated ...]"
        parts.append(f"--- CHAPTER {c} ---\n{text}")
    return "\n\n".join(parts)


def run(state: SeriesState) -> dict:
    story = state["series_id"]
    bible = state["world_bible"]
    outline = state.get("chapter_outline", [])
    # current_chapter is fixed by the Showrunner; do NOT increment here, or a QA
    # revise-loop would advance the chapter number on every retry.
    chapter_no = state.get("current_chapter", 1)
    beat = next((c for c in outline if c.get("index") == chapter_no), None) or {
        "index": chapter_no, "title": f"Chapter {chapter_no}", "beat": "advance the conflict"}

    revise_note = ""
    if state.get("qa_verdict") == "revise" and state.get("qa_target") == "author":
        revise_note = "\n\nREVISION NOTES (address these):\n" + "\n".join(state.get("qa_notes", []))

    # RAG: pull the characters + lore most relevant to this chapter beat from the
    # vector DB and surface them up top, while still passing the full canon below.
    retrieved = rag.retrieve_block(
        story, beat, state.get("rolling_summary", ""), extra=revise_note)
    retrieved_block = f"{retrieved}\n\n" if retrieved else ""
    if retrieved:
        log("author", f"RAG: injected relevant lore for ch{chapter_no}")

    prompt = (
        f"Write chapter {chapter_no} ('{beat.get('title')}') of an audiobook. "
        f"Target {settings.CHAPTER_MIN_WORDS}-{settings.CHAPTER_MAX_WORDS} words. "
        "Write immersive prose with clear, attributable dialogue (use quotation marks and "
        "speaker tags). Keep each character's established voice and speaking style. "
        "Honour the canon exactly — do not contradict the magic rules or prior events.\n\n"
        f"{retrieved_block}"
        f"WORLD BIBLE (full lore):\n{json.dumps(bible)}\n\n"
        f"CHARACTERS:\n{_character_digest(bible)}\n\n"
        f"CHAPTER BEAT:\n{json.dumps(beat)}\n\n"
        f"ROLLING SUMMARY:\n{state.get('rolling_summary','(none yet)')}\n\n"
        f"PREVIOUS CHAPTERS (full text):\n{_previous_chapters_block(story, chapter_no)}"
        f"{revise_note}"
    )
    draft = gemini.generate_text(prompt)

    summary_prompt = (
        "In 2-3 sentences, update the rolling 'story so far' summary to include this chapter, "
        "preserving names and key facts for continuity.\n\n"
        f"PREVIOUS SUMMARY:\n{state.get('rolling_summary','')}\n\nNEW CHAPTER:\n{draft[:4000]}"
    )
    rolling = gemini.generate_text(summary_prompt)

    saved = files.save_text_revision(story, chapter_no, draft)
    log("author", f"wrote chapter {chapter_no} ({len(draft.split())} words) -> {saved.name}")
    return {"chapter_draft": draft, "rolling_summary": rolling}
