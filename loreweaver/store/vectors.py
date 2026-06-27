"""A self-contained vector database for the lore RAG layer.

Keeps the project's dependency-light, file-portable ethos: a single SQLite file
holds every chunk's text + metadata + its embedding (stored as raw float32
bytes), and similarity search is an in-process numpy cosine over the rows for one
series. No external vector-DB server, no extra services — but the interface
(`upsert` / `query` / `reset`) mirrors a real vector DB, so swapping in Chroma or
pgvector later is a localized change.

Vectors are L2-normalised on the way in (see tools.embeddings), so cosine
similarity reduces to a dot product.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np

from .. import settings


@dataclass
class Hit:
    chunk_id: str
    kind: str          # "character" | "lore"
    name: str          # character name or lore field
    text: str          # the retrievable passage
    metadata: dict
    score: float       # cosine similarity in [-1, 1]


def _neutralize_stale_sidecars() -> None:
    """Empty any stale journal/WAL sidecar files next to the DB.

    Some sandboxed / network / fuse-mounted folders forbid *unlinking* files,
    which breaks SQLite's default DELETE journaling (it can't remove the
    "-journal" file after a commit -> "disk I/O error"). We run in MEMORY journal
    mode so no new sidecar is ever created; any sidecar that already exists is
    therefore stale. We can't delete it, but truncating it to zero bytes (which
    these mounts *do* allow) makes SQLite treat it as absent.
    """
    base = str(settings.VECTOR_DB_PATH)
    for suffix in ("-journal", "-wal", "-shm"):
        p = base + suffix
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                open(p, "wb").close()
        except OSError:
            pass


@contextmanager
def _conn():
    settings.ensure_dirs()
    _neutralize_stale_sidecars()
    con = sqlite3.connect(settings.VECTOR_DB_PATH)
    con.row_factory = sqlite3.Row
    # MEMORY journaling keeps the rollback journal off disk (see above), so the
    # DB works even on folders that reject the on-disk "-journal" sidecar.
    try:
        con.execute("PRAGMA journal_mode=MEMORY")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                series_id  TEXT NOT NULL,
                chunk_id   TEXT NOT NULL,
                kind       TEXT NOT NULL,
                name       TEXT,
                text       TEXT NOT NULL,
                metadata   TEXT,
                dim        INTEGER NOT NULL,
                embedding  BLOB NOT NULL,
                PRIMARY KEY (series_id, chunk_id)
            );
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_series ON chunks(series_id);")


def upsert(series_id: str, chunks: list[dict], embeddings: list[np.ndarray]) -> int:
    """Insert or replace chunks for a series.

    Each chunk is a dict with keys: chunk_id, kind, name, text, metadata(optional).
    `embeddings[i]` is the (already L2-normalised) vector for `chunks[i]`.
    """
    if not chunks:
        return 0
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings length mismatch")
    init()
    rows = []
    for ch, emb in zip(chunks, embeddings):
        vec = np.asarray(emb, dtype=np.float32)
        rows.append((
            series_id,
            ch["chunk_id"],
            ch.get("kind", "lore"),
            ch.get("name", ""),
            ch["text"],
            json.dumps(ch.get("metadata", {})),
            int(vec.shape[0]),
            vec.tobytes(),
        ))
    with _conn() as con:
        con.executemany(
            """INSERT INTO chunks (series_id, chunk_id, kind, name, text, metadata, dim, embedding)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(series_id, chunk_id) DO UPDATE SET
                   kind=excluded.kind, name=excluded.name, text=excluded.text,
                   metadata=excluded.metadata, dim=excluded.dim, embedding=excluded.embedding
            """,
            rows,
        )
    return len(rows)


def query(series_id: str, query_vec: np.ndarray, k: int = 5,
          kind: str | None = None) -> list[Hit]:
    """Return the top-k most similar chunks for a series (optionally one kind)."""
    init()
    q = np.asarray(query_vec, dtype=np.float32)
    sql = "SELECT * FROM chunks WHERE series_id=?"
    params: list = [series_id]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
    if not rows:
        return []

    # Compare only against vectors of matching dimensionality (guards against a
    # mock-built index being queried with a live vector or vice-versa).
    usable = [r for r in rows if r["dim"] == q.shape[0]]
    if not usable:
        return []

    matrix = np.frombuffer(b"".join(r["embedding"] for r in usable), dtype=np.float32)
    matrix = matrix.reshape(len(usable), q.shape[0])
    scores = matrix @ q  # cosine, since everything is L2-normalised
    order = np.argsort(-scores)[:k]

    hits = []
    for i in order:
        r = usable[int(i)]
        hits.append(Hit(
            chunk_id=r["chunk_id"],
            kind=r["kind"],
            name=r["name"] or "",
            text=r["text"],
            metadata=json.loads(r["metadata"] or "{}"),
            score=float(scores[int(i)]),
        ))
    return hits


def count(series_id: str) -> int:
    init()
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE series_id=?", (series_id,)
        ).fetchone()
    return int(row["n"]) if row else 0


def reset(series_id: str) -> None:
    """Drop all chunks for a series (used before a full re-index)."""
    init()
    with _conn() as con:
        con.execute("DELETE FROM chunks WHERE series_id=?", (series_id,))
