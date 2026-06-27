"""LangGraph wiring for Loreweaver.

The graph branches on new-world vs. continue, fans out audio + cover rendering
in parallel, loops on QA failures with bounded retries, and verifies the live
artifacts after publishing. If LangGraph isn't installed, a faithful sequential
fallback executor runs the same node functions so the pipeline is always
runnable.
"""
from __future__ import annotations

from .agents import (audio_producer, author, casting, continuity_qa, cover_artist,
                     loremaster, lorekeeper, publisher, scout, showrunner)
from .state import SeriesState


# ---- routing predicates -----------------------------------------------------
def _route_entry(state: SeriesState) -> str:
    return "scout" if state.get("mode") == "new_series" else "author"


def _route_qa(state: SeriesState) -> str:
    # On pass, hand off to the Lorekeeper to sheet any new characters before casting.
    return "author" if state.get("qa_verdict") == "revise" else "lorekeeper"


# ---- LangGraph build --------------------------------------------------------
def build_graph(checkpointer=None):
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(SeriesState)

    g.add_node("enter", showrunner.enter)
    g.add_node("scout", scout.run)
    g.add_node("loremaster", loremaster.run)
    g.add_node("author", author.run)
    g.add_node("qa_draft", continuity_qa.review_draft)
    g.add_node("lorekeeper", lorekeeper.run)
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
    g.add_conditional_edges("qa_draft", _route_qa,
                            {"author": "author", "lorekeeper": "lorekeeper"})
    g.add_edge("lorekeeper", "casting")
    # parallel fan-out, then join at publisher
    g.add_edge("casting", "audio")
    g.add_edge("casting", "cover")
    g.add_edge("audio", "publisher")
    g.add_edge("cover", "publisher")
    g.add_edge("publisher", "qa_publish")
    g.add_edge("qa_publish", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)


# ---- sequential fallback (no langgraph), with JSON checkpoint resume --------
def _seq_checkpoint_path(story: str):
    from . import settings

    settings.ensure_dirs()
    d = settings.DATA_DIR / "_runstate"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{story}.json"


def _run_sequential(state: SeriesState) -> SeriesState:
    import json

    from .settings import MAX_RETRIES

    ckpt = _seq_checkpoint_path(state["series_id"])
    if ckpt.exists():  # resume an interrupted run
        try:
            state.update(json.loads(ckpt.read_text()))
            print(f"  [graph] resuming from checkpoint ({ckpt.name})")
        except Exception:  # noqa: BLE001
            pass

    def step(name, fn, *, skip_if):
        """Run a phase unless its output is already in the (resumed) state."""
        if skip_if and state.get(skip_if):
            print(f"  [graph] skip '{name}' (already done)")
            return
        state.update(fn(state) or {})
        ckpt.write_text(json.dumps(state, default=str))

    if not state.get("world_bible") and not state.get("current_chapter"):
        step("enter", showrunner.enter, skip_if=None)
    if state.get("mode") == "new_series":
        step("scout", scout.run, skip_if="world_concept")
        step("loremaster", loremaster.run, skip_if="world_bible")

    if not state.get("chapter_draft"):
        for _ in range(MAX_RETRIES + 1):
            state.update(author.run(state) or {})
            state.update(continuity_qa.review_draft(state) or {})
            ckpt.write_text(json.dumps(state, default=str))
            if state.get("qa_verdict") != "revise":
                break

    step("lorekeeper", lorekeeper.run, skip_if=None)
    step("casting", casting.run, skip_if="performance_script")
    step("audio", audio_producer.run, skip_if="audio_path")
    step("cover", cover_artist.run, skip_if="covers")
    step("publisher", publisher.run, skip_if="publish_results")
    state.update(continuity_qa.verify_publish(state) or {})
    state.update(showrunner.finalize(state) or {})
    ckpt.unlink(missing_ok=True)  # run completed cleanly
    return state


def _make_checkpointer():
    """Persistent LangGraph checkpointer so interrupted runs can resume."""
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        from . import settings

        settings.ensure_dirs()
        conn = sqlite3.connect(str(settings.DATA_DIR / "checkpoints.sqlite"),
                               check_same_thread=False)
        return SqliteSaver(conn)
    except Exception as e:  # noqa: BLE001
        print(f"  [graph] checkpointing unavailable ({e}); running without resume")
        return None


def _target_chapter(series_id: str, mode: str) -> int:
    from .store import continuity

    continuity.init()
    existing = continuity.load_series(series_id)
    if mode == "new_series" or existing is None:
        return 1
    return existing["current_chapter"] + 1


def run_pipeline(series_id: str, mode: str = "new_series", fresh: bool = False) -> SeriesState:
    """Single entrypoint used by main.py and the scheduler.

    Runs are checkpointed per (series, chapter). If a run is interrupted, calling
    again resumes from the last completed step. `fresh=True` forces a clean run.
    """
    import uuid

    init: SeriesState = {"series_id": series_id, "mode": mode}  # type: ignore[assignment]
    try:
        checkpointer = _make_checkpointer()
        app = build_graph(checkpointer)
    except ImportError:
        print("  [graph] langgraph not installed — using sequential fallback executor")
        if fresh:
            _seq_checkpoint_path(series_id).unlink(missing_ok=True)
        return _run_sequential(init)

    if checkpointer is None:  # no resume available; just run
        return app.invoke(init)

    target = _target_chapter(series_id, mode)
    thread_id = f"{series_id}:ch{target:02d}"
    if fresh:  # brand-new thread id => ignore any prior checkpoint
        thread_id += f":{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    snapshot = app.get_state(config)
    if snapshot and snapshot.next:  # an interrupted run is pending
        print(f"  [graph] resuming '{thread_id}' from step: {', '.join(snapshot.next)}")
        return app.invoke(None, config)
    return app.invoke(init, config)
