# Loreweaver — Autonomous Audiobook Writer & Publisher

**A multi-agent system that invents a fantasy world, writes it, voices it with a full cast, and publishes it as a serialized podcast — with no human in the loop.**

Hackathon: *tokens & Hacks — Multi-Agent Systems, London*
Track: ship an autonomous agent that does real work on the open web.

---

## 1. The one-liner

> Pull one trigger. Loreweaver scouts the open web for a fresh fantasy premise, builds a canon world bible, writes a chapter, casts and renders a multi-voice audio performance, paints cover art, and publishes the episode to a live podcast feed, YouTube, and its own web player — then schedules itself to drop the next chapter tomorrow.

It keeps publishing after the demo ends. That "it's still running" moment is the autonomy pitch.

---

## 2. Why this scores

| Judging criterion | How Loreweaver wins it |
|---|---|
| **Autonomy** | Single trigger → end-to-end run with zero approvals. A continuity store lets it serialize indefinitely on a schedule. It self-corrects via QA loops and retries. |
| **Idea** | Not a chatbot wrapper — it produces a *finished, distributable creative product* (a real podcast people can subscribe to) and keeps a multi-week story coherent. |
| **Technical implementation** | LangGraph state machine with branching, loops, retries, and persisted long-term memory; clean separation of agents and tools. |
| **Tool use** | 5 sponsor/real tools doing real work: **Gemini** (reasoning + text), **Gemini image** (cover art), **ElevenLabs** (voice design + multi-voice TTS), **Tavily** (web search/crawl), plus live **publishing** to RSS host + YouTube + self-hosted player. |
| **Presentation** | The output *is* the demo. Play a 60-second clip, show the live feed in a podcast app, show the YouTube video, show the web player. Tangible and emotional. |

---

## 3. Core design decisions (locked)

- **Orchestration:** LangGraph (Python), supervisor + typed shared state.
- **LLM:** Google Gemini (reasoning, lore, prose, script parsing) + Gemini image model (covers).
- **Audio:** ElevenLabs — Voice Design for character voices, multi-voice TTS for the performance.
- **Web/research:** Tavily search + crawl/extract.
- **Autonomy:** Fully autonomous. No approval gates in the happy path.
- **Cadence:** Serialized drip — one trigger spawns a world; a scheduled job publishes one new chapter per run (e.g. daily).
- **Genre:** Fantasy (world-first storytelling).
- **Publish targets (all three):** podcast RSS feed on a host, YouTube audio-with-cover video, and a self-hosted RSS feed + web player.

---

## 4. The agent roster

Nine specialist agents under one orchestrator. Each agent owns one job, one set of tools, and a typed contract (inputs → outputs). This separation is what makes the autonomy legible to judges and the failures recoverable.

### 4.0 Showrunner (Orchestrator / Supervisor)
- **Role:** Owns the LangGraph state, routing, scheduling, retries, and the global "series bible." Decides whether this run starts a new world or continues an existing one.
- **Tools:** LangGraph runtime, the continuity store (DB), the scheduler.
- **In:** trigger event (`new_series` | `next_chapter`), series_id (optional).
- **Out:** a completed, published chapter + updated series state + next scheduled run.

### 4.1 Scout (Idea & Research Agent) — *Tavily*
- **Role:** Finds a fresh, non-derivative premise. Searches the open web for trending themes, underused mythologies, public-domain folklore, naming conventions, and real-world textures (ecology, architecture, politics) to ground the fantasy. Returns 3–5 concept candidates with sourced inspiration, then self-selects the strongest using Gemini.
- **Tools:** Tavily `search` + `extract`/crawl; Gemini for synthesis and ranking.
- **In:** optional theme seed (else fully open).
- **Out:** `WorldConcept` — premise, tone, target audience, 3 differentiators, source links (kept for the show notes / attribution).

### 4.2 Loremaster (Worldbuilding Agent) — *Gemini*
- **Role:** Expands the chosen concept into a **World Bible**: geography, factions, magic system with hard rules, key characters with voice/personality notes, central conflict, and a season-length arc broken into chapter beats. This is the canon every later run reads from.
- **Tools:** Gemini (long-context generation); writes to the continuity store.
- **In:** `WorldConcept`.
- **Out:** `WorldBible` + `ChapterOutline[]` (the season plan) + a `CharacterRoster` with per-character voice briefs (age, timbre, accent, temperament) used later by casting.

