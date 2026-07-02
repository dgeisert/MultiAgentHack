"""Studio flow — the manual, step-at-a-time chapter pipeline for the Studio UI.

The one-click graph (graph.run_pipeline) still exists for the autonomous / manage
flow. The Studio instead drives a chapter through discrete, human-reviewable
steps, each of which reads and writes the chapter workspace
(store.workspace) so the writer can edit between steps:

    1. characters   pick which roster characters appear in the chapter
    2. states       generate each character's CURRENT STATE (then edit)
    3. plot         generate the chapter's PLOT POINTS (then edit)
    4. author       write the chapter (states + plot + lore + prev chapter)
    5. qa           continuity / safety check (advisory)
    6. cast         parse prose into per-line speakers + assign voices
    7. assign       re-attribute any line to a different speaker
    8. tts          render each line to audio (regenerate any single line)
    9. combine      stitch the per-line clips into one chapter file
   10. cover        generate cover art
   11. publish      add the episode to the feed / player

Every function here is import-light at module load (heavy deps are pulled in
lazily) so the web server stays cheap to start.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import prompt_config, settings
from .store import continuity, files, workspace
from .tools import llm
from .tools.util import log


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _bible(series_id: str) -> dict:
    s = continuity.load_series(series_id) or {}
    return s.get("world_bible") or {}


def _roster(bible: dict) -> list[dict]:
    return [c for c in bible.get("characters", []) if c.get("name")]


def _character(bible: dict, name: str) -> dict:
    key = (name or "").strip().lower()
    return next((c for c in _roster(bible) if c.get("name", "").strip().lower() == key), {})


def _beat(series_id: str, chapter: int) -> dict:
    s = continuity.load_series(series_id) or {}
    outline = s.get("chapter_outline") or []
    return next((c for c in outline if c.get("index") == chapter), None) or {
        "index": chapter, "title": f"Chapter {chapter}", "beat": "advance the conflict"}


def _sheet_text(c: dict) -> str:
    return (
        f"{c.get('name','?')} ({c.get('role','')}). "
        f"Personality: {c.get('personality','')}. "
        f"Looks: {c.get('physical_description','')}. "
        f"Backstory: {c.get('backstory','')}. "
        f"Quirks: {c.get('quirks','')}. "
        f"Speaks: {c.get('speaking_style','')}."
    ).strip()


# Description fields frozen into a per-chapter character record at creation time.
_RECORD_DESC_FIELDS = ("role", "personality", "speaking_style",
                       "physical_description", "backstory", "quirks", "voice_brief")


def _base_record(bible: dict, name: str) -> dict:
    """Build a unified character record from the canonical base sheet — every
    description plus the current stat block — frozen as the chapter's starting
    point. Used for a character's first appearance (no prior chapter to pull
    forward from)."""
    c = _character(bible, name)
    files.ensure_character_stats(c)
    rec = {"name": name}
    for f in _RECORD_DESC_FIELDS:
        rec[f] = c.get(f, "")
    rec["level"] = c["level"]
    rec["char_class"] = c["char_class"]
    rec["stats"] = dict(c["stats"])
    rec["skills"] = list(c["skills"])
    rec["voice_id"] = c.get("voice_id", "")
    rec["voice_name"] = c.get("voice_name", "")
    rec["state"] = ""
    rec["source"] = "base_sheet"
    return rec


def _seed_record(series_id: str, chapter: int, bible: dict, name: str) -> dict:
    """The unified record this chapter should start from: pull forward the most
    recent prior chapter's record when one exists (carrying its descriptions,
    stats, skills and class), otherwise freeze the base sheet. State is cleared so
    the caller can (re)generate it."""
    found = workspace.find_previous_record(series_id, chapter, name)
    if found:
        prev_no, prev_rec = found
        rec = dict(prev_rec)
        rec["name"] = name
        rec["state"] = ""
        rec["seeded_from_chapter"] = prev_no
        rec["source"] = "previous_chapter"
        files.ensure_character_stats(rec)
        # guarantee every description field is present even on older records
        for f in _RECORD_DESC_FIELDS:
            rec.setdefault(f, "")
        return rec
    return _base_record(bible, name)


def _persist_chapter_records(series_id: str, chapter: int, records: dict) -> None:
    """Mirror the unified records to data/<series>/chNN/characters.json."""
    try:
        files.save_chapter_characters(series_id, chapter, records)
    except OSError:
        pass


def _chapter_character(ws: dict, bible: dict, name: str) -> dict:
    """The character data to use for THIS chapter: its frozen per-chapter record
    if one exists, else the live base sheet."""
    rec = (ws.get("character_records") or {}).get(name)
    return rec if isinstance(rec, dict) and rec else _character(bible, name)


def _copy_to_assets(src: str, name: str) -> str:
    """Copy a produced artifact into the web player's assets dir and return a
    same-origin relative URL the Studio can load directly."""
    if not src or not Path(src).exists():
        return ""
    assets = settings.WEB_PLAYER_DIR / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    dst_name = f"{name}{Path(src).suffix}"
    shutil.copy(src, assets / dst_name)
    return f"/assets/{dst_name}"


# --------------------------------------------------------------------------- #
# step 1 — character selection (synchronous)
# --------------------------------------------------------------------------- #
def set_characters(series_id: str, chapter: int, selected: list[str]) -> dict:
    bible = _bible(series_id)
    valid = {c["name"] for c in _roster(bible)}
    chosen = [n for n in (selected or []) if n in valid]
    ws = workspace.update(series_id, chapter,
                          selected_characters=chosen, stage="states")
    log("studio", f"ch{chapter}: selected {len(chosen)} character(s)")
    return ws


# --------------------------------------------------------------------------- #
# step 2 — current character states (LLM; run as a job)
# --------------------------------------------------------------------------- #
def generate_states(series_id: str, chapter: int) -> dict:
    """Build (or refresh) the unified per-chapter character records. Each record
    is seeded from the previous chapter's record when available (carrying its
    frozen descriptions + stats), otherwise from the base character sheet — so a
    chapter always captures the full base descriptions as of its creation.

    The narrative STATE at the start of the chapter is intentionally NOT
    auto-filled: neither seeding nor pull-forward writes a state, and the model is
    not invoked to fabricate one. Any state the user has already entered for this
    chapter is preserved; otherwise the field is left blank for the user to fill
    in manually. Records are mirrored to data/<series>/chNN/characters.json."""
    bible = _bible(series_id)
    ws = workspace.load(series_id, chapter)
    selected = ws.get("selected_characters") or []
    if not selected:
        # default to the whole roster if nothing was picked
        selected = [c["name"] for c in _roster(bible)]
        ws["selected_characters"] = selected

    existing_states = ws.get("character_states") or {}
    records = dict(ws.get("character_records") or {})

    for name in selected:
        rec = _seed_record(series_id, chapter, bible, name)
        # never auto-fill the start-of-chapter state; keep any user-entered value.
        prior_rec = records.get(name) or {}
        rec["state"] = (prior_rec.get("state") or existing_states.get(name) or "").strip()
        records[name] = rec
        log("studio", f"ch{chapter}: record seeded for {name} "
                      f"({'from ch' + str(rec.get('seeded_from_chapter')) if rec.get('source') == 'previous_chapter' else 'from base sheet'}); "
                      "state left for manual entry")

    ws["character_records"] = records
    ws["character_states"] = {n: r.get("state", "") for n, r in records.items()}
    ws["stage"] = "plot"
    _persist_chapter_records(series_id, chapter, records)
    return workspace.save(series_id, chapter, ws)


# Editable fields on a per-chapter record (everything the unified card exposes).
_RECORD_EDIT_TEXT = _RECORD_DESC_FIELDS + ("state",)


def _clean_record(bible: dict, name: str, base: dict, incoming: dict) -> dict:
    """Merge user-supplied fields onto an existing/blank record, validating the
    stat block, level, class and skills the same way the base sheet does."""
    rec = dict(base or _base_record(bible, name))
    rec["name"] = name
    incoming = incoming or {}

    for f in _RECORD_EDIT_TEXT:
        if isinstance(incoming.get(f), str):
            rec[f] = incoming[f].strip()

    if "level" in incoming:
        try:
            rec["level"] = max(1, int(incoming["level"]))
        except (TypeError, ValueError):
            pass
    if isinstance(incoming.get("char_class"), str) and incoming["char_class"].strip():
        rec["char_class"] = incoming["char_class"].strip()

    if isinstance(incoming.get("stats"), dict):
        rec.setdefault("stats", {})
        for key in files.ABILITY_KEYS:
            if key in incoming["stats"]:
                try:
                    rec["stats"][key] = max(1, int(incoming["stats"][key]))
                except (TypeError, ValueError):
                    pass

    if "skills" in incoming:
        sk = incoming["skills"]
        if isinstance(sk, str):
            sk = [x.strip() for x in sk.split(",")]
        if isinstance(sk, list):
            rec["skills"] = [str(x).strip() for x in sk if str(x).strip()]

    files.ensure_character_stats(rec)
    return rec


def set_character_records(series_id: str, chapter: int, selected: list[str],
                          records: dict) -> dict:
    """Persist the unified per-chapter character cards: the selection plus each
    character's edited record (descriptions, stats, skills, level/class, state).
    Mirrors to characters.json and keeps character_states in sync."""
    bible = _bible(series_id)
    valid = {c["name"] for c in _roster(bible)}
    chosen = [n for n in (selected or []) if n in valid]

    ws = workspace.load(series_id, chapter)
    existing = dict(ws.get("character_records") or {})
    for name, incoming in (records or {}).items():
        if name not in valid:
            continue
        existing[name] = _clean_record(bible, name, existing.get(name), incoming)

    ws["selected_characters"] = chosen
    ws["character_records"] = existing
    ws["character_states"] = {n: r.get("state", "") for n, r in existing.items()}
    ws["stage"] = "plot" if existing else ws.get("stage", "characters")
    _persist_chapter_records(series_id, chapter, existing)
    log("studio", f"ch{chapter}: saved {len(chosen)} selected character record(s)")
    return workspace.save(series_id, chapter, ws)


def ensure_chapter_records(series_id: str, chapter: int) -> dict:
    """Backfill the unified per-chapter records for any selected character that
    doesn't have one yet — freezing the base descriptions (backstory, physical
    description, personality, etc.) and stats into chapter-specific data, or
    pulling forward a prior chapter's record — while preserving any existing
    narrative state. Also fills missing description fields on older partial
    records. Idempotent: persists only when something actually changes, makes no
    LLM calls, so it is safe to run whenever a chapter is opened."""
    bible = _bible(series_id)
    ws = workspace.load(series_id, chapter)
    roster_names = {c["name"] for c in _roster(bible)}
    selected = ws.get("selected_characters") or []
    records = dict(ws.get("character_records") or {})
    states = ws.get("character_states") or {}
    targets = selected or list(records.keys())
    changed = False

    for name in targets:
        if name not in roster_names:
            continue
        rec = records.get(name)
        if isinstance(rec, dict) and rec:
            # top up any description fields a legacy/partial record is missing
            base = _base_record(bible, name)
            for f in _RECORD_DESC_FIELDS:
                if not rec.get(f) and base.get(f):
                    rec[f] = base[f]
                    changed = True
            records[name] = rec
            continue
        rec = _seed_record(series_id, chapter, bible, name)
        rec["state"] = (states.get(name) or rec.get("state") or "").strip()
        records[name] = rec
        changed = True

    if changed:
        ws["character_records"] = records
        ws["character_states"] = {n: r.get("state", "") for n, r in records.items()}
        _persist_chapter_records(series_id, chapter, records)
        workspace.save(series_id, chapter, ws)
    return records


def set_states(series_id: str, chapter: int, states: dict) -> dict:
    """Persist user-edited current states (back-compat). Updates the matching
    per-chapter records' state field and mirrors to characters.json."""
    bible = _bible(series_id)
    clean = {k: (v or "").strip() for k, v in (states or {}).items()}
    ws = workspace.load(series_id, chapter)
    records = dict(ws.get("character_records") or {})
    for name, st in clean.items():
        rec = records.get(name) or _base_record(bible, name)
        rec["state"] = st
        records[name] = rec
    ws["character_records"] = records
    ws["character_states"] = {n: r.get("state", "") for n, r in records.items()} or clean
    _persist_chapter_records(series_id, chapter, records)
    return workspace.save(series_id, chapter, ws)


