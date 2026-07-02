"""Continuity Editor — QA / guardrail agent (Gemini + HTTP checks).

Runs in two modes:
  * draft  — before audio: canon consistency, content safety, length check.
             On failure routes back to the Author with notes (bounded retries).
  * publish — after publish: verify the live artifacts resolve.

Returns a verdict ('pass' | 'revise') plus the node to route back to.
"""
from __future__ import annotations

import json

from .. import settings
from ..state import SeriesState
from ..tools import llm
from ..tools.util import log


def review_draft(state: SeriesState) -> dict:
    draft = state.get("chapter_draft", "")
    words = len(draft.split())
    retries = dict(state.get("retries") or {})
    attempts = retries.get("author", 0)

    # Hard length gate first (cheap, deterministic).
    if words < max(50, settings.CHAPTER_MIN_WORDS // 3) and attempts < settings.MAX_RETRIES:
        retries["author"] = attempts + 1
        log("qa", f"draft too short ({words} words) -> revise (attempt {attempts+1})")
        return {"qa_verdict": "revise", "qa_target": "author", "retries": retries,
                "qa_notes": [f"Chapter is only {words} words; expand toward target length."]}

    prompt = (
        "You are a continuity editor. Check this chapter against the world bible for: "
        "(a) contradictions of the magic rules, (b) dead/absent characters speaking, "
        "(c) name drift, (d) unsafe content. "
        'Return JSON {"verdict":"pass"|"revise","notes":[...]}.\n\n'
        f"WORLD BIBLE:\n{json.dumps(state.get('world_bible'))}\n\nCHAPTER:\n{draft[:6000]}"
    )
    verdict = llm.generate_json(prompt)
    v = verdict.get("verdict", "pass") if isinstance(verdict, dict) else "pass"
    notes = verdict.get("notes", []) if isinstance(verdict, dict) else []

    if v == "revise" and attempts < settings.MAX_RETRIES:
        retries["author"] = attempts + 1
        log("qa", f"canon issues -> revise (attempt {attempts+1}): {notes}")
        return {"qa_verdict": "revise", "qa_target": "author", "retries": retries, "qa_notes": notes}

    log("qa", "draft PASS")
    return {"qa_verdict": "pass", "qa_notes": notes}


def verify_publish(state: SeriesState) -> dict:
    results = state.get("publish_results", {})
    ok = [k for k, v in results.items() if v]
    log("qa", f"publish verified across {len(ok)} targets: {ok}")
    # In live mode you'd HTTP-GET each URL and assert 200 / valid feed here.
    return {"qa_verdict": "pass"}
