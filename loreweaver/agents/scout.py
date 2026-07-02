"""Scout — idea & research agent (Tavily + Gemini).

Searches the open web for fresh fantasy premises and real-world grounding, then
asks Gemini to synthesise the strongest, non-derivative WorldConcept. Source
links are carried through to the published show notes for attribution.
"""
from __future__ import annotations

from ..state import SeriesState
from ..tools import llm, tavily
from ..tools.util import log


_QUERIES = [
    "underused world mythologies for fantasy worldbuilding",
    "real-world phenomena that inspire original magic systems",
    "fantasy story premises without a chosen-one trope",
]


def run(state: SeriesState) -> dict:
    log("scout", "researching premises on the open web")
    findings, sources = [], []
    for q in _QUERIES:
        for r in tavily.search(q, max_results=3):
            findings.append(f"- {r['title']}: {r['content'][:200]}")
            sources.append(r["url"])

    prompt = (
        "You are a fantasy development editor. Using the web research below, invent ONE "
        "fresh, non-derivative fantasy concept. Avoid chosen-one plots and Tolkien pastiche.\n\n"
        f"RESEARCH:\n{chr(10).join(findings)}\n\n"
        "Return JSON for a world CONCEPT with keys: title, premise, tone, audience, "
        "differentiators (3 strings), sources (array of the most relevant URLs)."
    )
    concept = llm.generate_json(prompt)
    if isinstance(concept, dict):
        concept.setdefault("sources", sources[:4])
    log("scout", f"selected concept: {concept.get('title', '?')}")
    return {"world_concept": concept}