### 4.3 Author (Writing Agent) — *Gemini*
- **Role:** Writes the prose for the current chapter, reading the World Bible, the chapter beat, and a rolling summary of prior chapters so continuity holds across weeks. Targets a consistent length (e.g. 1,500–2,500 words ≈ 12–18 min audio).
- **Tools:** Gemini; reads continuity store, writes the chapter draft + an updated rolling summary.
- **In:** `WorldBible`, current `ChapterBeat`, `RollingSummary`.
- **Out:** `ChapterDraft` (clean prose) + updated `RollingSummary`.

### 4.4 Casting Director (Script-Parsing & Voice-Mapping Agent) — *Gemini + ElevenLabs Voice Design*
- **Role:** The clever bit. Converts prose into a **performance script**: segments text into lines, attributes each line to a speaker (narrator vs. named character), resolves pronouns/"he said" attribution, and tags emotion/pace per line. Maps each character to an ElevenLabs voice — reusing a voice if the character already has one in the roster, or **designing a new voice** from the Loremaster's voice brief for new characters. Locks the voice→character mapping into the continuity store so a character sounds identical across chapters.
- **Tools:** Gemini (structured parsing → JSON), ElevenLabs Voice Design (create/lookup voices).
- **In:** `ChapterDraft`, `CharacterRoster`, existing `VoiceMap`.
- **Out:** `PerformanceScript` (ordered lines, each with `speaker`, `voice_id`, `emotion`, `text`), updated `VoiceMap`.

### 4.5 Audio Producer (TTS & Mastering Agent) — *ElevenLabs*
- **Role:** Renders each script line with its assigned voice and emotion, stitches the segments in order, inserts beats/pauses, and masters a single chapter MP3 (loudness-normalized, with optional intro/outro sting).
- **Tools:** ElevenLabs TTS (multi-voice); ffmpeg for stitch/normalize.
- **In:** `PerformanceScript`.
- **Out:** `chapter_audio.mp3` + duration + transcript.

### 4.6 Cover Artist (Cover-Art Agent) — *Gemini image*
- **Role:** Generates a series cover (once) and a per-chapter variant, from a prompt derived from the World Bible's visual identity + this chapter's key scene. Produces podcast-spec (3000×3000 square) and YouTube-spec (16:9 thumbnail) art.
- **Tools:** Gemini image generation; Pillow for resizing/spec compliance.
- **In:** `WorldBible` visual identity, current chapter summary.
- **Out:** `cover_square.png`, `cover_thumb.png`.

