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


def characters_dir(story: str) -> Path:
    d = story_dir(story) / "characters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lore_dir(story: str) -> Path:
    d = story_dir(story) / "lore"
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


def save_character_sheet(story: str, character: dict) -> Path:
    """Write a markdown character sheet to data/<story>/characters/<name>.md."""
    name = character.get("name", "Unknown")
    fields = [
        ("Role", character.get("role", "")),
        ("First seen", f"Chapter {character.get('first_seen_chapter')}"
         if character.get("first_seen_chapter") else ""),
        ("Personality", character.get("personality", "")),
        ("Speaking style", character.get("speaking_style", "")),
        ("Voice brief", character.get("voice_brief", "")),
    ]
    lines = [f"# {name}", ""]
    for label, val in fields:
        if val:
            lines.append(f"**{label}:** {val}")
    if character.get("physical_description"):
        lines += ["", "## Physical description", character["physical_description"]]
    if character.get("backstory"):
        lines += ["", "## Backstory", character["backstory"]]
    if character.get("quirks"):
        lines += ["", "## Quirks", character["quirks"]]
    path = characters_dir(story) / f"{slug(name)}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def list_character_names(story: str) -> list[str]:
    """Names already sheeted on disk (used to detect genuinely new characters)."""
    d = story_dir(story) / "characters"
    if not d.exists():
        return []
    return [p.stem for p in d.glob("*.md")]


# Lore fields rendered as individual markdown files under data/<story>/lore/.
_LORE_FIELDS = ("premise", "tone", "geography", "magic_system", "central_conflict",
                "visual_identity")


def save_lore(story: str, bible: dict) -> list[Path]:
    """Persist the world bible: a JSON master + readable per-topic lore files.
    Characters are saved separately via save_character_sheet()."""
    import json

    d = lore_dir(story)
    written = []
    (d / "world_bible.json").write_text(json.dumps(bible, indent=2), encoding="utf-8")
    written.append(d / "world_bible.json")

    for field in _LORE_FIELDS:
        val = bible.get(field)
        if val:
            p = d / f"{field}.md"
            p.write_text(f"# {field.replace('_', ' ').title()}\n\n{val}\n", encoding="utf-8")
            written.append(p)

    factions = bible.get("factions")
    if factions:
        body = "\n".join(f"- **{f.get('name','?')}** — {f.get('goal', f.get('description',''))}"
                         for f in factions)
        p = d / "factions.md"
        p.write_text(f"# Factions\n\n{body}\n", encoding="utf-8")
        written.append(p)
    return written


def latest_chapter_text(story: str, chapter: int) -> str:
    """Return the most recent revision text for a chapter (edit_NN over draft_01)."""
    d = story_dir(story) / f"ch{chapter:02d}"
    if not d.exists():
        return ""
    edits = sorted(d.glob("edit_*.txt"))
    target = edits[-1] if edits else (d / "draft_01.txt")
    return target.read_text(encoding="utf-8") if target.exists() else ""


def previous_chapters_text(story: str, current_chapter: int) -> list[tuple[int, str]]:
    """All prior chapters' final text, in order, for full-context generation."""
    out = []
    for c in range(1, current_chapter):
        text = latest_chapter_text(story, c)
        if text:
            out.append((c, text))
    return out


def clip_path(story: str, chapter: int, index: int, character: str) -> str:
    """clip_01_Narrator.mp3 under data/<story>/audio/chNN/."""
    return str(audio_dir(story, chapter) / f"clip_{index:02d}_{slug(character)}.mp3")


def chapter_audio_path(story: str, chapter: int) -> str:
    return str(audio_dir(story, chapter) / "chapter.mp3")