# --------------------------------------------------------------------------- #
# step 3 — plot points (LLM; run as a job)
# --------------------------------------------------------------------------- #
def generate_plot_points(series_id: str, chapter: int) -> dict:
    bible = _bible(series_id)
    ws = workspace.load(series_id, chapter)
    beat = _beat(series_id, chapter)
    # Only the selected (checked) characters' states feed the plot; drop any
    # leftover state from characters that were unchecked.
    selected = ws.get("selected_characters") or [c["name"] for c in _roster(bible)]
    all_states = ws.get("character_states") or {}
    states = {n: all_states[n] for n in selected if n in all_states}
    prev_text = files.latest_chapter_text(series_id, chapter - 1) if chapter > 1 else ""
    s = continuity.load_series(series_id) or {}

    states_block = "\n".join(f"- {n}: {st}" for n, st in states.items()) or "(none)"
    prompt = (
        f"You are plotting chapter {chapter} ('{beat.get('title')}') of a serialized "
        "audiobook. List the MAJOR ACTIONS / events that happen in THIS chapter, in "
        "order, as concise bullet points (one action each). 4-8 bullets. "
        'Return JSON: {"plot_points": ["...", "..."]}.\n\n'
        f"CHAPTER BEAT:\n{json.dumps(beat)}\n\n"
        f"CHARACTER STATES AT CHAPTER START:\n{states_block}\n\n"
        f"STORY SO FAR:\n{s.get('rolling_summary','(none yet)')}\n\n"
        f"PREVIOUS CHAPTER (excerpt):\n{(prev_text or '(none)')[:3000]}\n\n"
        f"WORLD BIBLE (lore):\n{json.dumps({k: bible.get(k) for k in ('premise','magic_system','central_conflict') if bible.get(k)})}"
    )
    raw = llm.generate_json(prompt)
    points: list[str] = []
    if isinstance(raw, dict):
        for p in raw.get("plot_points", []) or []:
            if isinstance(p, str) and p.strip():
                points.append(p.strip())
    if not points:  # mock / empty fallback derived from the beat
        points = [beat.get("beat") or "Advance the central conflict."]

    ws["plot_points"] = points
    ws["stage"] = "author"
    log("studio", f"ch{chapter}: {len(points)} plot point(s) generated")
    return workspace.save(series_id, chapter, ws)


