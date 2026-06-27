# 🜂 Loreweaver

**An autonomous multi-agent system that invents a fantasy world, writes it, voices it with a full cast, paints cover art, and publishes it as a serialized podcast — with no human in the loop.**

Built for *tokens & Hacks — Multi-Agent Systems, London*. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design and judging-criteria mapping.

```
trigger ─▶ Showrunner ─▶ Scout(Tavily) ─▶ Loremaster(Gemini) ─▶ Author(Gemini)
                                                                     │
                                              ┌── QA loop ◀──────────┘
                                              ▼
                            Casting(Gemini+ElevenLabs VoiceDesign)
                                              │
                          ┌───────────────────┴───────────────────┐
                  Audio Producer(ElevenLabs)            Cover Artist(Gemini image)
                          └───────────────────┬───────────────────┘
                                              ▼
                       Publisher ─▶ RSS feed + web player · podcast host · YouTube
                                              ▼
                       Showrunner ─▶ persist + schedule tomorrow's chapter
```

## Why it scores

- **Autonomy** — one trigger, zero approvals, end-to-end. A SQLite continuity store + scheduler let it serialize indefinitely. QA loops self-heal.
- **Tool use** — Gemini (text), Gemini (image), ElevenLabs (Voice Design + multi-voice TTS), Tavily (web research), plus three real publish targets.
- **Idea / Presentation** — the output *is* the demo: a real, subscribable podcast that keeps dropping chapters after the pitch ends.

## Quick start (zero keys — full mock demo)

```bash
pip install -r requirements.txt          # langgraph + Pillow are enough for mock mode
python -m loreweaver.main new --series tidebound     # spawn world + chapter 1
python -m loreweaver.main next --series tidebound    # chapter 2 (continuity preserved)
python -m loreweaver.main serve                      # open http://localhost:8000
```

With **no API keys**, every tool falls back to deterministic stubs: Gemini text/JSON is canned, covers are generated with Pillow, and ElevenLabs writes per-voice sine-tone WAVs that are stitched into a **real, playable chapter audio file** and a **valid podcast feed**. Flip to live by filling in `.env` (see `.env.example`).

## Going live

Set `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, `TAVILY_API_KEY` in `.env` to leave mock mode. Add `YOUTUBE_CLIENT_SECRETS`, `PODCAST_HOST_API_KEY`, and a real `deploy()` target (Vercel/Netlify/S3) to publish to the open web. `ffmpeg` enables real MP3 stitching + YouTube video render (a pure-Python WAV fallback runs without it).

## Resuming interrupted runs

Every run is checkpointed after each step (LangGraph `SqliteSaver`, keyed by
`series:chapter`). If a run dies partway — a flaky API call, a bad model name,
Ctrl-C — just run the same command again and it **picks up where it left off**,
re-running only the failed step onward:

```bash
python -m loreweaver.main new --series tidebound     # crashes at, say, the cover step
python -m loreweaver.main new --series tidebound     # resumes from the cover step
```

The audio step additionally skips any chapter clips already rendered, so an
interrupted narration doesn't re-spend TTS on lines it already produced. To
ignore the checkpoint and start a run clean, add `--fresh`.

## Serialized drip

```bash
python -m loreweaver.main schedule --series tidebound   # daily, via APScheduler or crontab
```

## Lore RAG (relevant lore per chapter)

The Author retrieves the characters and world-lore most relevant to the chapter
it's about to write from a **self-contained vector DB** and injects them at the
top of the prompt (the full canon still follows below). As a series grows to
dozens of characters, this keeps the model's attention on who and what actually
matter for the current beat.

- **Index** — the Loremaster chunks the world bible (one chunk per character, one
  per lore topic), embeds each, and stores them in `data/lore_vectors.db`. The
  Lorekeeper incrementally adds any new characters it discovers mid-series.
- **Embeddings** — Gemini `text-embedding-004` in live mode; a deterministic
  offline feature-hashing embedding in mock mode, so retrieval works with zero
  keys (and still respects real lexical overlap).
- **Store** — SQLite + numpy cosine search. No external vector-DB service; the
  `upsert`/`query` interface mirrors a real one so Chroma/pgvector can drop in.

```bash
python -m loreweaver.main reindex  --series tidebound                 # (re)build the index
python -m loreweaver.main retrieve --series tidebound --query "salt memory dive"
```

Knobs (env): `LOREWEAVER_RAG=0` disables it, `LOREWEAVER_RAG_CHARACTERS` /
`LOREWEAVER_RAG_LORE` set how many of each to retrieve, `LOREWEAVER_EMBED_MODEL`
picks the embedding model.

## Layout

```
loreweaver/
  graph.py            LangGraph wiring (+ sequential fallback)
  state.py            typed contracts
  settings.py         config + mock-mode switch
  rag.py              lore RAG: chunk · index · retrieve relevant lore per chapter
  agents/             the 9 agent nodes
  tools/              gemini · elevenlabs · tavily · youtube · rss · deploy · media · embeddings
  store/continuity.py SQLite long-term memory (world bible, voice map, summary)
  store/vectors.py    self-contained vector DB (sqlite + numpy cosine search)
  web_player/         self-hosted feed + player (served as a publish target)
  scheduler.py · main.py
```
