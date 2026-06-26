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

## Serialized drip

```bash
python -m loreweaver.main schedule --series tidebound   # daily, via APScheduler or crontab
```

## Layout

```
loreweaver/
  graph.py            LangGraph wiring (+ sequential fallback)
  state.py            typed contracts
  settings.py         config + mock-mode switch
  agents/             the 9 agent nodes
  tools/              gemini · elevenlabs · tavily · youtube · rss · deploy · media
  store/continuity.py SQLite long-term memory (world bible, voice map, summary)
  web_player/         self-hosted feed + player (served as a publish target)
  scheduler.py · main.py
```