def set_plot_points(series_id: str, chapter: int, points: list[str]) -> dict:
    clean = [str(p).strip() for p in (points or []) if str(p).strip()]
    return workspace.update(series_id, chapter, plot_points=clean)


def set_special_notes(series_id: str, chapter: int, notes: str) -> dict:
    return workspace.update(series_id, chapter, special_notes=(notes or "").strip())


def set_included_prev_chapters(series_id: str, chapter: int,
                               chapters: list[int]) -> dict:
    """Persist which previous chapters' full text to include in the author prompt.
    Values are clamped to valid prior chapters (1 <= c < chapter)."""
    clean = sorted({c for c in (int(x) for x in (chapters or []))
                    if 1 <= c < chapter})
    return workspace.update(series_id, chapter, included_prev_chapters=clean)


# --------------------------------------------------------------------------- #
# step 4 — author the chapter (LLM; run as a job)
# --------------------------------------------------------------------------- #
def author_chapter(series_id: str, chapter: int) -> dict:
    from . import rag

    bible = _bible(series_id)
    ws = workspace.load(series_id, chapter)
    beat = _beat(series_id, chapter)
    s = continuity.load_series(series_id) or {}

    # Only the selected (checked) characters drive the prompt. Unchecked
    # characters may still have leftover records/states in the workspace, so we
    # filter everything by `selected` to keep them out of the chapter entirely.
    selected = ws.get("selected_characters") or [c["name"] for c in _roster(bible)]
    all_states = ws.get("character_states") or {}
    states = {n: all_states[n] for n in selected if n in all_states}
    plot = ws.get("plot_points") or []
    notes = (ws.get("special_notes") or "").strip()
    states_block = "\n".join(f"- {n}: {st}" for n, st in states.items()) or "(none)"
    plot_block = "\n".join(f"- {p}" for p in plot) or "(none)"
    notes_block = f"\nSPECIAL NOTES (honour these):\n{notes}\n" if notes else ""

    # Prefer the per-chapter frozen record (descriptions captured at chapter
    # creation, plus any edits) over the live base sheet.
    char_block = "\n".join(
        _sheet_text(_chapter_character(ws, bible, n))
        for n in selected if _chapter_character(ws, bible, n)) or "(none)"

    # Which previous chapters to feed in full. None = default (immediate previous
    # chapter only); an explicit list (possibly empty) overrides that choice.
    included = ws.get("included_prev_chapters")
    if included is None:
        included = [chapter - 1] if chapter > 1 else []
    included = sorted({c for c in (int(x) for x in included) if 1 <= c < chapter})
    if chapter <= 1:
        prev_block = "(this is the first chapter)"
    else:
        parts = []
        for c in included:
            text = files.latest_chapter_text(series_id, c)
            if text:
                parts.append(f"--- PREVIOUS CHAPTER ({c}) ---\n{text}")
        prev_block = "\n\n".join(parts) if parts else "(no previous chapters included)"

    retrieved = rag.retrieve_block(series_id, beat, s.get("rolling_summary", ""))
    retrieved_block = f"{retrieved}\n\n" if retrieved else ""

    # The lore block must NOT carry the base character roster: only this chapter's
    # Studio character section (char_block, from the per-chapter records) should
    # describe the characters, so per-chapter edits aren't overridden by the
    # story-wide base sheets.
    lore_only = {k: v for k, v in bible.items() if k != "characters"}

    # The prompt's static text sections live in prompt_config (editable from the
    # Settings page); here we supply the data that fills its {placeholders}.
    prompt = prompt_config.render_writing_prompt({
        "chapter": chapter,
        "title": beat.get("title") or f"Chapter {chapter}",
        "min_words": settings.CHAPTER_MIN_WORDS,
        "max_words": settings.CHAPTER_MAX_WORDS,
        "states_block": states_block,
        "plot_block": plot_block,
        "notes_block": notes_block,
        "retrieved_block": retrieved_block,
        "world_bible": json.dumps(lore_only),
        "char_block": char_block,
        "rolling_summary": s.get("rolling_summary", "(none yet)"),
        "prev_block": prev_block,
    })
    draft = llm.generate_text(prompt)

    rolling = llm.generate_text(
        "In 2-3 sentences, update the rolling 'story so far' summary to include this "
        "chapter, preserving names and key facts.\n\n"
        f"PREVIOUS SUMMARY:\n{s.get('rolling_summary','')}\n\nNEW CHAPTER:\n{draft[:4000]}"
    ).strip()

    saved = files.save_text_revision(series_id, chapter, draft)
    # persist the rolling summary so QA / cover / publish see it
    continuity.save_series(
        series_id,
        title=s.get("title") or bible.get("title") or series_id,
        world_bible=bible,
        chapter_outline=s.get("chapter_outline") or [],
        voice_map=s.get("voice_map") or {},
        rolling_summary=rolling,
        current_chapter=s.get("current_chapter") or 0,
        cover_url=s.get("cover_url") or "",
    )
    ws["draft"] = draft
    ws["stage"] = "qa"
    # a new draft invalidates any prior cast / audio
    ws["script"] = []
    ws["combined_url"] = ""
    ws["qa"] = None
    log("studio", f"ch{chapter}: authored ({len(draft.split())} words) -> {saved.name}")
    return workspace.save(series_id, chapter, ws)


