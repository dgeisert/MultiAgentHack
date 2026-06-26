"""Gemini wrapper: text generation (with optional JSON mode) + image generation.

Live path uses google-generativeai. Mock path returns deterministic content so
the graph is fully runnable offline.
"""
from __future__ import annotations

import hashlib
import json as _json
import textwrap

from .. import settings
from .util import log, retry


# ---------------------------------------------------------------- text -------
@retry(times=3)
def _live_text(prompt: str, json_mode: bool, system: str | None) -> str:
    import google.generativeai as genai

    genai.configure(api_key=settings.GEMINI_API_KEY)
    cfg = {"response_mime_type": "application/json"} if json_mode else {}
    model = genai.GenerativeModel(
        settings.GEMINI_TEXT_MODEL,
        system_instruction=system,
        generation_config=cfg or None,
    )
    resp = model.generate_content(prompt)
    return resp.text


def generate_text(prompt: str, *, json_mode: bool = False, system: str | None = None) -> str:
    if settings.mock_mode():
        return _mock_text(prompt, json_mode)
    log("gemini", f"generate_text (json={json_mode}, {len(prompt)} chars)")
    return _live_text(prompt, json_mode, system)


def generate_json(prompt: str, *, system: str | None = None) -> object:
    raw = generate_text(prompt, json_mode=True, system=system)
    try:
        return _json.loads(raw)
    except Exception:  # tolerate code-fenced JSON from the live model
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        return _json.loads(raw)


# --------------------------------------------------------------- image -------
@retry(times=3)
def _live_image(prompt: str, out_path: str) -> str:
    import google.generativeai as genai

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model = genai.ImageGenerationModel(settings.GEMINI_IMAGE_MODEL)
    result = model.generate_images(prompt=prompt, number_of_images=1)
    result[0].save(out_path)
    return out_path


def generate_image(prompt: str, out_path: str) -> str:
    if settings.mock_mode():
        return _mock_image(prompt, out_path)
    log("gemini", f"generate_image -> {out_path}")
    return _live_image(prompt, out_path)


# ----------------------------------------------------------------- mock ------
def _seeded(prompt: str) -> int:
    return int(hashlib.sha1(prompt.encode()).hexdigest(), 16)


def _mock_text(prompt: str, json_mode: bool) -> str:
    p = prompt.lower()
    if json_mode and "concept" in p:
        return _json.dumps({
            "title": "The Tidebound Archive",
            "premise": "On a drowned continent, librarians dive sunken cities to "
                       "recover memories crystallised in salt before the next tide erases them.",
            "tone": "melancholic wonder, slow-burn mystery",
            "audience": "adult fantasy listeners",
            "differentiators": ["memory-as-magic", "oceanic archaeology", "no chosen one"],
            "sources": ["https://example.org/folklore/tide-myths",
                        "https://example.org/marine-archaeology"],
        })
    if json_mode and "world bible" in p:
        return _json.dumps(_MOCK_BIBLE)
    if json_mode and "script" in p:
        return _json.dumps(_MOCK_SCRIPT_LINES)
    if json_mode and ("qa" in p or "verdict" in p):
        return _json.dumps({"verdict": "pass", "notes": ["Canon consistent.", "Safe content."]})
    if "rolling summary" in p:
        return "Maren recovered the first salt-memory and learned the Archive is sinking faster than recorded."
    # default prose (a chapter)
    return textwrap.dedent(
        """\
        The tide had not yet turned when Maren slipped beneath the black water.
        Salt stung the old scar along her wrist, and the drowned city of Veil
        opened below her like a held breath. "Stay close," she told the lantern-fish,
        though it never listened. Somewhere in the archive-spires, a memory was
        crystallising, and if she was slow, the sea would take it forever.
        """
    ).strip() * 6  # pad to a believable chapter length in mock mode


def _mock_image(prompt: str, out_path: str) -> str:
    """Generate a deterministic placeholder cover with Pillow (no network)."""
    try:
        from PIL import Image, ImageDraw

        seed = _seeded(prompt)
        r, g, b = (seed % 200) + 30, (seed // 200 % 200) + 30, (seed // 40000 % 200) + 30
        img = Image.new("RGB", (1024, 1024), (r, g, b))
        d = ImageDraw.Draw(img)
        for i in range(0, 1024, 48):
            d.line([(0, i), (1024, i)], fill=(r // 2, g // 2, b // 2), width=2)
        d.rectangle([60, 420, 964, 604], fill=(0, 0, 0))
        d.text((90, 480), "LOREWEAVER\n(mock cover)", fill=(255, 255, 255))
        img.save(out_path)
    except Exception:
        # last-resort: a tiny valid PNG so downstream code never crashes
        with open(out_path, "wb") as f:
            f.write(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108020000"
                    "00907753de0000000c4944415408d76360000000020001e221bc330000"
                    "000049454e44ae426082"
                )
            )
    return out_path


_MOCK_BIBLE = {
    "title": "The Tidebound Archive",
    "premise": "Librarians dive a drowned continent to recover crystallised memories.",
    "tone": "melancholic wonder",
    "geography": "A flooded world of archive-spires and salt-reefs over sunken cities.",
    "magic_system": "Memories crystallise into salt. Reading one costs the reader a memory of "
                    "equal weight. Nothing is free; the sea always takes something back.",
    "factions": [
        {"name": "The Tidebound", "goal": "preserve memory before erasure"},
        {"name": "The Undertow", "goal": "let the past drown so the world can move on"},
    ],
    "characters": [
        {"name": "Narrator", "role": "narrator", "personality": "measured, intimate",
         "voice_brief": "warm mid-range storyteller, unhurried, faint coastal lilt"},
        {"name": "Maren", "role": "protagonist", "personality": "stubborn, grief-driven archivist",
         "voice_brief": "woman, late 30s, low alto, weathered, quiet resolve"},
        {"name": "Coll", "role": "rival diver", "personality": "reckless charmer",
         "voice_brief": "man, 20s, bright tenor, fast cadence, sardonic"},
    ],
    "central_conflict": "Maren must decide which memories are worth the cost of her own.",
    "visual_identity": "deep teal and salt-white, bioluminescent accents, drowned gothic spires",
}

_MOCK_SCRIPT_LINES = {
    "lines": [
        {"idx": 0, "speaker": "Narrator", "emotion": "calm",
         "text": "The tide had not yet turned when Maren slipped beneath the black water."},
        {"idx": 1, "speaker": "Maren", "emotion": "tense", "text": "Stay close."},
        {"idx": 2, "speaker": "Narrator", "emotion": "wry",
         "text": "she told the lantern-fish, though it never listened."},
        {"idx": 3, "speaker": "Coll", "emotion": "sardonic",
         "text": "You always talk to the fish before the dangerous part."},
    ]
}
