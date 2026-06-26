"""On-disk artifact storage, organised per story.

Layout (story name == the --series id you pass):

    data/<story>/
        ch01/
            draft_01.txt        first Author pass
            edit_01.txt         after the 1st QA revision
            edit_02.txt         ...
        audio/
            ch01/
                clip_01_Narrator.mp3
                clip_02_Maren.mp3
                chapter.mp3      final stitched chapter
        cover/
            ch01_square.png
            ch01_thumb.png
        video/
            ch01.mp4

Chapter is encoded in the folder/clip path so the serialized drip never
overwrites earlier chapters' text or audio.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import settings


def slug(name: str) -> str:
    """Filesystem-safe token from an arbitrary name."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("_")
    return s or "voice"


def story_dir(story: str) -> Path:
    d = settings.DATA_DIR / slug(story)
    d.mkdir(parents=True, exist_ok=True)
    return d


def chapter_dir(story: str, chapter: int) -> Path:
    d = story_dir(story) / f"ch{chapter:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_dir(story: str, chapter: int) -> Path:
    d = story_dir(story) / "audio" / f"ch{chapter:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cover_dir(story: str) -> Path:
    d = story_dir(story) / "cover"
    d.mkdir(parents=True, exist_ok=True)
    return d


def video_dir(story: str) -> Path:
    d = story_dir(story) / "video"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_text_revision(story: str, chapter: int, text: str) -> Path:
    """Persist a chapter draft. The first save for a chapter is draft_01.txt;
    every later save (a QA-driven rewrite) becomes edit_01.txt, edit_02.txt, …
    """
    d = chapter_dir(story, chapter)
    draft = d / "draft_01.txt"
    if not draft.exists():
        path = draft
    else:
        n = len(list(d.glob("edit_*.txt"))) + 1
        path = d / f"edit_{n:02d}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def clip_path(story: str, chapter: int, index: int, character: str) -> str:
    """clip_01_Narrator.mp3 under data/<story>/audio/chNN/."""
    return str(audio_dir(story, chapter) / f"clip_{index:02d}_{slug(character)}.mp3")


def chapter_audio_path(story: str, chapter: int) -> str:
    return str(audio_dir(story, chapter) / "chapter.mp3")
