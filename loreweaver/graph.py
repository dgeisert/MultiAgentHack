"""LangGraph wiring for Loreweaver.

The graph branches on new-world vs. continue, fans out audio + cover rendering
in parallel, loops on QA failures with bounded retries, and verifies the live
artifacts after publishing. If LangGraph isn't installed, a faithful sequential
fallback executor runs the same node functions so the pipeline is always
runnable.
"""
from __future__ import annotations

from .agents import (audio_producer, author, casting, continuity_qa, cover_artist,
                     loremaster, publisher, scout, showrunner)
from .state import SeriesState


# ---- routing predicates -----------------------------------------------------
def _route_entry(state: SeriesState) -> str:
    return "scout" if state.get("mode") == "new_series" else "author"


def _route_qa(state: SeriesState) -> str:
    return "author" if state.get("qa_verdict") == "revise" else "casting"


# ---- LangGraph build --------------------------------------------------------
def build_graph():
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(SeriesState)

    g.add_node("enter", showrunner.enter)
    g.add_node("scout", scout.run)
    g.add_node("loremaster", loremaster.run)
    g.add_node("author", author.run)
    g.add_node("qa_draft", continuity_qa.review_draft)
    g.add_node("casting", casting.run)
    g.add_node("audio", audio_producer.run)
    g.add_node("cover", cover_artist.run)
    g.add_node("publisher", publisher.run)
    g.add_node("qa_publish", continuity_qa.verify_publish)
    g.add_node("finalize", showrunner.finalize)

    g.add_edge(START, "enter")
    g.add_conditional_edges("enter", _route_entry, {"scout": "scout", "author": "author"})
    g.add_edge("scout", "loremaster")
    g.add_edge("loremaster", "author")
    g.add_edge("author", "qa_draft")
    g.add_conditional_edges("qa_draft", _route_qa, {"author": "author", "casting": "casting"})
    # parallel fan-out, then join at publisher
    g.add_edge("casting", "audio")
    g.add_edge("casting", "cover")
    g.add_edge("audio", "publisher")
    g.add_edge("cover", "publisher")
    g.add_edge("publisher", "qa_publish")
    g.add_edge("qa_publish", "finalize")
    g.add_edge("finalize", END)

    return g.compile()


# ---- sequential fallback (no langgraph) ------------------------------------
def _run_sequential(state: SeriesState) -> SeriesState:
    from .settings import MAX_RETRIES

    def merge(s, upd):
        s.update(upd or {})
        return s

    state = merge(state, showrunner.enter(state))
    if state.get("mode") == "new_series":
        state = merge(state, scout.run(state))
        state = merge(state, loremaster.run(state))

    for _ in range(MAX_RETRIES + 1):
        state = merge(state, author.run(state))
        state = merge(state, continuity_qa.review_draft(state))
        if state.get("qa_verdict") != "revise":
            break

    state = merge(state, casting.run(state))
    state = merge(state, audio_producer.run(state))   # parallel branches, run in sequence
    state = merge(state, cover_artist.run(state))
    state = merge(state, publisher.run(state))
    state = merge(state, continuity_qa.verify_publish(state))
    state = merge(state, showrunner.finalize(state))
    return state


def run_pipeline(series_id: str, mode: str = "new_series") -> SeriesState:
    """Single entrypoint used by main.py and the scheduler."""
    init: SeriesState = {"series_id": series_id, "mode": mode}  # type: ignore[assignment]
    try:
        app = build_graph()
        return app.invoke(init)
    except ImportError:
        print("  [graph] langgraph not installed — using sequential fallback executor")
        return _run_sequential(init)
