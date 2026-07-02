"""Chapter workspace store — per-chapter state for the Studio's manual flow.

The Studio walks a writer through a chapter one reviewable step at a time:
select characters -> generate/edit current states -> generate/edit plot points
-> author -> QA -> cast voices -> edit per-line speakers -> render TTS ->
review/regenerate clips -> combine -> cover -> publish.

All of that intermediate, human-editable state lives here, keyed by
(series_id, chapter). It is persisted to the SAME SQLite file as the continuity
store AND mirrored to a readable data/<series>/chNN/workspace.json so it follows
the existing "DB + files" pattern and survives restarts.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from .. import settings
from . import files


@contextmanager
def _conn():
    settings.ensure_dirs()
    con = sqlite3.connect(settings.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS chapter_workspace (
                series_id TEXT,
                chapter   INTEGER,
                data      TEXT,
                updated   REAL,
                PRIMARY KEY (series_id, chapter)
            )
            """
        )


# The empty shape of a chapter workspace. Every stage writes its own slice.
def _blank(series_id: str, chapter: int) -> dict:
    return {
        "series_id": series_id,
        "chapter": chapter,
        "selected_characters": [],   # names chosen to appear in this chapter
        "character_states": {},      # name -> current-state description (editable; kept
                                     # in sync with character_records[name]["state"])
        "character_records": {},     # name -> unified per-chapter character record:
                                     # frozen base descriptions (captured at chapter
                                     # creation) + stats/skills/level/class + state.
                                     # Mirrored to data/<series>/chNN/characters.json.
        "plot_points": [],           # ordered bullet strings (editable)
        "included_prev_chapters": None,  # chapter numbers whose full text to feed the
                                     # author prompt. None = default (immediate previous
                                     # chapter only). [] = include none.
        "special_notes": "",         # free-form notes injected into the author prompt (editable)
        "draft": "",                 # authored chapter text (editable)
        "qa": None,                  # {"verdict","notes"} from the QA step
        "script": [],                # [{idx,speaker,emotion,text,voice_id,clip}]
        "combined_url": "",          # /assets/... once clips are stitched
        "cover_url": "",             # /assets/... once a cover is made
        "published": None,           # publish_results once published
        "stage": "characters",       # furthest step reached (for UI hinting)
    }


def load(series_id: str, chapter: int) -> dict:
    """Return the workspace for a chapter, creating a blank one if absent."""
    init()
    with _conn() as con:
        row = con.execute(
            "SELECT data FROM chapter_workspace WHERE series_id=? AND chapter=?",
            (series_id, chapter),
        ).fetchone()
    if not row:
        return _blank(series_id, chapter)
    try:
        data = json.loads(row["data"] or "{}")
    except ValueError:
        data = {}
    # Merge onto the blank so newly added keys always exist for older rows.
    merged = _blank(series_id, chapter)
    merged.update(data)
    merged["series_id"] = series_id
    merged["chapter"] = chapter
    return merged


def save(series_id: str, chapter: int, data: dict) -> dict:
    """Persist a workspace to SQLite and mirror it to disk. Returns it back."""
    init()
    data = dict(data)
    data["series_id"] = series_id
    data["chapter"] = chapter
    blob = json.dumps(data)
    with _conn() as con:
        con.execute(
            """INSERT INTO chapter_workspace (series_id, chapter, data, updated)
               VALUES (?,?,?,?)
               ON CONFLICT(series_id, chapter) DO UPDATE SET
                   data=excluded.data, updated=excluded.updated""",
            (series_id, chapter, blob, time.time()),
        )
    # Mirror a readable copy next to the chapter's text/audio.
    try:
        path = files.chapter_dir(series_id, chapter) / "workspace.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return data


def update(series_id: str, chapter: int, **fields) -> dict:
    """Load, shallow-merge `fields`, and save. Convenience for step handlers."""
    ws = load(series_id, chapter)
    ws.update(fields)
    return save(series_id, chapter, ws)


def all_chapters(series_id: str) -> list[int]:
    """Every chapter number that has a saved workspace for this series, ascending.
    Used by cross-chapter operations like renaming a character."""
    init()
    with _conn() as con:
        rows = con.execute(
            "SELECT chapter FROM chapter_workspace WHERE series_id=? ORDER BY chapter ASC",
            (series_id,),
        ).fetchall()
    return [int(r["chapter"]) for r in rows]


def delete(series_id: str, chapter: int) -> None:
    init()
    with _conn() as con:
        con.execute(
            "DELETE FROM chapter_workspace WHERE series_id=? AND chapter=?",
            (series_id, chapter),
        )


def shift_down_after(series_id: str, removed_chapter: int) -> None:
    """After a planned chapter is removed from the outline, move every later
    chapter's workspace down by one so in-progress Studio state (draft, plot,
    character records, …) stays attached to its now-renumbered beat.

    Processes chapters in ascending order so each moves into the slot the previous
    move just vacated. The removed chapter's own workspace is overwritten by the
    first shift (or left for the caller to delete when nothing follows it).
    """
    init()
    with _conn() as con:
        rows = con.execute(
            "SELECT chapter FROM chapter_workspace "
            "WHERE series_id=? AND chapter>? ORDER BY chapter ASC",
            (series_id, removed_chapter),
        ).fetchall()
    for r in rows:
        c = int(r["chapter"])
        ws = load(series_id, c)
        delete(series_id, c)
        ws["chapter"] = c - 1
        save(series_id, c - 1, ws)


def find_previous_state(series_id: str, chapter: int, character: str) -> tuple[int, str] | None:
    """Most recent prior chapter (< `chapter`) that recorded a current-state
    description for `character`. Returns (chapter_no, state) or None.

    This is what lets a character's state evolve across chapters: chapter N's
    generation seeds from chapter N-1's saved state, falling back further until
    one is found (and finally to the base sheet, handled by the caller).
    """
    init()
    for c in range(chapter - 1, 0, -1):
        ws = load(series_id, c)
        state = (ws.get("character_states") or {}).get(character)
        if isinstance(state, str) and state.strip():
            return c, state.strip()
    return None


def find_previous_record(series_id: str, chapter: int, character: str) -> tuple[int, dict] | None:
    """Most recent prior chapter (< `chapter`) that recorded a unified character
    record for `character`. Returns (chapter_no, record) or None.

    This is what lets the unified character state evolve across chapters: chapter
    N's generation pulls forward chapter N-1's saved record (descriptions, stats,
    skills, level/class), falling back further until one is found (and finally to
    the base sheet, handled by the caller).
    """
    init()
    for c in range(chapter - 1, 0, -1):
        ws = load(series_id, c)
        rec = (ws.get("character_records") or {}).get(character)
        if isinstance(rec, dict) and rec:
            return c, rec
    return None


def previous_chapter_summary(series_id: str, chapter: int) -> dict:
    """States + plot points recorded for the immediately previous chapter, used
    as context when generating this chapter's character states."""
    if chapter <= 1:
        return {"chapter": None, "character_states": {}, "plot_points": []}
    ws = load(series_id, chapter - 1)
    return {
        "chapter": chapter - 1,
        "character_states": ws.get("character_states") or {},
        "plot_points": ws.get("plot_points") or [],
    }