### 4.7 Publisher (Distribution Agent) — *RSS host + YouTube + self-hosted*
- **Role:** Does the real open-web work. (a) Updates the **podcast RSS feed** with a new `<item>` (audio enclosure, show notes incl. Scout's source links, cover) and pushes to the podcast host. (b) Renders an **audio-+-cover video** and uploads to **YouTube** via its API. (c) Writes the chapter into the **self-hosted feed** and redeploys the **web player**. Verifies each publish (HTTP 200 / valid feed / returned video ID) before reporting success.
- **Tools:** Podcast host API (e.g. Transistor/Buzzsprout) or direct RSS + object storage; YouTube Data API; a deploy/host target (Vercel/Netlify/S3) for the feed + player; ffmpeg for the video.
- **In:** `chapter_audio.mp3`, covers, transcript, show notes.
- **Out:** live URLs (podcast episode, YouTube video, web-player link) written back to series state.

### 4.8 Continuity Editor (QA / Guardrail Agent) — *Gemini*
- **Role:** The verification step that protects autonomy. Before audio, checks the draft against canon (no contradicting the magic rules, dead characters speaking, name drift), runs a content-safety pass, and verifies length. After publish, confirms the live artifacts resolve. On failure it routes back to the responsible agent with notes (bounded retries).
- **Tools:** Gemini; HTTP checks.
- **In:** any stage artifact + canon.
- **Out:** `pass` or `revise(reasons[], target_node)`.

---

## 5. Orchestration — the LangGraph

### 5.1 Shared state (typed)

```python
class SeriesState(TypedDict):
    series_id: str
    mode: Literal["new_series", "next_chapter"]
    world_concept: WorldConcept | None
    world_bible: WorldBible | None
    chapter_outline: list[ChapterBeat]
    current_chapter: int
    rolling_summary: str
    chapter_draft: str
    performance_script: list[ScriptLine]
    voice_map: dict[str, str]          # character -> elevenlabs voice_id
    audio_path: str | None
    covers: dict[str, str]
    publish_results: dict[str, str]    # target -> live url
    qa_notes: list[str]
    retries: dict[str, int]            # node -> attempts
```

Long-lived fields (`world_bible`, `voice_map`, `rolling_summary`, `current_chapter`) persist in the **continuity store** (SQLite/Postgres) so each scheduled run resumes the series exactly where it left off.

### 5.2 The graph

```
                        ┌─────────────┐
   trigger ───────────► │  Showrunner │  (decides new vs. continue)
                        └──────┬──────┘
            mode == new_series │  mode == next_chapter
                  ┌────────────┴───────────────┐
                  ▼                             ▼
             ┌─────────┐                 (load world_bible,
             │  Scout  │ (Tavily)         voice_map, summary
             └────┬────┘                  from continuity store)
                  ▼                             │
            ┌───────────┐                       │
            │ Loremaster│ (Gemini)              │
            └────┬──────┘                       │
                 └──────────────┬───────────────┘
                                ▼
                          ┌──────────┐
                          │  Author  │ (Gemini)
                          └────┬─────┘
                               ▼
                        ┌──────────────┐   revise
                        │ Continuity QA│◄────────┐
                        └──────┬───────┘         │
                          pass │   fail──────────┘ (back to Author, max N)
                               ▼
                     ┌───────────────────┐
                     │ Casting Director  │ (Gemini + ElevenLabs Voice Design)
                     └─────────┬─────────┘
                               ▼
                ┌──────────────┴──────────────┐
                ▼                              ▼
        ┌───────────────┐              ┌───────────────┐
        │ Audio Producer│ (ElevenLabs) │ Cover Artist  │ (Gemini image)
        └───────┬───────┘              └───────┬───────┘
                └──────────────┬───────────────┘   (parallel branch, joined)
                               ▼
                         ┌───────────┐
                         │ Publisher │  → RSS host + YouTube + self-hosted
                         └─────┬─────┘
                               ▼
                        ┌──────────────┐  fail → retry publish target
                        │ Continuity QA│  (verify live URLs)
                        └──────┬───────┘
                               ▼
                        ┌─────────────┐
                        │  Showrunner │ → persist state, schedule next run
                        └─────────────┘
```

Key graph properties judges will notice:
- **Conditional entry** (new world vs. continue) — real branching, not a linear pipeline.
- **Parallel fan-out/fan-in** for audio + cover, then join at Publisher.
- **Cyclic QA loops** with bounded retries (`retries[node] < N`), so the system self-heals instead of crashing.
- **Persisted memory** between scheduled runs = genuine serialized autonomy.

### 5.3 Autonomy & failure handling
- Every node is idempotent and writes artifacts to disk/object storage keyed by `series_id/chapter`, so a re-run resumes rather than duplicates.
- Bounded retries per node; on exhaustion the Showrunner publishes a "delayed" note and reschedules instead of dying.
- Each external call is wrapped with timeout + exponential backoff.
- The post-publish QA verifies the live artifacts actually resolve before the run is marked done.

### 5.4 Scheduling (serialized drip)
The Showrunner registers the next run on completion (cron-style, e.g. `0 9 * * *`). New world only on first run or explicit `new_series`; every later fire is `next_chapter`. This is the "still running tomorrow" hook for the pitch.

---

## 6. Sponsor & real-tool integration map

| Tool | Used by | What it actually does | Notes |
|---|---|---|---|
| **Gemini (text)** | Scout, Loremaster, Author, Casting, QA | Concept synthesis, world bible, prose, structured script parsing (JSON mode), canon/safety checks | Use a long-context model; JSON/structured output for the script parser. |
| **Gemini (image)** | Cover Artist | Series + per-chapter cover art | Resize to podcast 3000×3000 and 16:9 thumbnail with Pillow. |
| **ElevenLabs** | Casting Director, Audio Producer | Voice Design to mint per-character voices; multi-voice TTS to render the performance | Persist `voice_id` per character for cross-chapter consistency. |
| **Tavily** | Scout | Web search + extract/crawl for premises, mythology, real-world grounding, attribution links | Source URLs flow into show notes. |
| **Publishing (real web actions)** | Publisher | Podcast RSS host API; YouTube Data API upload; deploy self-hosted feed + web player | Three independent live destinations; each verified post-publish. |

That's **3+ sponsor tools** comfortably exceeded, each doing *real* work, plus three real open-web publish actions.

> Tip for the build: the cleanest "4th sponsor tool" framing is to count Gemini image generation separately from Gemini text, and to use a named sponsor hosting/deploy provider for the self-hosted player. Confirm which exact sponsors are on the London board and swap the host/deploy provider to a sponsor if one qualifies.

---

## 7. Data contracts (abridged)

```python
class ScriptLine(TypedDict):
    idx: int
    speaker: str          # "Narrator" | character name
    voice_id: str         # ElevenLabs voice
    emotion: str          # e.g. "tense", "wry", "grief"
    text: str

class WorldBible(TypedDict):
    title: str
    premise: str
    tone: str
    geography: str
    magic_system: str     # hard rules, costs, limits
    factions: list[dict]
    characters: list[CharacterBrief]   # incl. voice brief
    central_conflict: str
    visual_identity: str  # palette, motifs -> drives covers
```

---

## 8. Suggested repo layout

```
loreweaver/
  graph.py              # LangGraph wiring + state
  state.py              # TypedDicts / contracts
  agents/
    showrunner.py
    scout.py            # Tavily
    loremaster.py       # Gemini
    author.py           # Gemini
    casting.py          # Gemini + ElevenLabs voice design
    audio_producer.py   # ElevenLabs TTS + ffmpeg
    cover_artist.py     # Gemini image + Pillow
    publisher.py        # RSS host + YouTube + self-hosted
    continuity_qa.py    # Gemini + http checks
  tools/
    gemini.py  elevenlabs.py  tavily.py  youtube.py  rss.py  deploy.py
  store/                # continuity DB + artifact storage
  web_player/           # static feed + player, deployed each run
  scheduler.py
  main.py               # trigger entrypoint
```

---

## 9. Demo script (≈3 minutes)

1. **Trigger live** (`new_series`) — show the LangGraph lighting up node by node in the terminal/trace.
2. While it runs, narrate the agent roster against the live trace (autonomy + tech).
3. **Open the podcast app** — the new episode appears in a real subscribable feed. Play 30–45s of the multi-voice performance.
4. **Open YouTube** — same chapter, with the generated cover as the video.
5. **Open the web player** — self-hosted feed live.
6. **The kicker:** show the scheduled job. "It will write and publish chapter 2 tomorrow morning. Nobody touches it." 

---

## 10. Build order for the hackathon (de-risked)

1. Vertical slice first: Gemini → hard-coded short text → ElevenLabs single voice → save MP3. Prove audio.
2. Add Publisher to **one** target (self-hosted feed + player) and get a real subscribable URL. Prove "real web action" early.
3. Add Tavily Scout + Loremaster for a real generated world.
4. Add Casting Director multi-voice + Voice Design. This is the demo's emotional peak — protect time for it.
5. Add Cover Artist + the other two publish targets (RSS host, YouTube).
6. Wrap in LangGraph with QA loops + the scheduler last, once nodes work standalone.

Ship target after step 2; everything after is score-maximizing.

---

## 11. Open risks & mitigations
- **Voice consistency across chapters** → persist `voice_id` per character; never re-design an existing voice. (Mitigated in design.)
- **Long-run continuity drift** → rolling summary + canon QA gate. (Mitigated.)
- **YouTube API quota / OAuth friction** → pre-authorize before demo; have self-hosted player as the guaranteed-live fallback.
- **TTS latency for long chapters** → cap demo chapter length (~12 min) and render lines concurrently.
- **Content safety on autonomous output** → QA safety pass before audio; fantasy genre keeps it tame.
