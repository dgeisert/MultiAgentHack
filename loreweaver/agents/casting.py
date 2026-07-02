"""Casting Director — script parsing + voice mapping (Gemini + ElevenLabs).

Parsing is QUOTE-DRIVEN, not LLM-driven: the prose is split deterministically so
that anything inside double quotes is dialogue and everything else is narration
(always the Narrator). The LLM is only consulted to attribute the SPEAKER of
each quoted line, and only sees that quote plus the surrounding sentences as
context — never the whole script.

After parsing it assigns each speaker an APPROPRIATE, UNIQUE premade ElevenLabs
voice (matched on gender/age/accent from the Loremaster's voice brief). No
custom voices are trained. The voice map is persisted so a character keeps the
same voice across every chapter, and so no two characters share a voice (until
the catalog is exhausted).
"""
from __future__ import annotations

import json
import re

from ..state import SeriesState
from ..tools import elevenlabs, llm
from ..tools.util import log

# Heuristic synonyms so a free-text brief maps onto catalog labels. Matched on
# whole words (not substrings) so e.g. "woman" never triggers the "man" cue.
_MALE = {"man", "male", "masculine", "he", "boy", "tenor", "baritone", "bass", "gruff"}
_FEMALE = {"woman", "female", "feminine", "she", "girl", "alto", "soprano", "matron"}
_OLD = {"old", "elder", "elderly", "aged", "ancient", "weathered", "grizzled", "middle"}
_YOUNG = {"young", "youth", "youthful", "boyish", "girlish", "teen", "teenage", "child"}

# The narrator is always cast with this voice. We match by catalog name, but
# fall back to the canonical voice id so the narrator is Jessica even if a live
# catalog fetch returns a list without an exact "Jessica" entry.
NARRATOR_VOICE_NAME = "Jessica"
NARRATOR_VOICE_ID = "cgSgspJ2msm6clMCkdW9"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (text or "").lower()))


def _score(brief: str, voice: dict, want_narrator: bool) -> int:
    toks = _tokens(brief)
    score = 0
    g = voice.get("gender", "")
    if g == "male" and ("male" in toks or toks & _MALE):
        score += 4
    if g == "female" and ("female" in toks or toks & _FEMALE):
        score += 4
    age = voice.get("age", "")
    if age:
        if "young" in age and toks & _YOUNG:
            score += 2
        if ("middle" in age or "old" in age) and toks & _OLD:
            score += 2
    accent = voice.get("accent", "").split("-")[0]
    if accent and accent in toks:
        score += 2
    desc_toks = _tokens(voice.get("description", ""))
    if want_narrator and (desc_toks & {"narration", "narrate", "story", "storytelling"}):
        score += 5
    # reward overlapping descriptive words (e.g. "calm", "soft", "deep")
    score += len({w for w in desc_toks if len(w) > 3} & toks)
    return score


def _assign_unique_voices(speakers, characters, voice_map, catalog) -> None:
    used = set(voice_map.values())
    for spk in speakers:
        if spk in voice_map:
            continue
        brief = characters.get(spk, {}).get("voice_brief", "") or ""
        want_narrator = spk.lower().startswith("narrator") or "narrat" in brief.lower()

        # Always cast the narrator with the designated voice (Jessica) if the
        # catalog has it, regardless of scoring or whether it's already used.
        if want_narrator:
            pin = next((v for v in catalog
                        if v.get("name", "").lower() == NARRATOR_VOICE_NAME.lower()), None)
            voice_id = pin["voice_id"] if pin else NARRATOR_VOICE_ID
            voice_map[spk] = voice_id
            used.add(voice_id)
            log("casting", f"cast {spk} -> {NARRATOR_VOICE_NAME} (narrator, pinned)")
            continue

        free = [v for v in catalog if v["voice_id"] not in used]
        pool = free or catalog  # only reuse once the catalog is exhausted
        best = max(pool, key=lambda v: _score(brief, v, want_narrator))

        voice_map[spk] = best["voice_id"]
        used.add(best["voice_id"])
        log("casting", f"cast {spk} -> {best['name']} "
                       f"({best.get('gender','?')}/{best.get('accent','?')})")


# Matches a quoted span using straight ("...") or curly ("...") double quotes.
# Content is non-greedy and may not itself contain a quote character.
_QUOTE_RE = re.compile(r'["“]([^"“”]+)["”]')

# Splits a chunk of prose into sentences on terminal punctuation. Used only to
# pull a little context around a quote, so it doesn't need to be perfect.
_SENT_RE = re.compile(r"(?<=[.!?])\s+")

# How many narration sentences of context to send on either side of a quote.
_CONTEXT_SENTENCES = 2


