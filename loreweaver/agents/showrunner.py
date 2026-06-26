"""Showrunner — orchestrator entry/exit nodes.

`enter` decides whether this run starts a new world or continues an existing
series, hydrating long-lived state from the continuity store. `finalize`
persists updated state and (optionally) schedules the next chapter.
"""
from __future__ import annotations

from ..state import SeriesState
from ..store import continuity
from ..tools.util import log


def enter(state: SeriesState) -> dict:
    continuity.init()
    sid = state["series_id"]
    existing = continuity.load_series(sid)

    # current_chapter always means "the chapter being produced this run", set
    # once here so QA revise-loops can re-run the Author without advancing it.
    if state.get("mode") == "new_series" or existing is None:
        log("showrunner", f"NEW SERIES '{sid}' — producing chapter 1")
        return {"mode": "new_series", "current_chapter": 1, "retries": {}, "qa_notes": []}

    next_chapter = existing["current_chapter"] + 1
    log("showrunner", f"CONTINUE '{sid}' — producing chapter {next_chapter}")
    return {
        "mode": "next_chapter",
        "world_bible": existing["world_bible"],
        "chapter_outline": existing["chapter_outline"],
        "voice_map": existing["voice_map"],
        "rolling_summary": existing["rolling_summary"],
        "current_chapter": next_chapter,
        "retries": {},
        "qa_notes": [],
    }


def finalize(state: SeriesState) -> dict:
    sid = state["series_id"]
    bible = state.get("world_bible") or {}
    chapter = state.get("current_chapter", 1)
    continuity.save_series(
        sid,
        title=bible.get("title", sid),
        world_bible=bible,
        chapter_outline=state.get("chapter_outline", []),
        voice_map=state.get("voice_map", {}),
        rolling_summary=state.get("rolling_summary", ""),
        current_chapter=chapter,
        cover_url=state.get("publish_results", {}).get("cover_url", ""),
    )
    log("showrunner", f"persisted state @ chapter {chapter}; ready to drip next run")
    return {}
