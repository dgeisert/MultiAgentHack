"""Offline tests for the lore RAG layer.

Runs fully in MOCK_MODE (deterministic hashed embeddings, no network), so the
vector DB is built and queried with zero credentials. Each test uses its own
temp vector DB so it never touches the real data/ index.

    LOREWEAVER_MOCK=1 python -m pytest tests/test_rag.py -q
    LOREWEAVER_MOCK=1 python tests/test_rag.py          # also runs standalone
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOREWEAVER_MOCK", "1")

from loreweaver import rag, settings  # noqa: E402
from loreweaver.store import vectors  # noqa: E402

SERIES = "tidebound_test"

BIBLE = {
    "title": "The Tidebound Archive",
    "premise": "Librarians dive a drowned continent to recover crystallised memories.",
    "magic_system": "Memories crystallise into salt. Reading one costs a memory of equal weight.",
    "geography": {"reefs": "salt-reefs over sunken cities", "spires": "archive-spires"},
    "factions": [
        {"name": "The Tidebound", "goal": "preserve memory before erasure"},
        {"name": "The Undertow", "goal": "let the past drown"},
    ],
    "characters": [
        {"name": "Maren", "role": "protagonist",
         "personality": "stubborn, grief-driven archivist",
         "physical_description": "late 30s, salt-cracked hands, a scar along one wrist",
         "speaking_style": "terse, clipped", "voice_brief": "low alto, weathered"},
        {"name": "Coll", "role": "rival diver", "personality": "reckless charmer",
         "physical_description": "wiry 20s, sun-bleached hair",
         "speaking_style": "fast, sardonic", "voice_brief": "bright tenor"},
        {"name": "Narrator", "role": "narrator", "personality": "measured",
         "speaking_style": "lyrical", "voice_brief": "warm storyteller"},
    ],
}


def _use_temp_db():
    settings.VECTOR_DB_PATH = os.path.join(tempfile.mkdtemp(), "vec.db")


def test_index_and_count():
    _use_temp_db()
    n = rag.index_world_bible(SERIES, BIBLE, reset=True)
    # 3 characters + 4 lore fields (premise, magic_system, geography, factions)
    assert n == 7, n
    assert vectors.count(SERIES) == 7


def test_retrieval_surfaces_relevant_character():
    _use_temp_db()
    rag.index_world_bible(SERIES, BIBLE, reset=True)
    beat = {"title": "The Salt-Memory Dive",
            "beat": "The grief-driven archivist dives the salt-reefs to recover a memory."}
    hits = rag.retrieve(SERIES, rag.beat_query(beat), k_characters=1, k_lore=1)
    assert hits["characters"], "expected at least one character hit"
    # Maren (archivist, grief, salt) should beat Coll/Narrator on this beat.
    assert hits["characters"][0].name == "Maren", [h.name for h in hits["characters"]]
    assert hits["lore"], "expected at least one lore hit"


def test_retrieve_block_is_formatted_and_nonempty():
    _use_temp_db()
    rag.index_world_bible(SERIES, BIBLE, reset=True)
    beat = {"title": "Reading the Memory", "beat": "The cost of reading a crystallised memory."}
    block = rag.retrieve_block(SERIES, beat)
    assert "RELEVANT LORE FOR THIS CHAPTER" in block
    assert "Most relevant characters" in block


def test_incremental_character_index():
    _use_temp_db()
    rag.index_world_bible(SERIES, BIBLE, reset=True)
    before = vectors.count(SERIES)
    rag.index_characters(SERIES, [
        {"name": "The Salt-Warden", "role": "antagonist",
         "personality": "implacable keeper of the deep archive",
         "physical_description": "towering figure crusted in living salt",
         "speaking_style": "slow, formal", "voice_brief": "deep resonant bass"},
    ])
    assert vectors.count(SERIES) == before + 1
    beat = {"title": "The Warden", "beat": "A towering antagonist crusted in living salt appears."}
    hits = rag.retrieve(SERIES, rag.beat_query(beat), k_characters=1)
    assert hits["characters"][0].name == "The Salt-Warden"


def test_disabled_rag_returns_empty():
    _use_temp_db()
    rag.index_world_bible(SERIES, BIBLE, reset=True)
    old = settings.RAG_ENABLED
    settings.RAG_ENABLED = False
    try:
        assert rag.retrieve_block(SERIES, {"title": "x", "beat": "y"}) == ""
    finally:
        settings.RAG_ENABLED = old


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("\nAll RAG tests passed.")
