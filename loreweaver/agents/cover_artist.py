"""Cover Artist — cover-art agent (Gemini image + Pillow).

Generates a per-chapter cover from the world's visual identity and the chapter's
key moment, producing podcast-square and YouTube-thumbnail variants.
"""
from __future__ import annotations

from .. import settings
from ..state import SeriesState
from ..store import files
from ..tools import gemini
from ..tools.util import log


def _resize(src: str, dst: str, size: tuple[int, int]) -> str:
    try:
        from PIL import Image

        Image.open(src).convert("RGB").resize(size).save(dst)
        return dst
    except Exception:  # noqa: BLE001
        return src


def run(state: SeriesState) -> dict:
    settings.ensure_dirs()
    sid = state["series_id"]
    chapter = state.get("current_chapter", 1)
    bible = state.get("world_bible") or {}

    visual = bible.get("visual_identity", "epic fantasy, dramatic lighting")
    title = bible.get("title", "Loreweaver")
    prompt = (
        f"Audiobook cover art for '{title}', chapter {chapter}. Visual identity: {visual}. "
        f"Scene mood from: {state.get('rolling_summary','')[:300]}. "
        "Cinematic, no text, painterly fantasy illustration."
    )

    out_dir = files.cover_dir(sid)
    raw = gemini.generate_image(prompt, str(out_dir / f"ch{chapter:02d}_raw.png"))
    square = _resize(raw, str(out_dir / f"ch{chapter:02d}_square.png"), (3000, 3000))
    thumb = _resize(raw, str(out_dir / f"ch{chapter:02d}_thumb.png"), (1280, 720))

    log("cover", f"cover art ready ({square})")
    return {"covers": {"square": square, "thumb": thumb}}
