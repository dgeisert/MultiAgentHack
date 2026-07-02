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
def _client():
    """A Gemini client with no request timeout: a full chapter generation can
    run arbitrarily long and must never be cut off. Streaming keeps the
    connection alive during the model's long thinking phase."""
    from google import genai

    return genai.Client(api_key=settings.GEMINI_API_KEY)


# Retry transient connection drops (RemoteProtocolError etc.) with backoff.
@retry(times=4, base_delay=2.0)
def _live_text(prompt: str, json_mode: bool, system: str | None) -> str:
    from google.genai import types

    # Keep a named reference to the client for the whole call: if it is only a
    # temporary (e.g. _client().models...), CPython can finalise it mid-request
    # and close its HTTP connection -> "Cannot send a request, client closed".
    client = _client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json" if json_mode else None,
    )
    # STREAM the response. gemini-3.x are *thinking* models: on a large
    # generation they spend many seconds reasoning before emitting the first
    # token. A non-streaming generate_content holds one idle connection open
    # for that entire wait, and the server/intermediary closes it before the
    # first byte -> "Server disconnected without sending a response". Streaming
    # keeps bytes flowing so the connection stays alive; we reassemble the text
    # chunks into a single string (identical return contract to before).
    parts: list[str] = []
    for chunk in client.models.generate_content_stream(
        model=settings.GEMINI_TEXT_MODEL, contents=prompt, config=config
    ):
        if chunk.text:
            parts.append(chunk.text)
    return "".join(parts)


def generate_text(prompt: str, *, json_mode: bool = False, system: str | None = None) -> str:
    if settings.mock_mode():
        return _mock_text(prompt, json_mode)
    log("gemini", f"generate_text (json={json_mode}, {len(prompt)} chars)")
    try:
        return _live_text(prompt, json_mode, system)
    except Exception as e:  # noqa: BLE001
        if settings.fallback_mock():
            log("gemini", f"live failed ({type(e).__name__}); falling back to mock content")
            return _mock_text(prompt, json_mode)
        raise RuntimeError(
            f"Gemini call failed after retries ({type(e).__name__}: {e}). "
            f"Check GEMINI_TEXT_MODEL ('{settings.GEMINI_TEXT_MODEL}') is a valid, "
            "available model and that GEMINI_API_KEY is a Gemini API key (usually "
            "starts with 'AIza'). Set LOREWEAVER_MOCK=1 to run fully offline, or "
            "LOREWEAVER_FALLBACK_MOCK=1 to auto-degrade to mock on live errors."
        ) from e


def generate_json(prompt: str, *, system: str | None = None) -> object:
    raw = generate_text(prompt, json_mode=True, system=system)
    try:
        return _json.loads(raw)
    except Exception:  # tolerate code-fenced JSON from the live model
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        return _json.loads(raw)


