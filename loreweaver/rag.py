"""Lore RAG — retrieval-augmented context for chapter generation.

This is the bridge between the world bible and the Author agent. It:

  1. *Chunks* a world bible into retrievable passages — one per character and one
     per lore topic (premise, magic system, geography, factions, …).
  2. *Indexes* those chunks into the self-contained vector DB (store.vectors),
     embedding each with tools.embeddings.
  3. *Retrieves* the chunks most relevant to the chapter about to be written
     (queried by the chapter beat + rolling summary) and formats them into a
     compact prompt block.

The Author augments its full-bible prompt with this block, so the model's
attention is steered toward the characters and rules that actually matter for the
current chapter — which scales as a series accumulates dozens of characters.

Every public function is defensive: if embeddings/vector search fail for any
reason, indexing/retrieval degrade to a no-op rather than breaking the pipeline.
"""
from __future__ import annotations

import json

from . import settings
from .store import files, vectors
from .tools import embeddings
from .tools.util import log

# Lore topics rendered as individual retrievable chunks.
_LORE_FIELDS = ("premise", "tone", "geography", "magic_system",
                "central_conflict", "visual_identity")


# ----------------------------------------------------------------- chunking --
def _stringify(value) -> str:
    """Render a lore field (which may be a str, dict, or list) as readable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return "\n".join(
            f"{k.replace('_', ' ')}: {_stringify(v)}" for k, v in value.items() if v
        )
    if isinstance(value, list):
        return "\n".join(f"- {_stringify(v)}" for v in value if v)
    return str(value)


def _character_text(c: dict) -> str:
    """A self-contained passage describing one character (what the model reads)."""
    parts = [f"{c.get('name', '?')} ({c.get('role', '')})".strip()]
    for label, key in (
        ("Personality", "personality"),
        ("Physical description", "physical_description"),
        ("Backstory", "backstory"),
        ("Quirks", "quirks"),
        ("Speaking style", "speaking_style"),
        ("Voice brief", "voice_brief"),
    ):
        val = c.get(key)
        if val:
            parts.append(f"{label}: {val}")
    return "\n".join(parts)


def chunk_bible(bible: dict) -> list[dict]:
    """Break a world bible into vector-DB chunks (characters + lore topics)."""
    chunks: list[dict] = []

    for c in bible.get("characters", []) or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        chunks.append({
            "chunk_id": f"char::{files.slug(name)}",
            "kind": "character",
            "name": name,
            "text": _character_text(c),
            "metadata": {"role": c.get("role", ""),
                         "first_seen_chapter": c.get("first_seen_chapter")},
        })

    for field in _LORE_FIELDS:
        text = _stringify(bible.get(field))
        if text:
            chunks.append({
                "chunk_id": f"lore::{field}",
                "kind": "lore",
                "name": field.replace("_", " ").title(),
                "text": f"{field.replace('_', ' ').title()}: {text}",
                "metadata": {"field": field},
            })

    factions = bible.get("factions")
    if factions:
        body = _stringify([
            f"{f.get('name', '?')} — {f.get('goal', f.get('description', ''))}"
            if isinstance(f, dict) else f
            for f in factions
        ])
        chunks.append({
            "chunk_id": "lore::factions",
            "kind": "lore",
            "name": "Factions",
            "text": f"Factions: {body}",
            "metadata": {"field": "factions"},
        })

    return chunks


def _character_chunks(characters: list[dict]) -> list[dict]:
    out = []
    for c in characters or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "chunk_id": f"char::{files.slug(name)}",
            "kind": "character",
            "name": name,
            "text": _character_text(c),
            "metadata": {"role": c.get("role", ""),
                         "first_seen_chapter": c.get("first_seen_chapter")},
        })
    return out


# ------------------------------------------------------------------ indexing --
def _index_chunks(series_id: str, chunks: list[dict]) -> int:
    if not chunks:
        return 0
    vecs = embeddings.embed_texts([c["text"] for c in chunks])
    return vectors.upsert(series_id, chunks, vecs)


def index_world_bible(series_id: str, bible: dict, *, reset: bool = True) -> int:
    """(Re)build the entire vector index for a series from its world bible."""
    if not settings.RAG_ENABLED or not bible:
        return 0
    try:
        if reset:
            vectors.reset(series_id)
        n = _index_chunks(series_id, chunk_bible(bible))
        log("rag", f"indexed {n} chunk(s) for '{series_id}' into the vector DB")
        return n
    except Exception as e:  # noqa: BLE001  — never break the pipeline over RAG
        log("rag", f"index failed ({type(e).__name__}: {e}); continuing without RAG")
        return 0


def index_characters(series_id: str, characters: list[dict]) -> int:
    """Add/refresh just a few character chunks (e.g. new ones from the Lorekeeper)."""
    if not settings.RAG_ENABLED or not characters:
        return 0
    try:
        n = _index_chunks(series_id, _character_chunks(characters))
        if n:
            log("rag", f"indexed {n} new character chunk(s) for '{series_id}'")
        return n
    except Exception as e:  # noqa: BLE001
        log("rag", f"character index failed ({type(e).__name__}: {e}); skipping")
        return 0


# ----------------------------------------------------------------- retrieval --
def retrieve(series_id: str, query_text: str, *,
             k_characters: int | None = None, k_lore: int | None = None) -> dict:
    """Return the most relevant character + lore chunks for a query.

    Result: {"characters": [Hit, ...], "lore": [Hit, ...]}.
    """
    k_characters = settings.RAG_TOP_CHARACTERS if k_characters is None else k_characters
    k_lore = settings.RAG_TOP_LORE if k_lore is None else k_lore
    if not settings.RAG_ENABLED or not (query_text or "").strip():
        return {"characters": [], "lore": []}
    try:
        if vectors.count(series_id) == 0:
            return {"characters": [], "lore": []}
        qv = embeddings.embed_text(query_text)
        return {
            "characters": vectors.query(series_id, qv, k=k_characters, kind="character"),
            "lore": vectors.query(series_id, qv, k=k_lore, kind="lore"),
        }
    except Exception as e:  # noqa: BLE001
        log("rag", f"retrieval failed ({type(e).__name__}: {e}); returning nothing")
        return {"characters": [], "lore": []}


def beat_query(beat: dict, rolling_summary: str = "", extra: str = "") -> str:
    """Compose a retrieval query from the chapter beat + story-so-far summary."""
    parts = [beat.get("title", ""), beat.get("beat", ""), rolling_summary or "", extra or ""]
    return "\n".join(p for p in parts if p).strip()


def retrieve_block(series_id: str, beat: dict, rolling_summary: str = "",
                   extra: str = "") -> str:
    """Format a ready-to-paste prompt block of the most relevant lore.

    Returns "" when RAG is disabled, the index is empty, or nothing is found —
    so the caller can simply concatenate it.
    """
    hits = retrieve(series_id, beat_query(beat, rolling_summary, extra))
    chars, lore = hits["characters"], hits["lore"]
    if not chars and not lore:
        return ""

    lines = ["RELEVANT LORE FOR THIS CHAPTER (retrieved from the vector DB — "
             "prioritise these characters and rules; full canon follows below):"]
    if chars:
        lines.append("\nMost relevant characters:")
        for h in chars:
            lines.append(f"- {h.text}")
    if lore:
        lines.append("\nMost relevant world lore:")
        for h in lore:
            lines.append(f"- {h.text}")
    return "\n".join(lines)


# ------------------------------------------------------------------ rebuild ---
def reindex_from_disk(series_id: str) -> int:
    """Rebuild the index for a series from its saved world_bible.json on disk.

    Handy as a one-off CLI for series created before RAG existed.
    """
    path = files.lore_dir(series_id) / "world_bible.json"
    if not path.exists():
        log("rag", f"no world_bible.json for '{series_id}' at {path}")
        return 0
    bible = json.loads(path.read_text(encoding="utf-8"))
    return index_world_bible(series_id, bible, reset=True)
