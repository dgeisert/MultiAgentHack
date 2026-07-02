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


def chapter_characters_dir(story: str, chapter: int) -> Path:
    """Per-chapter character sheet snapshots: data/<story>/chNN/characters/."""
    d = chapter_dir(story, chapter) / "characters"
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


# RPG ability scores tracked for every character, in display order.
ABILITY_KEYS = ("strength", "wisdom", "intelligence", "dexterity",
                "constitution", "charisma", "luck")


def _coerce_int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def ensure_character_stats(character: dict) -> dict:
    """Fill in the RPG progression fields (level, class, ability scores, skills)
    with sane defaults so every character sheet has a complete stat block, even
    for series authored before stats existed. Mutates and returns the dict."""
    character["level"] = max(1, _coerce_int(character.get("level"), 1))

    # Accept either "char_class" (our key) or a raw "class" from the model.
    cls = character.get("char_class") or character.get("class") or "Adventurer"
    character["char_class"] = str(cls).strip() or "Adventurer"
    character.pop("class", None)

    raw = character.get("stats")
    raw = raw if isinstance(raw, dict) else {}
    stats = {}
    for key in ABILITY_KEYS:
        stats[key] = max(1, _coerce_int(raw.get(key), 10))
    character["stats"] = stats

    skills = character.get("skills")
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",")]
    if not isinstance(skills, list):
        skills = []
    # de-dupe while preserving order
    seen, clean = set(), []
    for s in skills:
        s = str(s).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            clean.append(s)
    character["skills"] = clean
    return character


def _character_sheet_markdown(character: dict, *, chapter: int | None = None,
                              state_note: str = "") -> str:
    """Render a character's full markdown sheet. With a `chapter`, the heading is
    annotated and (optionally) a per-chapter STATE section is appended, so the
    same renderer produces both the canonical sheet and per-chapter snapshots."""
    ensure_character_stats(character)
    name = character.get("name", "Unknown")

    voice = ""
    if character.get("voice_name") and character.get("voice_id"):
        voice = f"{character['voice_name']} ({character['voice_id']})"
    elif character.get("voice_name"):
        voice = character["voice_name"]
    elif character.get("voice_id"):
        voice = character["voice_id"]

    fields = [
        ("Role", character.get("role", "")),
        ("Level", str(character["level"])),
        ("Class", character["char_class"]),
        ("First seen", f"Chapter {character.get('first_seen_chapter')}"
         if character.get("first_seen_chapter") else ""),
        ("Personality", character.get("personality", "")),
        ("Speaking style", character.get("speaking_style", "")),
        ("Voice brief", character.get("voice_brief", "")),
        ("Voice (cast)", voice),
    ]
    heading = f"# {name}" + (f" — as of Chapter {chapter}" if chapter else "")
    lines = [heading, ""]
    for label, val in fields:
        if val:
            lines.append(f"**{label}:** {val}")

    if chapter and state_note:
        lines += ["", f"## State in Chapter {chapter}", state_note.strip()]

    lines += ["", "## Stats", "", "| Ability | Score |", "| --- | --- |"]
    for key in ABILITY_KEYS:
        lines.append(f"| {key.title()} | {character['stats'][key]} |")

    lines += ["", "## Skills"]
    if character["skills"]:
        lines += [f"- {s}" for s in character["skills"]]
    else:
        lines.append("_None yet._")

    if character.get("physical_description"):
        lines += ["", "## Physical description", character["physical_description"]]
    if character.get("backstory"):
        lines += ["", "## Backstory", character["backstory"]]
    if character.get("quirks"):
        lines += ["", "## Quirks", character["quirks"]]
    return "\n".join(lines) + "\n"


def save_character_sheet(story: str, character: dict) -> Path:
    """Write the canonical markdown character sheet to
    data/<story>/characters/<name>.md."""
    name = character.get("name", "Unknown")
    path = characters_dir(story) / f"{slug(name)}.md"
    path.write_text(_character_sheet_markdown(character), encoding="utf-8")
    return path


def save_chapter_character_sheet(story: str, chapter: int, character: dict,
                                 state_note: str = "") -> Path:
    """Snapshot a character's sheet AS OF a given chapter to
    data/<story>/chNN/characters/<name>.md — a frozen record of where the
    character was (stats, skills, and optional in-chapter state) in that chapter."""
    name = character.get("name", "Unknown")
    path = chapter_characters_dir(story, chapter) / f"{slug(name)}.md"
    path.write_text(
        _character_sheet_markdown(character, chapter=chapter, state_note=state_note),
        encoding="utf-8")
    return path


def chapter_characters_json_path(story: str, chapter: int) -> Path:
    """data/<story>/chNN/characters.json — the unified per-chapter character
    records (frozen base descriptions + stats/skills + in-chapter state)."""
    return chapter_dir(story, chapter) / "characters.json"


def save_chapter_characters(story: str, chapter: int, records: dict) -> Path:
    """Persist the unified per-chapter character records to the chapter folder as
    a single structured JSON file the Studio (and anything else) can reload."""
    import json

    path = chapter_characters_json_path(story, chapter)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return path


def load_chapter_characters(story: str, chapter: int) -> dict:
    """Read back data/<story>/chNN/characters.json, or {} if absent/invalid."""
    import json

    path = chapter_characters_json_path(story, chapter)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


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