# --------------------------------------------------------------- image -------
@retry(times=4, base_delay=2.0)
def _live_image(prompt: str, out_path: str) -> str:
    import base64

    from google.genai import types

    client = _client()  # hold the reference (see _live_text)
    model = settings.GEMINI_IMAGE_MODEL

    # Imagen models use the predict-style generate_images endpoint...
    if model.lower().startswith("imagen"):
        result = client.models.generate_images(
            model=model, prompt=prompt,
            config=types.GenerateImagesConfig(number_of_images=1),
        )
        result.generated_images[0].image.save(out_path)
        return out_path

    # ...Gemini "flash-image" models return image bytes via generate_content.
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
    )
    for cand in (resp.candidates or []):
        for part in (getattr(cand.content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                data = inline.data
                if isinstance(data, str):
                    data = base64.b64decode(data)
                with open(out_path, "wb") as f:
                    f.write(data)
                return out_path
    raise RuntimeError("Gemini returned no image data for the cover prompt")


def generate_image(prompt: str, out_path: str) -> str:
    if settings.mock_mode():
        return _mock_image(prompt, out_path)
    log("gemini", f"generate_image -> {out_path}")
    try:
        return _live_image(prompt, out_path)
    except Exception as e:  # noqa: BLE001
        if settings.fallback_mock():
            log("gemini", f"image gen failed ({type(e).__name__}); using mock cover")
            return _mock_image(prompt, out_path)
        raise


# ----------------------------------------------------------------- mock ------
def _seeded(prompt: str) -> int:
    return int(hashlib.sha1(prompt.encode()).hexdigest(), 16)


def _mock_attribution(prompt: str) -> dict:
    """Mock a single per-quote speaker attribution.

    Parses the KNOWN CHARACTERS list out of the prompt and deterministically
    picks one (seeded by the quoted line) so offline runs are stable.
    """
    import ast

    chars: list[str] = []
    for line in prompt.splitlines():
        if line.startswith("KNOWN CHARACTERS:"):
            try:
                chars = ast.literal_eval(line.split(":", 1)[1].strip())
            except Exception:  # noqa: BLE001
                chars = []
            break
    quote = ""
    for line in prompt.splitlines():
        if line.startswith("QUOTED LINE:"):
            quote = line.split(":", 1)[1].strip()
            break
    speaker = chars[_seeded(quote) % len(chars)] if chars else "Unknown"
    return {"speaker": speaker, "emotion": "neutral"}


def _mock_progression(prompt: str) -> dict:
    """Mock a post-chapter RPG progression update for one character.

    Deterministic (seeded by the prompt, which includes the character name) so
    offline demos produce stable, plausible-looking growth.
    """
    seed = _seeded(prompt)
    abilities = ["strength", "wisdom", "intelligence", "dexterity",
                 "constitution", "charisma", "luck"]
    bumped = abilities[seed % len(abilities)]
    skills_pool = ["Tide-Reading", "Salt-Diving", "Memory-Sifting", "Cold Endurance",
                   "Lantern Signaling", "Undertow Lore", "Breath-Holding"]
    gained = skills_pool[seed % len(skills_pool)]
    return {
        "level_delta": 1,
        "new_class": None,
        "stat_deltas": {bumped: 1},
        "skills_gained": [gained],
        "summary": f"Grew through the chapter's trials (+1 {bumped.title()}, learned {gained}).",
    }


def _mock_text(prompt: str, json_mode: bool) -> str:
    p = prompt.lower()
    # Intent routing is PRIORITY-ORDERED: several prompts legitimately contain
    # overlapping words (the bible prompt mentions "concept"; the QA and outline
    # prompts embed the world bible). Check the most specific intent first.
    if json_mode:
        if "rpg progression update" in p:  # post-chapter character-sheet update
            return _json.dumps(_mock_progression(prompt))
        if "brain-dump" in p:  # most specific: the braindumper intent
            return _json.dumps(_MOCK_BRAINDUMP)
        if "continuity editor" in p or "verdict" in p:
            return _json.dumps({"verdict": "pass",
                                "notes": ["Canon consistent.", "Safe content."]})
        if "new named characters" in p or "character sheet" in p:
            return _json.dumps(_MOCK_NEW_CHARACTERS)
        if "outline" in p or "chapters" in p:
            return _json.dumps(_MOCK_OUTLINE)
        if "world bible" in p:
            return _json.dumps(_MOCK_BIBLE)
        if "attribute the quoted line" in p:  # per-quote speaker attribution
            return _json.dumps(_mock_attribution(prompt))
        if "performance script" in p or "script" in p:
            return _json.dumps(_MOCK_SCRIPT_LINES)
        if "concept" in p:
            return _json.dumps({
                "title": "The Tidebound Archive",
                "premise": "On a drowned continent, librarians dive sunken cities to "
                           "recover memories crystallised in salt before the tide erases them.",
                "tone": "melancholic wonder, slow-burn mystery",
                "audience": "adult fantasy listeners",
                "differentiators": ["memory-as-magic", "oceanic archaeology", "no chosen one"],
                "sources": ["https://example.org/folklore/tide-myths",
                            "https://example.org/marine-archaeology"],
            })
        return _json.dumps({})
    if "update the rolling" in p:  # the summary-writing instruction specifically
        return "Maren recovered the first salt-memory and learned the Archive is sinking faster than recorded."
    # default prose (a chapter). Repeated to a believable chapter length so the
    # QA length gate passes cleanly in offline mock-mode demos.
    para = textwrap.dedent(
        """\
        The tide had not yet turned when Maren slipped beneath the black water.
        Salt stung the old scar along her wrist, and the drowned city of Veil
        opened below her like a held breath. "Stay close," she told the lantern-fish,
        though it never listened. Somewhere in the archive-spires, a memory was
        crystallising, and if she was slow, the sea would take it forever. She kicked
        downward past the toppled colonnades where the Tidebound had once shelved a
        thousand lifetimes, and felt the old grief rise with the cold.
        """
    ).strip()
    return ("\n\n".join(para for _ in range(20)))  # ~1,400 words


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
         "physical_description": "unseen guide to the drowned world",
         "backstory": "An archivist of the Tidebound recounting events long after the fact.",
         "quirks": "addresses the listener directly in moments of doubt",
         "speaking_style": "unhurried, lyrical, fond of tidal metaphors",
         "voice_brief": "warm mid-range storyteller, unhurried, faint coastal lilt"},
        {"name": "Maren", "role": "protagonist",
         "personality": "stubborn, grief-driven archivist",
         "physical_description": "woman in her late 30s, salt-cracked hands, a pale scar along "
                                 "one wrist, close-cropped grey-streaked hair",
         "backstory": "Lost her sister to the Undertow and now dives to recover the memories "
                      "the sea is erasing, refusing to let the past drown.",
         "quirks": "talks to the lantern-fish; counts breaths before a dive",
         "speaking_style": "terse, clipped sentences that soften when she speaks of the dead",
         "voice_brief": "woman, late 30s, low alto, weathered, quiet resolve"},
        {"name": "Coll", "role": "rival diver", "personality": "reckless charmer",
         "physical_description": "wiry man in his 20s, sun-bleached hair, a gap-toothed grin",
         "backstory": "A freelance memory-diver who sells what he finds to the highest bidder.",
         "quirks": "flips a salt-coin when thinking; never stops talking underwater",
         "speaking_style": "fast, sardonic, trails off into half-jokes",
         "voice_brief": "man, 20s, bright tenor, fast cadence, sardonic"},
    ],
    "central_conflict": "Maren must decide which memories are worth the cost of her own.",
    "visual_identity": "deep teal and salt-white, bioluminescent accents, drowned gothic spires",
}

