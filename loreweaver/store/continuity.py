"""Continuity store — the long-term memory that makes serialized autonomy work.

Persists the world bible, voice map, rolling summary, and chapter cursor per
series, plus a list of published episodes for the RSS feed. SQLite keeps the
whole thing dependency-free and file-portable.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from .. import settings


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
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS series (
                series_id TEXT PRIMARY KEY,
                title TEXT,
                world_bible TEXT,
                chapter_outline TEXT,
                voice_map TEXT,
                rolling_summary TEXT,
                current_chapter INTEGER DEFAULT 0,
                cover_url TEXT
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id TEXT,
                chapter INTEGER,
                title TEXT,
                description TEXT,
                audio_url TEXT,
                image_url TEXT,
                guid TEXT,
                pub_date TEXT,
                duration TEXT
            );
            """
        )


def load_series(series_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM series WHERE series_id=?", (series_id,)).fetchone()
    if not row:
        return None
    return {
        "series_id": row["series_id"],
        "title": row["title"],
        "world_bible": json.loads(row["world_bible"] or "null"),
        "chapter_outline": json.loads(row["chapter_outline"] or "[]"),
        "voice_map": json.loads(row["voice_map"] or "{}"),
        "rolling_summary": row["rolling_summary"] or "",
        "current_chapter": row["current_chapter"] or 0,
        "cover_url": row["cover_url"] or "",
    }


def save_series(series_id: str, *, title="", world_bible=None, chapter_outline=None,
                voice_map=None, rolling_summary="", current_chapter=0, cover_url="") -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO series (series_id,title,world_bible,chapter_outline,voice_map,
                   rolling_summary,current_chapter,cover_url)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(series_id) DO UPDATE SET
                   title=excluded.title, world_bible=excluded.world_bible,
                   chapter_outline=excluded.chapter_outline, voice_map=excluded.voice_map,
                   rolling_summary=excluded.rolling_summary,
                   current_chapter=excluded.current_chapter, cover_url=excluded.cover_url
            """,
            (series_id, title, json.dumps(world_bible), json.dumps(chapter_outline or []),
             json.dumps(voice_map or {}), rolling_summary, current_chapter, cover_url),
        )


def list_series() -> list[dict]:
    """Every series known to the store, enriched with an episode count.

    Series are sourced from the `series` table *and* from any series_id that
    only appears in `episodes` (e.g. a run that published before finalize wrote
    its series row), so the management UI never misses a generation.
    """
    with _conn() as con:
        srows = con.execute("SELECT * FROM series").fetchall()
        counts = {
            r["series_id"]: r["n"]
            for r in con.execute(
                "SELECT series_id, COUNT(*) AS n FROM episodes GROUP BY series_id"
            ).fetchall()
        }
        ep_only = {
            r["series_id"]
            for r in con.execute("SELECT DISTINCT series_id FROM episodes").fetchall()
        }

    out: list[dict] = []
    seen: set[str] = set()
    for row in srows:
        sid = row["series_id"]
        seen.add(sid)
        bible = json.loads(row["world_bible"] or "null") or {}
        out.append({
            "series_id": sid,
            "title": row["title"] or bible.get("title") or sid,
            "premise": bible.get("premise", ""),
            "current_chapter": row["current_chapter"] or 0,
            "cover_url": row["cover_url"] or "",
            "episode_count": counts.get(sid, 0),
            "has_world": bool(bible),
        })

    for sid in ep_only - seen:  # episodes exist but no series row yet
        eps = list_episodes(sid)
        out.append({
            "series_id": sid,
            "title": eps[-1]["title"].split(" — ")[0] if eps else sid,
            "premise": "",
            "current_chapter": max((e["chapter"] for e in eps), default=0),
            "cover_url": eps[-1].get("image_url", "") if eps else "",
            "episode_count": len(eps),
            "has_world": False,
        })

    out.sort(key=lambda s: s["series_id"])
    return out


def get_episode(series_id: str, chapter: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM episodes WHERE series_id=? AND chapter=? ORDER BY id DESC LIMIT 1",
            (series_id, chapter),
        ).fetchone()
    return dict(row) if row else None


def delete_episode(series_id: str, chapter: int) -> int:
    """Remove all episode rows for a chapter. Returns rows deleted."""
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM episodes WHERE series_id=? AND chapter=?", (series_id, chapter)
        )
        return cur.rowcount


def delete_series(series_id: str) -> None:
    """Drop a series and all of its episodes from the store."""
    with _conn() as con:
        con.execute("DELETE FROM episodes WHERE series_id=?", (series_id,))
        con.execute("DELETE FROM series WHERE series_id=?", (series_id,))


def set_current_chapter(series_id: str, chapter: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE series SET current_chapter=? WHERE series_id=?", (chapter, series_id)
        )


def add_episode(series_id: str, episode: dict) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO episodes (series_id,chapter,title,description,audio_url,
                   image_url,guid,pub_date,duration) VALUES (?,?,?,?,?,?,?,?,?)""",
            (series_id, episode["chapter"], episode["title"], episode.get("description", ""),
             episode.get("audio_url", ""), episode.get("image_url", ""), episode["guid"],
             episode.get("pub_date", ""), episode.get("duration", "")),
        )


def list_episodes(series_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM episodes WHERE series_id=? ORDER BY chapter ASC", (series_id,)
        ).fetchall()
    return [dict(r) for r in rows]
