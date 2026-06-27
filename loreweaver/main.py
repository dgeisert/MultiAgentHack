"""CLI entrypoint.

    python -m loreweaver.main new      --series tidebound   # spawn a world + chapter 1
    python -m loreweaver.main next     --series tidebound   # publish the next chapter
    python -m loreweaver.main serve                          # serve the web player + feed
    python -m loreweaver.main schedule --series tidebound    # register the daily drip
    python -m loreweaver.main reindex  --series tidebound    # (re)build the lore vector DB
    python -m loreweaver.main retrieve --series tidebound --query "salt memory dive"
"""
from __future__ import annotations

import argparse

from .graph import run_pipeline


def _print_results(state) -> None:
    r = state.get("publish_results", {})
    print("\n=== PUBLISHED ===")
    print(f"  world      : {(state.get('world_bible') or {}).get('title','?')}")
    print(f"  chapter    : {state.get('current_chapter')}")
    for k in ("feed", "self_hosted", "podcast", "youtube"):
        if r.get(k):
            print(f"  {k:<10} : {r[k]}")
    print(f"  audio file : {state.get('audio_path')}")


def cmd_new(args):
    _print_results(run_pipeline(args.series, mode="new_series", fresh=args.fresh))


def cmd_next(args):
    _print_results(run_pipeline(args.series, mode="next_chapter", fresh=args.fresh))


def cmd_serve(args):
    from .server import serve

    serve(args.port)


def cmd_schedule(args):
    from .scheduler import register_daily

    register_daily(args.series)


def cmd_reindex(args):
    from . import rag

    n = rag.reindex_from_disk(args.series)
    print(f"  reindexed {n} chunk(s) for '{args.series}' into the lore vector DB")


def cmd_retrieve(args):
    from . import rag

    beat = {"title": args.query, "beat": args.query}
    block = rag.retrieve_block(args.series, beat)
    print(block or "  (no chunks retrieved — is the series indexed? run 'reindex')")


def main():
    p = argparse.ArgumentParser(prog="loreweaver")
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("new", help="spawn a new world + publish chapter 1")
    pn.add_argument("--series", required=True)
    pn.add_argument("--fresh", action="store_true",
                    help="ignore any saved checkpoint and start this run clean")
    pn.set_defaults(func=cmd_new)

    px = sub.add_parser("next", help="publish the next chapter of an existing series")
    px.add_argument("--series", required=True)
    px.add_argument("--fresh", action="store_true",
                    help="ignore any saved checkpoint and start this run clean")
    px.set_defaults(func=cmd_next)

    ps = sub.add_parser("serve", help="serve the self-hosted feed + web player")
    ps.add_argument("--port", type=int, default=8000)
    ps.set_defaults(func=cmd_serve)

    pc = sub.add_parser("schedule", help="register the daily serialized drip")
    pc.add_argument("--series", required=True)
    pc.set_defaults(func=cmd_schedule)

    pr = sub.add_parser("reindex", help="(re)build the lore vector DB from world_bible.json")
    pr.add_argument("--series", required=True)
    pr.set_defaults(func=cmd_reindex)

    pq = sub.add_parser("retrieve", help="debug: show lore retrieved for a query")
    pq.add_argument("--series", required=True)
    pq.add_argument("--query", required=True)
    pq.set_defaults(func=cmd_retrieve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
