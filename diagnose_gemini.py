"""Isolated reproduction of loreweaver/tools/gemini.py::_live_text.

Run with the project's venv:
    ./.venv/bin/python diagnose_gemini.py

It mirrors the exact SDK call the Author agent makes, but strips away
LangGraph and the retry wrapper, and turns on verbose HTTP logging so we
can see what really happens on the wire.
"""
from __future__ import annotations

import logging
import os
import sys
import traceback

# 1) Make httpx / the SDK as loud as possible.
logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s %(message)s")
for noisy in ("httpx", "httpcore", "google_genai", "google.genai"):
    logging.getLogger(noisy).setLevel(logging.DEBUG)

# 2) Load .env exactly like settings.py does.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.0-flash")

print("=" * 70)
print(f"python         : {sys.version.split()[0]}")
print(f"model          : {MODEL!r}")
print(f"key length     : {len(API_KEY) if API_KEY else 0}")
print(f"key prefix     : {API_KEY[:5]!r}" if API_KEY else "key prefix     : <none>")
try:
    import importlib.metadata as m
    for pkg in ("google-genai", "httpx", "httpcore", "h11", "h2"):
        try:
            print(f"{pkg:<14} : {m.version(pkg)}")
        except Exception:
            print(f"{pkg:<14} : <not installed>")
except Exception:
    pass
print(f"HTTP(S)_PROXY  : {os.getenv('HTTPS_PROXY') or os.getenv('https_proxy') or '<none>'}")
print("=" * 70)

if not API_KEY:
    print("No GEMINI_API_KEY / GOOGLE_API_KEY found in environment/.env")
    sys.exit(1)

from google import genai
from google.genai import types

client = genai.Client(
    api_key=API_KEY,
    http_options=types.HttpOptions(timeout=180_000),
)

config = types.GenerateContentConfig(system_instruction=None, response_mime_type=None)

try:
    resp = client.models.generate_content(
        model=MODEL, contents="Say hello in three words.", config=config
    )
    print("\n=== SUCCESS ===")
    print(resp.text)
except Exception as e:  # noqa: BLE001
    print("\n=== FAILED ===")
    print(f"{type(e).__module__}.{type(e).__name__}: {e}")
    print("\n--- full traceback ---")
    traceback.print_exc()
    # Walk the exception chain so we see the *root* transport error.
    cur = e.__cause__ or e.__context__
    depth = 1
    while cur is not None and depth < 8:
        print(f"\n--- cause [{depth}] {type(cur).__module__}.{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
        depth += 1
