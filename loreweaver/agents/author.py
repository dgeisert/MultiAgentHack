"""Author — writing agent (Gemini).

Writes the current chapter from the World Bible, the chapter beat, and a rolling
summary of prior chapters (so continuity holds across weeks), then updates the
rolling summary for the next run.
"""
from __future__ import annotations

import json

from .. import settings
from ..state import SeriesState
from ..store import files
from ..tools import gemini
from ..tools.util import log


def run(state: SeriesState) -> dict:
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

    prompt = (
        f"Write chapter {chapter_no} ('{beat.get('title')}') of an audiobook. "
        f"Target {settings.CHAPTER_MIN_WORDS}-{settings.CHAPTER_MAX_WORDS} words. "
        "Write immersive prose with clear, attributable dialogue (use quotation marks and "
        "speaker tags). Honour the canon exactly — do not contradict the magic rules.\n\n"
        f"WORLD BIBLE:\n{json.dumps(bible)}\n\n"
        f"CHAPTER BEAT:\n{json.dumps(beat)}\n\n"
        f"STORY SO FAR:\n{state.get('rolling_summary','(this is the first chapter)')}"
        f"{revise_note}"
    )
    draft = gemini.generate_text(prompt)

    summary_prompt = (
        "In 2-3 sentences, update the rolling 'story so far' summary to include this chapter, "
        "preserving names and key facts for continuity.\n\n"
        f"PREVIOUS SUMMARY:\n{state.get('rolling_summary','')}\n\nNEW CHAPTER:\n{draft[:4000]}"
    )
    rolling = gemini.generate_text(summary_prompt)

    saved = files.save_text_revision(state["series_id"], chapter_no, draft)
    log("author", f"wrote chapter {chapter_no} ({len(draft.split())} words) -> {saved.name}")
    return {"chapter_draft": draft, "rolling_summary": rolling}