_MOCK_BRAINDUMP = {
    "lore": {
        "premise": "A drowned world where memory is the only currency that outlasts the tide.",
        "tone": "melancholic wonder, slow-burn mystery",
        "geography": "Archive-spires and salt-reefs rising over sunken cities.",
        "magic_system": "Memories crystallise into salt; reading one costs a memory of equal "
                        "weight. The sea always takes something back.",
        "central_conflict": "Who decides which memories are worth saving — and at what cost.",
        "factions": [
            {"name": "The Tidebound", "goal": "preserve memory before erasure"},
            {"name": "The Undertow", "goal": "let the past drown so the world can move on"},
        ],
    },
    "characters": [
        {"name": "Maren", "role": "protagonist",
         "personality": "stubborn, grief-driven archivist",
         "physical_description": "woman in her late 30s, salt-cracked hands, a pale scar along "
                                 "one wrist, close-cropped grey-streaked hair",
         "backstory": "Lost her sister to the Undertow and now dives to recover memories the "
                      "sea is erasing.",
         "quirks": "talks to the lantern-fish; counts breaths before a dive",
         "speaking_style": "terse, clipped sentences that soften when she speaks of the dead",
         "voice_brief": "woman, late 30s, low alto, weathered, quiet resolve"},
    ],
}

_MOCK_NEW_CHARACTERS = {
    "characters": [
        {"name": "The Salt-Warden", "role": "antagonist",
         "personality": "implacable, sorrowful keeper of the deep archive",
         "physical_description": "a towering figure crusted in living salt, eyes like drowned "
                                 "lanterns, voice that echoes as if from underwater",
         "backstory": "Once human, bound by the Tidebound to guard the oldest memories until "
                      "they are claimed — a duty that has slowly calcified him into salt.",
         "quirks": "speaks only in the third person; weeps brine when he lies",
         "speaking_style": "slow, formal, archaic cadence with long pauses",
         "voice_brief": "man, ancient, deep resonant bass, slow and grave, hollow echo"},
    ]
}

_MOCK_OUTLINE = {
    "chapters": [
        {"index": 1, "title": "The First Salt-Memory", "beat": "Maren recovers a memory she shouldn't have."},
        {"index": 2, "title": "The Undertow's Offer", "beat": "A rival faction tempts her to let the past drown."},
        {"index": 3, "title": "What the Tide Keeps", "beat": "The true cost of reading a memory is revealed."},
        {"index": 4, "title": "Coll's Bargain", "beat": "Coll trades a memory he can't afford."},
        {"index": 5, "title": "The Sinking Archive", "beat": "The Archive collapses faster than predicted."},
        {"index": 6, "title": "Held Breath", "beat": "Maren decides which memories are worth her own."},
    ]
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
