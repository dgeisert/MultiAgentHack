"""Central configuration. Reads from environment / .env.

MOCK_MODE is the key switch: when any required API key is missing (or
LOREWEAVER_MOCK=1 is set), tool wrappers return deterministic stub data so the
entire graph runs end-to-end with zero credentials. This makes the demo
bulletproof and lets you develop offline, then flip to live by adding keys.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # optional, nice-to-have
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("LOREWEAVER_DATA", ROOT / "data"))
ARTIFACTS_DIR = DATA_DIR / "artifacts"
WEB_PLAYER_DIR = ROOT / "loreweaver" / "web_player"
DB_PATH = DATA_DIR / "continuity.db"
VECTOR_DB_PATH = Path(os.getenv("LOREWEAVER_VECTOR_DB", DATA_DIR / "lore_vectors.db"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.0-flash")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "imagen-3.0-generate-002")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
EMBED_MODEL = os.getenv("LOREWEAVER_EMBED_MODEL", "text-embedding-004")

# Publishing targets
PODCAST_HOST_API_KEY = os.getenv("PODCAST_HOST_API_KEY")
YOUTUBE_CLIENT_SECRETS = os.getenv("YOUTUBE_CLIENT_SECRETS")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

# Story knobs
CHAPTER_MIN_WORDS = int(os.getenv("CHAPTER_MIN_WORDS", "900"))
CHAPTER_MAX_WORDS = int(os.getenv("CHAPTER_MAX_WORDS", "2200"))
MAX_RETRIES = int(os.getenv("LOREWEAVER_MAX_RETRIES", "2"))
SCHEDULE_CRON = os.getenv("LOREWEAVER_CRON", "0 9 * * *")

# Lore RAG knobs: retrieve the most relevant characters + lore for each chapter
# beat and inject them into the Author's prompt (augmenting the full world bible).
RAG_ENABLED = os.getenv("LOREWEAVER_RAG", "1").lower() not in ("0", "false", "no")
RAG_TOP_CHARACTERS = int(os.getenv("LOREWEAVER_RAG_CHARACTERS", "4"))
RAG_TOP_LORE = int(os.getenv("LOREWEAVER_RAG_LORE", "4"))

_FORCE_MOCK = os.getenv("LOREWEAVER_MOCK", "").lower() in ("1", "true", "yes")


def mock_mode() -> bool:
    """True when we should use stub tool implementations."""
    if _FORCE_MOCK:
        return True
    # If the core creative keys are missing, run fully mocked.
    return not (GEMINI_API_KEY and ELEVENLABS_API_KEY and TAVILY_API_KEY)


def fallback_mock() -> bool:
    """If True, a failed LIVE call degrades to mock output instead of crashing
    the run — a safety net for live demos. Opt in with LOREWEAVER_FALLBACK_MOCK=1."""
    return os.getenv("LOREWEAVER_FALLBACK_MOCK", "").lower() in ("1", "true", "yes")


def ensure_dirs() -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
