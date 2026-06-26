"""CLI entrypoint.

    python -m loreweaver.main new      --series tidebound   # spawn a world + chapter 1
    python -m loreweaver.main next     --series tidebound   # publish the next chapter
    python -m loreweaver.main serve                          # serve the web player + feed
    python -m loreweaver.main schedule --series tidebound    # register the daily drip
"""
from __future__ import annotations

import argparse
import http.server
import socketserver

from . import settings
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
    _print_results(run_pipeline(args.series, mode="new_series"))


def cmd_next(args):
    _print_results(run_pipeline(args.series, mode="next_chapter"))


def cmd_serve(args):
    import os

    os.chdir(settings.WEB_PLAYER_DIR)
    port = args.port
    with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
        print(f"Serving web player + feed at http://localhost:{port}")
        httpd.serve_forever()


def cmd_schedule(args):
    from .scheduler import register_daily

    register_daily(args.series)


def main():
    p = argparse.ArgumentParser(prog="loreweaver")
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("new", help="spawn a new world + publish chapter 1")
    pn.add_argument("--series", required=True)
    pn.set_defaults(func=cmd_new)

    px = sub.add_parser("next", help="publish the next chapter of an existing series")
    px.add_argument("--series", required=True)
    px.set_defaults(func=cmd_next)

    ps = sub.add_parser("serve", help="serve the self-hosted feed + web player")
    ps.add_argument("--port", type=int, default=8000)
    ps.set_defaults(func=cmd_serve)

    pc = sub.add_parser("schedule", help="register the daily serialized drip")
    pc.add_argument("--series", required=True)
    pc.set_defaults(func=cmd_schedule)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