def set_draft(series_id: str, chapter: int, draft: str) -> dict:
    """Persist a user-edited chapter draft (also writes a new on-disk revision)."""
    draft = draft or ""
    files.save_text_revision(series_id, chapter, draft)
    return workspace.update(series_id, chapter, draft=draft,
                            script=[], combined_url="", qa=None, stage="qa")


# --------------------------------------------------------------------------- #
# step 5 — QA (LLM; run as a job)
# --------------------------------------------------------------------------- #
def run_qa(series_id: str, chapter: int) -> dict:
    ws = workspace.load(series_id, chapter)
    draft = ws.get("draft") or files.latest_chapter_text(series_id, chapter)
    bible = _bible(series_id)
    words = len((draft or "").split())

    prompt = (
        "You are a continuity editor. Check this chapter against the world bible for: "
        "(a) contradictions of the magic rules, (b) dead/absent characters speaking, "
        "(c) name drift, (d) unsafe content. "
        'Return JSON {"verdict":"pass"|"revise","notes":[...]}.\n\n'
        f"WORLD BIBLE:\n{json.dumps(bible)}\n\nCHAPTER:\n{(draft or '')[:6000]}"
    )
    verdict = llm.generate_json(prompt)
    v = verdict.get("verdict", "pass") if isinstance(verdict, dict) else "pass"
    notes = verdict.get("notes", []) if isinstance(verdict, dict) else []
    if words < max(50, settings.CHAPTER_MIN_WORDS // 3):
        v = "revise"
        notes = [f"Chapter is only {words} words; expand toward target length."] + list(notes)

    qa = {"verdict": v, "notes": notes, "word_count": words}
    log("studio", f"ch{chapter}: QA {v} ({words} words)")
    return workspace.update(series_id, chapter, qa=qa, stage="cast")


# --------------------------------------------------------------------------- #
# step 6 — cast voices: parse prose -> per-line speakers + voices (job)
# --------------------------------------------------------------------------- #
def cast_voices(series_id: str, chapter: int) -> dict:
    from .agents import casting
    from .tools import elevenlabs

    ws = workspace.load(series_id, chapter)
    draft = ws.get("draft") or files.latest_chapter_text(series_id, chapter)
    if not draft:
        raise ValueError("nothing to cast — author the chapter first")

    bible = _bible(series_id)
    characters = {c["name"]: c for c in _roster(bible)}
    selected = ws.get("selected_characters") or list(characters)
    # Speakers eligible for dialogue attribution: selected, minus the narrator.
    dialogue_chars = [n for n in selected
                      if n in characters and not n.lower().startswith("narrator")]
    if not dialogue_chars:
        dialogue_chars = [n for n in characters if not n.lower().startswith("narrator")]

    segments = casting._segment(draft)
    lines: list[dict] = []
    for seg in segments:
        if seg["kind"] == "narration":
            lines.append({"idx": len(lines), "speaker": "Narrator",
                          "emotion": "calm", "text": seg["text"]})
            continue
        preceding, following = casting._context(draft, seg["start"], seg["end"])
        attr = casting._attribute_quote(seg["text"], preceding, following, dialogue_chars)
        lines.append({"idx": len(lines), "speaker": attr["speaker"],
                      "emotion": attr["emotion"], "text": seg["text"]})
    if not lines:
        lines = [{"idx": 0, "speaker": "Narrator", "emotion": "calm", "text": draft}]

    # distinct speakers, first-appearance order
    speakers, seen = [], set()
    for ln in lines:
        sp = ln["speaker"]
        if sp not in seen:
            seen.add(sp)
            speakers.append(sp)

    s = continuity.load_series(series_id) or {}
    voice_map = dict(s.get("voice_map") or {})
    catalog = elevenlabs.list_voices()
    casting._assign_unique_voices(speakers, characters, voice_map, catalog)
    for ln in lines:
        ln["voice_id"] = voice_map.get(ln["speaker"])
        ln["clip"] = ""
        ln["clip_v"] = 0

    # persist the voice map on the series AND onto each character sheet
    _persist_voice_map(series_id, voice_map, catalog)

    ws["script"] = lines
    ws["combined_url"] = ""
    ws["stage"] = "assign"
    log("studio", f"ch{chapter}: cast {len(lines)} line(s) across {len(speakers)} voice(s)")
    return workspace.save(series_id, chapter, ws)


def _voice_name(catalog: list[dict], voice_id: str) -> str:
    return next((v["name"] for v in catalog if v["voice_id"] == voice_id), "")


def _persist_voice_map(series_id: str, voice_map: dict, catalog: list[dict]) -> None:
    """Save the series voice map AND record each character's voice on their sheet,
    so a character keeps the same voice across chapters and the choice is visible."""
    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    for c in _roster(bible):
        vid = voice_map.get(c["name"])
        if vid:
            c["voice_id"] = vid
            c["voice_name"] = _voice_name(catalog, vid)
            files.save_character_sheet(series_id, c)
    continuity.save_series(
        series_id,
        title=s.get("title") or bible.get("title") or series_id,
        world_bible=bible,
        chapter_outline=s.get("chapter_outline") or [],
        voice_map=voice_map,
        rolling_summary=s.get("rolling_summary") or "",
        current_chapter=s.get("current_chapter") or 0,
        cover_url=s.get("cover_url") or "",
    )


_SHEET_TEXT_FIELDS = ("role", "personality", "speaking_style",
                      "physical_description", "backstory", "quirks", "voice_brief")


def set_character_sheet(series_id: str, name: str, fields: dict) -> dict:
    """Persist user edits to a character's base sheet (synchronous). Updates the
    character in the world bible AND rewrites their markdown sheet on disk, so the
    edit shows on the main player and carries into later chapters' generation.
    Returns the fully-resolved character dict."""
    from .tools import elevenlabs

    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    c = _character(bible, name)
    if not c:
        raise ValueError(f"unknown character '{name}'")
    fields = fields or {}

    for k in _SHEET_TEXT_FIELDS:
        if isinstance(fields.get(k), str):
            c[k] = fields[k].strip()

    if "level" in fields:
        try:
            c["level"] = max(1, int(fields["level"]))
        except (TypeError, ValueError):
            pass
    if isinstance(fields.get("char_class"), str):
        c["char_class"] = fields["char_class"].strip() or "Adventurer"

    if isinstance(fields.get("stats"), dict):
        c.setdefault("stats", {})
        for key in files.ABILITY_KEYS:
            if key in fields["stats"]:
                try:
                    c["stats"][key] = max(1, int(fields["stats"][key]))
                except (TypeError, ValueError):
                    pass

    if "skills" in fields:
        sk = fields["skills"]
        if isinstance(sk, str):
            sk = [x.strip() for x in sk.split(",")]
        if isinstance(sk, list):
            c["skills"] = [str(x).strip() for x in sk if str(x).strip()]

    if isinstance(fields.get("voice_id"), str) and fields["voice_id"].strip():
        c["voice_id"] = fields["voice_id"].strip()
        c["voice_name"] = _voice_name(elevenlabs.list_voices(), c["voice_id"])

    files.ensure_character_stats(c)
    files.save_character_sheet(series_id, c)

    continuity.save_series(
        series_id,
        title=s.get("title") or bible.get("title") or series_id,
        world_bible=bible,
        chapter_outline=s.get("chapter_outline") or [],
        voice_map=s.get("voice_map") or {},
        rolling_summary=s.get("rolling_summary") or "",
        current_chapter=s.get("current_chapter") or 0,
        cover_url=s.get("cover_url") or "",
    )
    log("studio", f"character sheet saved: {name}")
    return c


def add_character(series_id: str, name: str, fields: dict | None = None) -> dict:
    """Add a brand-new character to the series roster (the world bible's character
    list). Validates the name is non-empty and not already taken, builds a full
    sheet (defaults + any supplied fields), and persists the bible to the DB and
    on-disk lore plus a fresh markdown sheet. Returns the new character dict."""
    name = (name or "").strip()
    if not name:
        raise ValueError("character name is required")

    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    roster = bible.get("characters") or []
    if any((c.get("name", "").strip().lower() == name.lower())
           for c in roster if c.get("name")):
        raise ValueError(f"character '{name}' already exists")

    c: dict = {"name": name}
    fields = fields or {}
    for k in _SHEET_TEXT_FIELDS:
        if isinstance(fields.get(k), str):
            c[k] = fields[k].strip()
    if isinstance(fields.get("char_class"), str) and fields["char_class"].strip():
        c["char_class"] = fields["char_class"].strip()
    if "level" in fields:
        try:
            c["level"] = max(1, int(fields["level"]))
        except (TypeError, ValueError):
            pass
    files.ensure_character_stats(c)

    roster = list(roster) + [c]
    bible["characters"] = roster
    _persist_bible(series_id, s, bible)
    files.save_character_sheet(series_id, c)
    log("studio", f"character added: {name}")
    return c


def remove_character(series_id: str, name: str) -> dict:
    """Remove a character from the series roster (the world bible's character
    list). Drops their voice-map entry and deletes their markdown sheet, then
    persists the bible to the DB and on-disk lore. Per-chapter frozen records are
    left untouched; the character simply stops appearing in the roster. Returns
    the remaining roster names."""
    name = (name or "").strip()
    if not name:
        raise ValueError("character name is required")

    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    roster = bible.get("characters") or []
    key = name.lower()
    new_roster = [c for c in roster if c.get("name", "").strip().lower() != key]
    if len(new_roster) == len(roster):
        raise ValueError(f"unknown character '{name}'")
    bible["characters"] = new_roster

    voice_map = dict(s.get("voice_map") or {})
    voice_map.pop(name, None)
    _persist_bible(series_id, s, bible, voice_map=voice_map)

    # best-effort delete of the canonical markdown sheet
    try:
        sheet = files.characters_dir(series_id) / f"{files.slug(name)}.md"
        if sheet.exists():
            sheet.unlink()
    except OSError:
        pass

    log("studio", f"character removed: {name}")
    return {"removed": name, "roster": [c["name"] for c in new_roster]}


def rename_character(series_id: str, old_name: str, new_name: str) -> dict:
    """Rename a character everywhere it is keyed by name: the world bible roster,
    the series voice map, the canonical on-disk sheet, and every chapter's
    workspace (selected list, unified records, narrative states, script line
    speakers) plus the mirrored characters.json / per-chapter sheet files.

    Validates that the old character exists and the new name is non-empty and not
    already taken by a *different* character (a pure case change of the same
    character is allowed). Returns the renamed character dict + affected chapters."""
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name:
        raise ValueError("current character name is required")
    if not new_name:
        raise ValueError("new character name is required")

    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    roster = bible.get("characters") or []

    old_key = old_name.lower()
    target = next((c for c in roster if c.get("name", "").strip().lower() == old_key), None)
    if target is None:
        raise ValueError(f"unknown character '{old_name}'")

    # canonical existing name (as stored) — this is the exact key used in workspaces
    canonical_old = target.get("name", "").strip()

    # reject a collision with a *different* character
    new_key = new_name.lower()
    if new_key != old_key and any(
            c.get("name", "").strip().lower() == new_key for c in roster):
        raise ValueError(f"character '{new_name}' already exists")

    if new_name == canonical_old:
        return {"renamed": canonical_old, "to": new_name, "chapters": []}

    # 1) world bible roster
    target["name"] = new_name

    # 2) voice map key
    voice_map = dict(s.get("voice_map") or {})
    if canonical_old in voice_map:
        voice_map[new_name] = voice_map.pop(canonical_old)

    _persist_bible(series_id, s, bible, voice_map=voice_map)

    # 3) canonical markdown sheet: rewrite under the new name, drop the old file
    try:
        files.save_character_sheet(series_id, target)
        old_sheet = files.characters_dir(series_id) / f"{files.slug(canonical_old)}.md"
        if old_sheet.exists() and files.slug(canonical_old) != files.slug(new_name):
            old_sheet.unlink()
    except OSError:
        pass

    # 4) every chapter workspace
    affected: list[int] = []
    for ch in workspace.all_chapters(series_id):
        ws = workspace.load(series_id, ch)
        touched = False

        selected = ws.get("selected_characters") or []
        if canonical_old in selected:
            ws["selected_characters"] = [new_name if n == canonical_old else n
                                         for n in selected]
            touched = True

        records = dict(ws.get("character_records") or {})
        if canonical_old in records:
            rec = records.pop(canonical_old)
            if isinstance(rec, dict):
                rec["name"] = new_name
            records[new_name] = rec
            ws["character_records"] = records
            touched = True

        states = dict(ws.get("character_states") or {})
        if canonical_old in states:
            states[new_name] = states.pop(canonical_old)
            ws["character_states"] = states
            touched = True

        script = ws.get("script") or []
        for ln in script:
            if isinstance(ln, dict) and ln.get("speaker") == canonical_old:
                ln["speaker"] = new_name
                touched = True

        if touched:
            workspace.save(series_id, ch, ws)
            _persist_chapter_records(series_id, ch, ws.get("character_records") or {})
            # rename the frozen per-chapter sheet file if one was snapshotted
            try:
                cdir = files.chapter_characters_dir(series_id, ch)
                old_cs = cdir / f"{files.slug(canonical_old)}.md"
                if old_cs.exists() and files.slug(canonical_old) != files.slug(new_name):
                    new_cs = cdir / f"{files.slug(new_name)}.md"
                    old_cs.rename(new_cs)
            except OSError:
                pass
            affected.append(ch)

    log("studio", f"character renamed: {canonical_old} -> {new_name} "
                  f"(chapters {affected or '—'})")
    return {"renamed": canonical_old, "to": new_name, "character": target,
            "chapters": affected}


def _persist_bible(series_id: str, s: dict, bible: dict,
                   voice_map: dict | None = None) -> None:
    """Write the world bible back to the continuity DB and the on-disk lore master
    (world_bible.json + per-topic files) so a later reload-from-disk stays in
    sync. Preserves the series' volatile progress fields."""
    continuity.save_series(
        series_id,
        title=s.get("title") or bible.get("title") or series_id,
        world_bible=bible,
        chapter_outline=s.get("chapter_outline") or [],
        voice_map=s.get("voice_map") or {} if voice_map is None else voice_map,
        rolling_summary=s.get("rolling_summary") or "",
        current_chapter=s.get("current_chapter") or 0,
        cover_url=s.get("cover_url") or "",
    )
    try:
        files.save_lore(series_id, bible)
    except OSError:
        pass


def snapshot_chapter_sheets(series_id: str, chapter: int) -> list[str]:
    """Write a frozen copy of each appearing character's sheet — stats, skills and
    their tracked in-chapter state — into data/<series>/chNN/characters/, so each
    chapter keeps a record of where every character was at that point."""
    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    ws = workspace.load(series_id, chapter)
    states = ws.get("character_states") or {}
    selected = ws.get("selected_characters") or [c["name"] for c in _roster(bible)]
    written = []
    for name in selected:
        if name.lower().startswith("narrator"):
            continue
        c = _character(bible, name)
        if not c:
            continue
        files.ensure_character_stats(c)
        p = files.save_chapter_character_sheet(
            series_id, chapter, c, state_note=states.get(name, ""))
        written.append(str(p))
    log("studio", f"ch{chapter}: snapshotted {len(written)} character sheet(s)")
    return written


def set_voice(series_id: str, chapter: int, character: str, voice_id: str) -> dict:
    """Change the voice assigned to a character (synchronous). Updates the series
    voice map, the character sheet, and the current chapter's script lines."""
    from .tools import elevenlabs

    s = continuity.load_series(series_id) or {}
    voice_map = dict(s.get("voice_map") or {})
    voice_map[character] = voice_id
    catalog = elevenlabs.list_voices()
    _persist_voice_map(series_id, voice_map, catalog)

    ws = workspace.load(series_id, chapter)
    for ln in ws.get("script") or []:
        if ln.get("speaker") == character:
            ln["voice_id"] = voice_id
            ln["clip"] = ""  # voice changed -> clip is stale
    log("studio", f"ch{chapter}: {character} -> voice {voice_id}")
    return workspace.save(series_id, chapter, ws)


# --------------------------------------------------------------------------- #
# step 7 — re-attribute a single line's speaker (synchronous)
# --------------------------------------------------------------------------- #
def set_line_speaker(series_id: str, chapter: int, idx: int, speaker: str) -> dict:
    s = continuity.load_series(series_id) or {}
    voice_map = dict(s.get("voice_map") or {})
    ws = workspace.load(series_id, chapter)
    script = ws.get("script") or []
    if idx < 0 or idx >= len(script):
        raise ValueError(f"line {idx} out of range")

    # ensure the new speaker has a voice; assign one if needed
    if speaker not in voice_map:
        from .agents import casting
        from .tools import elevenlabs
        bible = _bible(series_id)
        characters = {c["name"]: c for c in _roster(bible)}
        catalog = elevenlabs.list_voices()
        casting._assign_unique_voices([speaker], characters, voice_map, catalog)
        _persist_voice_map(series_id, voice_map, catalog)

    script[idx]["speaker"] = speaker
    script[idx]["voice_id"] = voice_map.get(speaker)
    script[idx]["clip"] = ""  # speaker changed -> clip is stale
    ws["script"] = script
    ws["combined_url"] = ""
    log("studio", f"ch{chapter}: line {idx} -> {speaker}")
    return workspace.save(series_id, chapter, ws)


# --------------------------------------------------------------------------- #
# step 8 — render TTS (job): all lines, or a single line
# --------------------------------------------------------------------------- #
def _render_line(series_id: str, chapter: int, line: dict) -> str:
    from .tools import elevenlabs
    out = files.clip_path(series_id, chapter, line["idx"] + 1, line.get("speaker", "Narrator"))
    path = elevenlabs.tts(line["text"], line.get("voice_id"), out,
                          emotion=line.get("emotion", ""))
    return path


def render_all(series_id: str, chapter: int) -> dict:
    ws = workspace.load(series_id, chapter)
    script = ws.get("script") or []
    if not script:
        raise ValueError("no script to render — cast the chapter first")
    rendered = 0
    for ln in script:
        if ln.get("clip") and Path(ln["clip"]).exists():
            continue  # already rendered and still valid
        ln["clip"] = _render_line(series_id, chapter, ln)
        ln["clip_v"] = int(ln.get("clip_v", 0)) + 1
        rendered += 1
    ws["script"] = script
    ws["combined_url"] = ""  # clips changed -> must re-combine
    ws["stage"] = "review"
    log("studio", f"ch{chapter}: rendered {rendered} clip(s)")
    return workspace.save(series_id, chapter, ws)


def render_one(series_id: str, chapter: int, idx: int) -> dict:
    ws = workspace.load(series_id, chapter)
    script = ws.get("script") or []
    if idx < 0 or idx >= len(script):
        raise ValueError(f"line {idx} out of range")
    ln = script[idx]
    ln["clip"] = _render_line(series_id, chapter, ln)
    ln["clip_v"] = int(ln.get("clip_v", 0)) + 1
    ws["script"] = script
    ws["combined_url"] = ""
    log("studio", f"ch{chapter}: re-rendered line {idx}")
    return workspace.save(series_id, chapter, ws)


# --------------------------------------------------------------------------- #
# step 9 — combine clips into one chapter file (job)
# --------------------------------------------------------------------------- #
def combine(series_id: str, chapter: int) -> dict:
    from .tools import media

    ws = workspace.load(series_id, chapter)
    script = ws.get("script") or []
    clips = [ln.get("clip") for ln in script if ln.get("clip")]
    if not clips or len(clips) < len(script):
        raise ValueError("render every line before combining")

    out = files.chapter_audio_path(series_id, chapter)
    final = media.stitch_audio(clips, out)
    url = _copy_to_assets(final, f"{series_id}_ch{chapter:02d}")
    ws["combined_path"] = final
    ws["combined_url"] = url
    ws["stage"] = "cover"
    log("studio", f"ch{chapter}: combined -> {final}")
    return workspace.save(series_id, chapter, ws)


# --------------------------------------------------------------------------- #
# step 10 — cover art (job)
# --------------------------------------------------------------------------- #
def make_cover(series_id: str, chapter: int) -> dict:
    from .agents import cover_artist

    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    state = {"series_id": series_id, "current_chapter": chapter,
             "world_bible": bible, "rolling_summary": s.get("rolling_summary", "")}
    result = cover_artist.run(state)  # writes square/thumb under data/<s>/cover
    square = result.get("covers", {}).get("square", "")
    url = _copy_to_assets(square, f"{series_id}_ch{chapter:02d}_cover")
    ws = workspace.update(series_id, chapter, cover_url=url, stage="publish")
    log("studio", f"ch{chapter}: cover ready")
    return ws


# --------------------------------------------------------------------------- #
# step 11 — publish (job)
# --------------------------------------------------------------------------- #
def publish(series_id: str, chapter: int) -> dict:
    from .agents import publisher

    ws = workspace.load(series_id, chapter)
    combined = ws.get("combined_path") or files.chapter_audio_path(series_id, chapter)
    if not combined or not Path(combined).exists():
        # fall back to the .wav twin if needed
        alt = (combined or "").rsplit(".", 1)[0] + ".wav"
        combined = alt if Path(alt).exists() else combined
    if not combined or not Path(combined).exists():
        raise ValueError("combine the audio before publishing")

    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    cover_dir = files.cover_dir(series_id)
    square = cover_dir / f"ch{chapter:02d}_square.png"
    thumb = cover_dir / f"ch{chapter:02d}_thumb.png"
    covers = {}
    if square.exists():
        covers["square"] = str(square)
    if thumb.exists():
        covers["thumb"] = str(thumb)

    state = {
        "series_id": series_id, "current_chapter": chapter, "world_bible": bible,
        "chapter_draft": ws.get("draft") or files.latest_chapter_text(series_id, chapter),
        "rolling_summary": s.get("rolling_summary", ""),
        "audio_path": combined, "covers": covers, "world_concept": {},
    }
    result = publisher.run(state)
    # advance the chapter cursor so the player/feed reflect this chapter
    continuity.set_current_chapter(series_id, max(s.get("current_chapter") or 0, chapter))
    # ensure this chapter keeps a frozen record of its character sheets
    ws["sheet_snapshots"] = snapshot_chapter_sheets(series_id, chapter)
    ws["published"] = result.get("publish_results", {})
    ws["stage"] = "done"
    log("studio", f"ch{chapter}: published")
    return workspace.save(series_id, chapter, ws)


# --------------------------------------------------------------------------- #
# step 12 — update character sheets from the chapter (LLM; run as a job)
# --------------------------------------------------------------------------- #
def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _apply_progression(character: dict, prog: dict) -> dict:
    """Apply a model-proposed progression delta to a character's stat block and
    return a human-readable record of exactly what changed."""
    files.ensure_character_stats(character)
    before = {
        "level": character["level"],
        "char_class": character["char_class"],
        "stats": dict(character["stats"]),
        "skills": list(character["skills"]),
    }

    # An explicit level stated in the chapter prose wins over the earned delta.
    level_set = None
    if prog.get("level_set") is not None:
        try:
            level_set = int(prog["level_set"])
        except (TypeError, ValueError):
            level_set = None
    if level_set is not None:
        character["level"] = max(1, level_set)
    else:
        lvl_delta = 0
        try:
            lvl_delta = int(prog.get("level_delta") or 0)
        except (TypeError, ValueError):
            lvl_delta = 0
        character["level"] = max(1, character["level"] + lvl_delta)

    new_class = (prog.get("new_class") or "").strip() if isinstance(prog.get("new_class"), str) else ""
    if new_class:
        character["char_class"] = new_class

    deltas = prog.get("stat_deltas")
    deltas = deltas if isinstance(deltas, dict) else {}
    for key in files.ABILITY_KEYS:
        try:
            d = int(deltas.get(key) or 0)
        except (TypeError, ValueError):
            d = 0
        if d:
            character["stats"][key] = max(1, character["stats"][key] + d)

    gained = prog.get("skills_gained")
    gained = gained if isinstance(gained, list) else []
    character["skills"] = character["skills"] + [str(g).strip() for g in gained if str(g).strip()]
    files.ensure_character_stats(character)  # dedupe + clamp

    # build a concise change record
    changes = []
    if character["level"] != before["level"]:
        changes.append(f"Level {before['level']}→{character['level']}")
    if character["char_class"] != before["char_class"]:
        changes.append(f"Class {before['char_class']}→{character['char_class']}")
    for key in files.ABILITY_KEYS:
        if character["stats"][key] != before["stats"][key]:
            changes.append(f"{key.title()} {before['stats'][key]}→{character['stats'][key]}")
    new_skills = [s for s in character["skills"] if s not in before["skills"]]
    if new_skills:
        changes.append("Skills +" + ", ".join(new_skills))
    return {
        "changes": changes,
        "summary": (prog.get("summary") or "").strip(),
        "level": character["level"],
        "char_class": character["char_class"],
        "stats": dict(character["stats"]),
        "skills": list(character["skills"]),
    }


def update_character_sheets(series_id: str, chapter: int) -> dict:
    """Read the finished chapter and let the LLM advance each appearing
    character's RPG stat block (level, class, abilities, skills) based on what
    they did. Persists the updated bible + per-character sheets, and records the
    changes on the chapter workspace so the Studio can show them."""
    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    ws = workspace.load(series_id, chapter)
    draft = ws.get("draft") or files.latest_chapter_text(series_id, chapter)
    if not draft:
        raise ValueError("write the chapter before updating character sheets")

    roster = {c["name"]: c for c in _roster(bible)}
    selected = ws.get("selected_characters") or list(roster)
    targets = [n for n in selected
               if n in roster and not n.lower().startswith("narrator")]
    if not targets:
        targets = [n for n in roster if not n.lower().startswith("narrator")]

    chapter_records = ws.get("character_records") or {}
    updates: dict[str, dict] = {}
    for name in targets:
        c = roster[name]
        # Build progression on top of this chapter's record (which may carry
        # hand-edited stats/skills) rather than the stale base sheet, so card
        # edits are respected and flow back into the base sheet.
        rec0 = chapter_records.get(name)
        if isinstance(rec0, dict):
            if isinstance(rec0.get("stats"), dict):
                c["stats"] = dict(rec0["stats"])
            if isinstance(rec0.get("skills"), list):
                c["skills"] = list(rec0["skills"])
            if rec0.get("level"):
                c["level"] = rec0["level"]
            if rec0.get("char_class"):
                c["char_class"] = rec0["char_class"]
        files.ensure_character_stats(c)
        prompt = (
            "RPG PROGRESSION UPDATE. You are the game master for a serialized "
            "fantasy audiobook. Based ONLY on what this character actually did and "
            f"experienced in chapter {chapter}, propose how their RPG stats should "
            "advance. Be conservative: small, earned changes. Most chapters give "
            "at most +1 level and one or two ability points; award skills only when "
            "the character clearly demonstrated or learned them.\n\n"
            "IMPORTANT: if the chapter TEXT explicitly states the character reached "
            "a specific level (e.g. 'reached level 7') or took on a specific class "
            '(e.g. \'became a Battle Mage\'), copy that exact value over: set '
            '"level_set" to that absolute level and/or "new_class" to that class. '
            "Use these only when the prose actually mentions them; otherwise leave "
            'them null and express earned growth through "level_delta".\n\n'
            'Return JSON: {"level_delta": int (0-1 typical), '
            '"level_set": int or null (ONLY if the chapter text states an explicit level), '
            '"new_class": string or null (only if a real or explicitly stated class change happened), '
            '"stat_deltas": {ability: int delta}, "skills_gained": [string], '
            '"summary": "one sentence on why"}. '
            "Abilities are strength, wisdom, intelligence, dexterity, constitution, "
            "charisma, luck.\n\n"
            f"CHARACTER: {name}\n"
            f"CURRENT LEVEL: {c['level']}  CLASS: {c['char_class']}\n"
            f"CURRENT STATS: {json.dumps(c['stats'])}\n"
            f"CURRENT SKILLS: {json.dumps(c['skills'])}\n\n"
            f"CHAPTER {chapter} TEXT:\n{draft[:6000]}"
        )
        prog = llm.generate_json(prompt)
        if not isinstance(prog, dict):
            prog = {}
        record = _apply_progression(c, prog)
        files.save_character_sheet(series_id, c)
        updates[name] = record
        log("studio", f"ch{chapter}: updated {name} ({'; '.join(record['changes']) or 'no change'})")

    # persist the mutated bible back onto the series
    continuity.save_series(
        series_id,
        title=s.get("title") or bible.get("title") or series_id,
        world_bible=bible,
        chapter_outline=s.get("chapter_outline") or [],
        voice_map=s.get("voice_map") or {},
        rolling_summary=s.get("rolling_summary") or "",
        current_chapter=s.get("current_chapter") or 0,
        cover_url=s.get("cover_url") or "",
    )

    ws["sheet_updates"] = updates
    # reflect end-of-chapter progression onto this chapter's unified records so
    # the next chapter pulls forward the advanced stats, then re-mirror the JSON.
    records = dict(ws.get("character_records") or {})
    for name, c in roster.items():
        rec = records.get(name)
        if not rec:
            continue
        rec["level"] = c["level"]
        rec["char_class"] = c["char_class"]
        rec["stats"] = dict(c["stats"])
        rec["skills"] = list(c["skills"])
    ws["character_records"] = records
    _persist_chapter_records(series_id, chapter, records)
    # freeze a per-chapter snapshot of every appearing character's sheet
    ws["sheet_snapshots"] = snapshot_chapter_sheets(series_id, chapter)
    log("studio", f"ch{chapter}: character sheets updated for {len(updates)} character(s)")
    return workspace.save(series_id, chapter, ws)
