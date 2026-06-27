"""Embeddings wrapper: turns text into vectors for the lore RAG store.

Live path uses Gemini's `text-embedding-004` via the unified google-genai SDK.
Mock path uses a deterministic, offline *feature-hashing* embedding so the whole
RAG pipeline runs with zero credentials — and, unlike random hashing, it encodes
real lexical overlap, so a chapter beat that mentions "Maren" and "salt" will
actually retrieve Maren's character chunk in mock mode. This keeps the demo
honest while staying dependency-free (numpy only).

All vectors are L2-normalised, so cosine similarity is a plain dot product.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from .. import settings
from .util import log, retry

# Dimensionality of the offline mock embedding. Live Gemini embeddings are 768-d;
# the two are never mixed within a single index (mock vs live is decided per run).
_MOCK_DIM = 512
_TOKEN_RE = re.compile(r"[a-z0-9']+")


# ------------------------------------------------------------------- mock ----
def _mock_embed(text: str) -> np.ndarray:
    """Hashed bag-of-words embedding (a.k.a. the hashing trick).

    Each token is hashed into a bucket; we accumulate a signed, sublinear count.
    Result: texts sharing vocabulary land near each other in cosine space, with
    no model and no network.
    """
    vec = np.zeros(_MOCK_DIM, dtype=np.float32)
    tokens = _TOKEN_RE.findall((text or "").lower())
    for tok in tokens:
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        bucket = h % _MOCK_DIM
        sign = 1.0 if (h >> 1) & 1 else -1.0
        vec[bucket] += sign
    # mild sublinear scaling so very repetitive text doesn't dominate
    vec = np.sign(vec) * np.log1p(np.abs(vec))
    return _normalize(vec)


# ------------------------------------------------------------------- live ----
@retry(times=4, base_delay=2.0)
def _live_embed_batch(texts: list[str]) -> list[np.ndarray]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=120_000),
    )
    resp = client.models.embed_content(
        model=settings.EMBED_MODEL,
        contents=texts,
    )
    return [_normalize(np.asarray(e.values, dtype=np.float32)) for e in resp.embeddings]


# ---------------------------------------------------------------- helpers ----
def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32)


def embed_texts(texts: list[str]) -> list[np.ndarray]:
    """Embed a batch of texts -> list of L2-normalised float32 vectors."""
    if not texts:
        return []
    if settings.mock_mode():
        return [_mock_embed(t) for t in texts]
    log("embeddings", f"embed {len(texts)} text(s) via {settings.EMBED_MODEL}")
    try:
        return _live_embed_batch(texts)
    except Exception as e:  # noqa: BLE001
        if settings.fallback_mock():
            log("embeddings", f"live embed failed ({type(e).__name__}); using mock vectors")
            return [_mock_embed(t) for t in texts]
        raise RuntimeError(
            f"Embedding call failed after retries ({type(e).__name__}: {e}). "
            f"Check EMBED_MODEL ('{settings.EMBED_MODEL}') and GEMINI_API_KEY, or set "
            "LOREWEAVER_MOCK=1 to run fully offline."
        ) from e


def embed_text(text: str) -> np.ndarray:
    """Embed a single string -> one L2-normalised float32 vector."""
    return embed_texts([text])[0]
