"""Typed data contracts shared across the Loreweaver agent graph.

These TypedDicts double as documentation: every agent reads and writes a
well-defined slice of SeriesState. The long-lived fields (world_bible,
voice_map, rolling_summary, current_chapter) are persisted to the continuity
store between scheduled runs so the series can serialize indefinitely.
"""
from __future__ import annotations

from typing import Literal, TypedDict


class CharacterBrief(TypedDict, total=False):
    name: str
    role: str
    personality: str
    voice_brief: str  # age, timbre, accent, temperament -> drives Voice Design


class WorldConcept(TypedDict, total=False):
    title: str
    premise: str
    tone: str
    audience: str
    differentiators: list[str]
    sources: list[str]  # Tavily source links -> show notes / attribution


class ChapterBeat(TypedDict, total=False):
    index: int
    title: str
    beat: str  # what must happen this chapter


class WorldBible(TypedDict, total=False):
    title: str
    premise: str
    tone: str
    geography: str
    magic_system: str  # hard rules, costs, limits
    factions: list[dict]
    characters: list[CharacterBrief]
    central_conflict: str
    visual_identity: str  # palette + motifs -> drives covers


class ScriptLine(TypedDict, total=False):
    idx: int
    speaker: str   # "Narrator" | character name
    voice_id: str  # ElevenLabs voice id
    emotion: str   # e.g. "tense", "wry", "grief"
    text: str


class SeriesState(TypedDict, total=False):
    series_id: str
    mode: Literal["new_series", "next_chapter"]

    world_concept: WorldConcept | None
    world_bible: WorldBible | None
    chapter_outline: list[ChapterBeat]
    current_chapter: int
    rolling_summary: str

    chapter_draft: str
    performance_script: list[ScriptLine]
    voice_map: dict[str, str]   # character name -> elevenlabs voice_id

    audio_path: str | None
    transcript: str
    covers: dict[str, str]      # "square"/"thumb" -> file path
    video_path: str | None

    publish_results: dict[str, str]  # target -> live url
    show_notes: str

    qa_notes: list[str]
    qa_verdict: Literal["pass", "revise"]
    qa_target: str              # node to route back to on revise
    retries: dict[str, int]     # node -> attempts