def _segment(text: str) -> list[dict]:
    """Split prose into ordered segments WITHOUT an LLM.

    Anything inside double quotes is a 'quote' segment (dialogue whose speaker
    must be attributed); everything else is 'narration' (always the Narrator).
    Each segment records its character span so we can pull surrounding context.
    """
    segments: list[dict] = []
    cursor = 0
    for m in _QUOTE_RE.finditer(text):
        before = text[cursor:m.start()]
        if before.strip():
            segments.append({"kind": "narration", "text": before.strip(),
                             "start": cursor, "end": m.start()})
        segments.append({"kind": "quote", "text": m.group(1).strip(),
                         "start": m.start(), "end": m.end()})
        cursor = m.end()
    tail = text[cursor:]
    if tail.strip():
        segments.append({"kind": "narration", "text": tail.strip(),
                         "start": cursor, "end": len(text)})
    return segments


def _context(text: str, start: int, end: int) -> tuple[str, str]:
    """Return (preceding, following) narration context for a quote span.

    Takes the last/first few *sentences* of prose immediately before and after
    the quote so the model can resolve 'he said'/'she said'-style attribution.
    """
    before = _SENT_RE.split(text[:start].strip())
    after = _SENT_RE.split(text[end:].strip())
    preceding = " ".join(s for s in before[-_CONTEXT_SENTENCES:] if s).strip()
    following = " ".join(s for s in after[:_CONTEXT_SENTENCES] if s).strip()
    return preceding, following


def _attribute_quote(quote: str, preceding: str, following: str,
                     characters: list[str]) -> dict:
    """Ask the model WHO speaks a single quoted line, given local context.

    Only the quote itself is attributed; surrounding prose is supplied purely as
    context. Returns {"speaker": ..., "emotion": ...}, defaulting to the first
    known character if the model can't decide.
    """
    prompt = (
        "Attribute the quoted line of dialogue to its speaker. Use ONLY the "
        "surrounding context to resolve 'he said'/'she said'-style attribution. "
        "Pick exactly one speaker from the known characters; never 'Narrator'. "
        "Also tag the line with a one-word emotion. "
        'Return JSON: {"speaker":"...","emotion":"..."}.\n\n'
        f"KNOWN CHARACTERS: {characters}\n\n"
        f"CONTEXT BEFORE: {preceding or '(none)'}\n"
        f'QUOTED LINE: "{quote}"\n'
        f"CONTEXT AFTER: {following or '(none)'}"
    )
    parsed = llm.generate_json(prompt)
    if not isinstance(parsed, dict):
        parsed = {}
    speaker = parsed.get("speaker") or (characters[0] if characters else "Unknown")
    return {"speaker": speaker, "emotion": parsed.get("emotion", "neutral")}


def run(state: SeriesState) -> dict:
    draft = state["chapter_draft"]
    bible = state.get("world_bible") or {}
    characters = {c["name"]: c for c in bible.get("characters", [])}
    # Speakers eligible for dialogue attribution (everyone except the narrator).
    dialogue_chars = [n for n in characters if not n.lower().startswith("narrator")]

    log("casting", "parsing prose into a performance script (quote-driven)")
    segments = _segment(draft)
    lines: list[dict] = []
    n_quotes = 0
    for seg in segments:
        if seg["kind"] == "narration":
            lines.append({"idx": len(lines), "speaker": "Narrator",
                          "emotion": "calm", "text": seg["text"]})
            continue
        # quote: attribute its speaker using only the surrounding sentences.
        preceding, following = _context(draft, seg["start"], seg["end"])
        attr = _attribute_quote(seg["text"], preceding, following, dialogue_chars)
        lines.append({"idx": len(lines), "speaker": attr["speaker"],
                      "emotion": attr["emotion"], "text": seg["text"]})
        n_quotes += 1

    if not lines:  # fallback: narrate the whole thing
        lines = [{"idx": 0, "speaker": "Narrator", "emotion": "calm", "text": draft}]
    log("casting", f"segmented into {len(lines)} lines "
                   f"({n_quotes} attributed quote(s), rest narration)")

    # Distinct speakers in first-appearance order.
    speakers, seen = [], set()
    for ln in lines:
        s = ln.get("speaker", "Narrator")
        if s not in seen:
            seen.add(s)
            speakers.append(s)

    voice_map = dict(state.get("voice_map") or {})
    catalog = elevenlabs.list_voices()
    _assign_unique_voices(speakers, characters, voice_map, catalog)

    for ln in lines:
        ln["voice_id"] = voice_map.get(ln.get("speaker", "Narrator"))

    log("casting", f"{len(lines)} lines across {len(speakers)} unique voices")
    return {"performance_script": lines, "voice_map": voice_map}
