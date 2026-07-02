"""Web server for the Loreweaver player + the braindump console.

Serves the static web player (feed.xml, index.html, assets) AND a small JSON API
so a writer can brain-dump freeform notes and have the system mine them into
canon lore + character sheets:

    POST /api/braindump   {"series": "...", "text": "..."}  -> generated entries
    GET  /api/world?series=...                              -> current bible
    GET  /api/series                                        -> known series ids

Everything runs through the same mock-aware tool wrappers, so it works offline
with zero API keys.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer

from . import settings
from .agents import braindumper
from .store import continuity, files
from .tools import rss


def _known_series() -> list[str]:
    """Series ids that have data on disk (a folder under DATA_DIR)."""
    out = []
    try:
        for child in sorted(settings.DATA_DIR.iterdir()):
            if child.is_dir() and not child.name.startswith(("_", ".")) \
                    and child.name != "artifacts":
                out.append(child.name)
    except FileNotFoundError:
        pass
    return out


_LORE_FIELDS = ("premise", "tone", "geography", "magic_system",
                "central_conflict", "visual_identity")


def _world_payload(series: str) -> dict:
    continuity.init()
    s = continuity.load_series(series)
    bible = (s or {}).get("world_bible") or {}
    # Surface the full character sheet (RPG stat block + sheet fields) so the
    # main player can show the same sheet the Studio edits. ensure_character_stats
    # fills sane defaults for characters authored before stats existed.
    characters = []
    for c in bible.get("characters", []):
        c = dict(c)
        files.ensure_character_stats(c)
        characters.append(c)
    return {
        "series": series,
        "title": bible.get("title", (s or {}).get("title", series)),
        "lore": {k: bible.get(k) for k in _LORE_FIELDS if bible.get(k)},
        "factions": bible.get("factions", []),
        "characters": characters,
    }


def _save_world(series: str, payload: dict) -> dict:
    """Merge edited title / lore / factions / characters into the world bible
    and persist it, preserving everything else on the series row. Rebuilds the
    feed so an edited title/premise shows up in the header. Returns the fresh
    world payload."""
    continuity.init()
    s = continuity.load_series(series) or {}
    bible = dict(s.get("world_bible") or {})

    lore = payload.get("lore") or {}
    for k in _LORE_FIELDS:
        if k in lore:
            bible[k] = lore[k]
    if "title" in payload:
        bible["title"] = payload["title"]
    if "factions" in payload:
        bible["factions"] = payload["factions"]
    if "characters" in payload:
        bible["characters"] = payload["characters"]

    continuity.save_series(
        series,
        title=payload.get("title", s.get("title") or "") or "",
        world_bible=bible,
        chapter_outline=s.get("chapter_outline") or [],
        voice_map=s.get("voice_map") or {},
        rolling_summary=s.get("rolling_summary") or "",
        current_chapter=s.get("current_chapter") or 0,
        cover_url=s.get("cover_url") or "",
    )
    if continuity.list_episodes(series):
        _rebuild_feed(series)
    return _world_payload(series)


def _lore_path(series_id: str) -> Path:
    """Path to a series' on-disk world bible (the lore master), without
    creating any directories (unlike files.lore_dir)."""
    return settings.DATA_DIR / files.slug(series_id) / "lore" / "world_bible.json"


def _reload_lore_from_disk(series_id: str) -> dict:
    """Repopulate the continuity DB for ONE series from its on-disk lore, then
    rebuild that series' vector index from the same file.

    The world bible at data/<series>/lore/world_bible.json is treated as the
    source of truth: its title / lore fields / factions / characters overwrite
    the DB row, while volatile progress fields already in the DB (chapter
    outline, voice map, rolling summary, chapter cursor, cover) are preserved.
    """
    from . import rag  # local import: pulls in embeddings, keep server start cheap

    path = _lore_path(series_id)
    if not path.exists():
        return {"series_id": series_id, "ok": False,
                "error": f"no world_bible.json on disk for '{series_id}'"}
    try:
        bible = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"series_id": series_id, "ok": False,
                "error": f"could not read world_bible.json: {e}"}

    existing = continuity.load_series(series_id) or {}
    continuity.save_series(
        series_id,
        title=bible.get("title") or existing.get("title") or series_id,
        world_bible=bible,
        chapter_outline=existing.get("chapter_outline") or [],
        voice_map=existing.get("voice_map") or {},
        rolling_summary=existing.get("rolling_summary") or "",
        current_chapter=existing.get("current_chapter") or 0,
        cover_url=existing.get("cover_url") or "",
    )
    indexed = rag.reindex_from_disk(series_id)
    return {
        "series_id": series_id,
        "ok": True,
        "title": bible.get("title") or series_id,
        "characters": len(bible.get("characters") or []),
        "lore_fields": [k for k in _LORE_FIELDS if bible.get(k)],
        "indexed_chunks": indexed,
    }


def _reload_all_lore() -> dict:
    """Repopulate every series that has a world_bible.json on disk."""
    continuity.init()
    results = [
        _reload_lore_from_disk(sid)
        for sid in _known_series()
        if _lore_path(sid).exists()
    ]
    return {"reloaded": results,
            "count": sum(1 for r in results if r.get("ok"))}


# ===========================================================================
# Generation management: background job runner + read/destructive helpers.
# ===========================================================================
class _LogWriter(io.TextIOBase):
    """Tees stdout/stderr writes into a job's log buffer (and the real stdout)."""

    def __init__(self, job: dict, real, lock: threading.Lock):
        self._job, self._real, self._lock = job, real, lock

    def write(self, s: str) -> int:
        if self._real:
            self._real.write(s)
        with self._lock:
            self._job["log"] = (self._job["log"] + s)[-20000:]  # cap memory
        return len(s)

    def flush(self):
        if self._real:
            self._real.flush()


class JobRunner:
    """One worker thread, FIFO queue. Serializing runs keeps the SQLite store
    and on-disk artifacts race-free while the UI polls for live progress."""

    def __init__(self):
        self._q: "queue.Queue[str]" = queue.Queue()
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def enqueue(self, kind, fn, *, series_id, chapter=None, label="") -> dict:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": kind, "series_id": series_id, "chapter": chapter,
               "label": label, "status": "queued", "log": "", "error": None,
               "created": time.time(), "started": None, "finished": None, "_fn": fn}
        with self._lock:
            self._jobs[jid] = job
        self._q.put(jid)
        return self._public(job)

    def _loop(self):
        import sys
        while True:
            jid = self._q.get()
            with self._lock:
                job = self._jobs[jid]
                job["status"], job["started"] = "running", time.time()
            writer = _LogWriter(job, sys.__stdout__, self._lock)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    job["_fn"]()
                with self._lock:
                    job["status"] = "done"
            except Exception as e:  # noqa: BLE001
                import traceback
                with self._lock:
                    job["status"], job["error"] = "error", str(e)
                    job["log"] += "\n" + traceback.format_exc()
            finally:
                with self._lock:
                    job["finished"] = time.time()
                self._q.task_done()

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def get(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
            return self._public(j) if j else None

    def list(self):
        with self._lock:
            jobs = [self._public(j) for j in self._jobs.values()]
        return sorted(jobs, key=lambda j: j["created"], reverse=True)


RUNNER = JobRunner()


def _run_new(series_id: str, fresh: bool = False):
    from .graph import run_pipeline
    run_pipeline(series_id, mode="new_series", fresh=fresh)


def _run_next(series_id: str, fresh: bool = False):
    from .graph import run_pipeline
    run_pipeline(series_id, mode="next_chapter", fresh=fresh)


def _assets_dir() -> Path:
    return settings.WEB_PLAYER_DIR / "assets"


def _rm(p: Path) -> None:
    """Best-effort delete of one path; never raises (a stubborn file must not
    abort a whole delete request or skip the feed rebuild)."""
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    except OSError as e:  # noqa: BLE001
        print(f"  [server] could not remove {p}: {e}")


def _delete_chapter_files(series_id: str, chapter: int) -> None:
    base = settings.DATA_DIR / files.slug(series_id)
    for t in (base / f"ch{chapter:02d}", base / "audio" / f"ch{chapter:02d}",
              base / "video" / f"ch{chapter:02d}.mp4"):
        _rm(t)
    for p in (base / "cover").glob(f"ch{chapter:02d}_*"):
        _rm(p)
    for p in _assets_dir().glob(f"{series_id}_ch{chapter:02d}.*"):
        _rm(p)


def _delete_series_files(series_id: str) -> None:
    _rm(settings.DATA_DIR / files.slug(series_id))
    for p in _assets_dir().glob(f"{series_id}_ch*"):
        _rm(p)
    _rm(settings.DATA_DIR / "_runstate" / f"{series_id}.json")


def _series_meta(series_id: str) -> dict:
    s = continuity.load_series(series_id)
    if s:
        bible = s.get("world_bible") or {}
        return {"title": s.get("title") or bible.get("title", "Loreweaver"),
                "premise": bible.get("premise", ""), "cover_url": s.get("cover_url", "")}
    eps = continuity.list_episodes(series_id)
    return {"title": eps[-1]["title"].split(" — ")[0] if eps else "Loreweaver",
            "premise": "", "cover_url": eps[-1].get("image_url", "") if eps else ""}


def _rebuild_feed(prefer_series: str | None = None) -> None:
    """feed.xml is single-series; prefer the just-edited series, else any series
    that still has episodes, else write an empty (valid) feed."""
    candidates = []
    if prefer_series and continuity.list_episodes(prefer_series):
        candidates.append(prefer_series)
    for s in continuity.list_series():
        if s["series_id"] not in candidates and s["episode_count"]:
            candidates.append(s["series_id"])
    if not candidates:
        (settings.WEB_PLAYER_DIR / "feed.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
            "  <channel>\n    <title>Loreweaver</title>\n"
            "    <description>No chapters yet.</description>\n  </channel>\n</rss>\n")
        return
    sid = candidates[0]
    rss.build_feed(_series_meta(sid), continuity.list_episodes(sid), settings.WEB_PLAYER_DIR)


def _series_detail(series_id: str) -> dict | None:
    s = continuity.load_series(series_id)
    eps = continuity.list_episodes(series_id)
    if not s and not eps:
        return None
    bible = (s or {}).get("world_bible") or {}
    voice_map = (s or {}).get("voice_map") or {}
    cast = [{"character": k, **(v if isinstance(v, dict) else {"voice_id": v})}
            for k, v in voice_map.items()]
    return {
        "series_id": series_id,
        "title": (s or {}).get("title") or bible.get("title") or series_id,
        "current_chapter": (s or {}).get("current_chapter", 0),
        "cover_url": (s or {}).get("cover_url", ""),
        "world_bible": bible,
        "rolling_summary": (s or {}).get("rolling_summary", ""),
        "chapter_outline": (s or {}).get("chapter_outline", []),
        "cast": cast,
        "episodes": eps,
    }


def _chapter_detail(series_id: str, chapter: int) -> dict:
    """Everything the Summary / Text / Audio tabs need for one chapter.

    `generated` is True once a chapter has been authored (text on disk). Until
    then we surface the framework beat so the UI can still show the plan.
    """
    s = continuity.load_series(series_id)
    outline = (s or {}).get("chapter_outline", []) or []
    beat = next((c for c in outline if c.get("index") == chapter), None) or {}
    ep = continuity.get_episode(series_id, chapter)
    text = files.latest_chapter_text(series_id, chapter)

    summary = ""
    if ep and ep.get("description"):
        # strip the boilerplate footer the publisher appends
        summary = ep["description"].split("\n\nAutonomously written")[0].strip()
    if not summary:
        summary = beat.get("synopsis") or beat.get("beat") or ""

    return {
        "series_id": series_id,
        "chapter": chapter,
        "title": (ep or {}).get("title") or beat.get("title") or f"Chapter {chapter}",
        "beat": beat,
        "generated": bool(text),
        "summary": summary,
        "text": text,
        "word_count": len(text.split()) if text else 0,
        "audio_url": (ep or {}).get("audio_url", ""),
        "image_url": (ep or {}).get("image_url", ""),
        "pub_date": (ep or {}).get("pub_date", ""),
    }


def _studio_workspace_payload(series_id: str, chapter: int) -> dict:
    """Everything the Studio chapter wizard needs: the saved workspace plus the
    roster, voice catalog, the chapter beat, and previous-chapter context."""
    from .store import workspace
    from .tools import elevenlabs

    s = continuity.load_series(series_id) or {}
    bible = s.get("world_bible") or {}
    voice_map = s.get("voice_map") or {}
    outline = s.get("chapter_outline") or []
    beat = next((c for c in outline if c.get("index") == chapter), None) or {
        "index": chapter, "title": f"Chapter {chapter}"}

    # Non-blocking: never wait on the live ElevenLabs API while loading a
    # workspace. Serves a cached/built-in catalog instantly and warms the real
    # one in the background.
    catalog = elevenlabs.cached_voices()
    cat_by_id = {v["voice_id"]: v for v in catalog}
    roster = []
    for c in bible.get("characters", []):
        if not c.get("name"):
            continue
        vid = c.get("voice_id") or voice_map.get(c["name"], "")
        files.ensure_character_stats(c)
        roster.append({
            "name": c["name"], "role": c.get("role", ""),
            "voice_id": vid,
            "voice_name": c.get("voice_name") or (cat_by_id.get(vid, {}).get("name", "")),
            "level": c["level"],
            "char_class": c["char_class"],
            "stats": dict(c["stats"]),
            "skills": list(c["skills"]),
            # full editable sheet fields for the Studio's sheet editor
            "personality": c.get("personality", ""),
            "speaking_style": c.get("speaking_style", ""),
            "physical_description": c.get("physical_description", ""),
            "backstory": c.get("backstory", ""),
            "quirks": c.get("quirks", ""),
            "voice_brief": c.get("voice_brief", ""),
        })

    # Backfill unified per-chapter character records (freezing base descriptions
    # into chapter-specific data) for any character that lacks one, preserving
    # existing states. No-op once records exist; makes no LLM calls.
    from . import studio_flow
    with contextlib.suppress(Exception):
        studio_flow.ensure_chapter_records(series_id, chapter)

    ws = workspace.load(series_id, chapter)
    # annotate each script line with whether its clip currently exists on disk
    for ln in ws.get("script") or []:
        clip = ln.get("clip") or ""
        ln["has_clip"] = bool(clip and Path(clip).exists())

    return {
        "series_id": series_id,
        "chapter": chapter,
        "beat": beat,
        "title": beat.get("title") or f"Chapter {chapter}",
        "roster": roster,
        "voices": [{"voice_id": v["voice_id"], "name": v.get("name", ""),
                    "gender": v.get("gender", ""), "accent": v.get("accent", "")}
                   for v in catalog],
        "has_prev_text": bool(files.latest_chapter_text(series_id, chapter - 1)) if chapter > 1 else False,
        "prev_summary": workspace.previous_chapter_summary(series_id, chapter),
        "workspace": ws,
    }


_CLIP_CT = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4"}


class Handler(SimpleHTTPRequestHandler):
    # Serve files out of the web player dir regardless of process cwd.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(settings.WEB_PLAYER_DIR), **kwargs)

    def log_message(self, fmt, *args):  # quieter console
        pass

    # ---- helpers ----------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _serve_clip(self, series_id: str, chapter: int, idx: int):
        """Stream a single rendered TTS clip for the review step."""
        from .store import workspace
        ws = workspace.load(series_id, chapter)
        script = ws.get("script") or []
        if idx < 0 or idx >= len(script):
            return self._send_json({"error": "line not found"}, 404)
        clip = script[idx].get("clip") or ""
        p = Path(clip)
        if not clip or not p.exists():
            alt = Path(clip.rsplit(".", 1)[0] + ".wav") if clip else None
            if alt and alt.exists():
                p = alt
            else:
                return self._send_json({"error": "clip not rendered"}, 404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CLIP_CT.get(p.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- routes -----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/series":
            # Source from the continuity store (same as the manage page) so the
            # dropdowns never miss a series that exists in the DB but has no
            # folder on disk yet. Merge with disk folders as a fallback.
            continuity.init()
            ids = [s["series_id"] for s in continuity.list_series()]
            for sid in _known_series():
                if sid not in ids:
                    ids.append(sid)
            return self._send_json({"series": sorted(ids)})
        if path == "/api/world":
            series = (self._query().get("series") or ["tidebound"])[0]
            return self._send_json(_world_payload(series))
        if path == "/api/prompts":
            from . import prompt_config
            return self._send_json({
                "writing": {
                    "sections": prompt_config.get_sections(),
                    "placeholders": prompt_config.placeholders(),
                },
                "model": {
                    **prompt_config.get_model_config(),
                    **prompt_config.model_options(),
                },
            })

        # ---- generation management (read) ----
        if path == "/api/gen/series":
            continuity.init()
            return self._send_json({"series": continuity.list_series()})
        if path == "/api/gen/jobs":
            return self._send_json({"jobs": RUNNER.list()})
        m = re.fullmatch(r"/api/gen/jobs/([0-9a-f]+)", path)
        if m:
            job = RUNNER.get(m.group(1))
            return self._send_json(job or {"error": "not found"}, 200 if job else 404)
        # ---- studio per-chapter workspace (read + clip streaming) ----
        m = re.fullmatch(r"/api/gen/series/([^/]+)/chapter/(\d+)/workspace", path)
        if m:
            continuity.init()
            return self._send_json(_studio_workspace_payload(m.group(1), int(m.group(2))))
        m = re.fullmatch(r"/api/gen/series/([^/]+)/chapter/(\d+)/clip/(\d+)", path)
        if m:
            return self._serve_clip(m.group(1), int(m.group(2)), int(m.group(3)))

        m = re.fullmatch(r"/api/gen/series/([^/]+)/chapter/(\d+)", path)
        if m:
            continuity.init()
            return self._send_json(_chapter_detail(m.group(1), int(m.group(2))))
        m = re.fullmatch(r"/api/gen/series/([^/]+)", path)
        if m:
            continuity.init()
            d = _series_detail(m.group(1))
            return self._send_json(d or {"error": "not found"}, 200 if d else 404)

        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        # ---- generation management (actions) ----
        if path.startswith("/api/gen/"):
            return self._do_gen_post(path)

        if path == "/api/world":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                series = (data.get("series") or "").strip()
                if not series:
                    return self._send_json({"error": "series is required"}, status=400)
                world = _save_world(series, data)
                return self._send_json({"ok": True, "world": world})
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)

        if path == "/api/prompts":
            from . import prompt_config
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                # Accept {"sections": {key: text}} (or legacy {"writing": {...}})
                # and/or {"model": {"provider", "claude_model"}}.
                updates = data.get("sections") or data.get("writing") or {}
                model_updates = data.get("model")
                if not isinstance(updates, dict):
                    return self._send_json({"error": "sections must be an object"}, 400)
                if model_updates is not None and not isinstance(model_updates, dict):
                    return self._send_json({"error": "model must be an object"}, 400)
                sections = prompt_config.save_sections(updates)
                if model_updates:
                    prompt_config.save_model_config(model_updates)
                return self._send_json({
                    "ok": True,
                    "writing": {
                        "sections": sections,
                        "placeholders": prompt_config.placeholders(),
                    },
                    "model": {
                        **prompt_config.get_model_config(),
                        **prompt_config.model_options(),
                    },
                })
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)

        if path != "/api/braindump":
            return self._send_json({"error": "not found"}, status=404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            series = (data.get("series") or "").strip()
            text = (data.get("text") or "").strip()
            if not series:
                return self._send_json({"error": "series is required"}, status=400)
            if not text:
                return self._send_json({"error": "text is required"}, status=400)
            result = braindumper.process_braindump(series, text)
            bible = result["world_bible"]
            return self._send_json({
                "series": series,
                "lore_updated": result["lore_updated"],
                "characters_added": result["characters_added"],
                "characters_updated": result["characters_updated"],
                "world": _world_payload(series),
                "mock": settings.mock_mode(),
            })
        except Exception as e:  # noqa: BLE001
            return self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)

    # ---- studio per-chapter wizard: POST actions -------------------------
    def _do_studio_post(self, path: str, body: dict):
        """Handle the Studio's stepped chapter routes. Returns a response object
        when the path matches a studio route, or None so the caller can fall
        through to the legacy generation routes."""
        base = r"/api/gen/series/([^/]+)/chapter/(\d+)"
        from . import studio_flow

        def job(kind, fn, sid, ch, label):
            j = RUNNER.enqueue(kind, fn, series_id=sid, chapter=ch, label=label)
            return self._send_json({"job": j}, 202)

        def sync(fn):
            try:
                return self._send_json({"ok": True, "workspace": fn()})
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 400)

        # ---- synchronous saves / edits ----
        m = re.fullmatch(base + r"/characters", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_characters(sid, ch, body.get("selected") or []))
        m = re.fullmatch(base + r"/states", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_states(sid, ch, body.get("states") or {}))
        m = re.fullmatch(base + r"/records", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_character_records(
                sid, ch, body.get("selected") or [], body.get("records") or {}))
        m = re.fullmatch(base + r"/plot", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_plot_points(sid, ch, body.get("plot_points") or []))
        m = re.fullmatch(base + r"/notes", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_special_notes(sid, ch, body.get("special_notes") or ""))
        m = re.fullmatch(base + r"/prev-chapters", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_included_prev_chapters(
                sid, ch, body.get("included_prev_chapters") or []))
        m = re.fullmatch(base + r"/draft", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_draft(sid, ch, body.get("draft") or ""))
        m = re.fullmatch(base + r"/voice", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_voice(
                sid, ch, body.get("character") or "", body.get("voice_id") or ""))
        m = re.fullmatch(base + r"/line", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return sync(lambda: studio_flow.set_line_speaker(
                sid, ch, int(body.get("idx", -1)), body.get("speaker") or ""))

        # ---- edit a chapter's outline beat (title + short description) ----
        m = re.fullmatch(base + r"/outline", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            try:
                outline = continuity.update_outline_beat(
                    sid, ch, {"title": body.get("title") or "",
                              "synopsis": body.get("synopsis") or ""})
            except ValueError as e:
                return self._send_json({"error": str(e)}, 404)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send_json({"ok": True, "chapter_outline": outline})

        # ---- remove a planned chapter from the outline ----
        m = re.fullmatch(base + r"/remove", path)
        if m:
            from .store import workspace
            sid, ch = m.group(1), int(m.group(2))
            s = continuity.load_series(sid)
            if not s:
                return self._send_json({"error": "unknown series"}, 404)
            # Removing here would renumber an already-generated chapter — refuse so
            # produced text/audio never falls out of sync with its number.
            if ch <= (s.get("current_chapter") or 0):
                return self._send_json(
                    {"error": f"can't remove chapter {ch}: it is already generated. "
                              f"Only planned chapters can be removed."}, 409)
            try:
                # tear down any in-progress workspace + on-disk artifacts for this
                # planned chapter, drop it from the plan, then slide the tail down.
                workspace.delete(sid, ch)
                _delete_chapter_files(sid, ch)
                continuity.delete_episode(sid, ch)
                outline = continuity.remove_outline_beat(sid, ch)
                workspace.shift_down_after(sid, ch)
                _rebuild_feed(sid)
            except ValueError as e:
                return self._send_json({"error": str(e)}, 404)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send_json({"ok": True, "removed": ch,
                                    "chapter_outline": outline})

        # ---- background jobs (LLM / TTS / media) ----
        m = re.fullmatch(base + r"/states/generate", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("states", lambda: studio_flow.generate_states(sid, ch), sid, ch,
                       f"States · {sid} ch{ch}")
        m = re.fullmatch(base + r"/plot/generate", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("plot", lambda: studio_flow.generate_plot_points(sid, ch), sid, ch,
                       f"Plot points · {sid} ch{ch}")
        m = re.fullmatch(base + r"/author", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("author", lambda: studio_flow.author_chapter(sid, ch), sid, ch,
                       f"Author · {sid} ch{ch}")
        m = re.fullmatch(base + r"/qa", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("qa", lambda: studio_flow.run_qa(sid, ch), sid, ch,
                       f"QA · {sid} ch{ch}")
        m = re.fullmatch(base + r"/cast", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("cast", lambda: studio_flow.cast_voices(sid, ch), sid, ch,
                       f"Cast voices · {sid} ch{ch}")
        m = re.fullmatch(base + r"/tts/(\d+)", path)
        if m:
            sid, ch, idx = m.group(1), int(m.group(2)), int(m.group(3))
            return job("tts1", lambda: studio_flow.render_one(sid, ch, idx), sid, ch,
                       f"Re-render line {idx} · {sid} ch{ch}")
        m = re.fullmatch(base + r"/tts", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("tts", lambda: studio_flow.render_all(sid, ch), sid, ch,
                       f"Render audio · {sid} ch{ch}")
        m = re.fullmatch(base + r"/combine", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("combine", lambda: studio_flow.combine(sid, ch), sid, ch,
                       f"Combine audio · {sid} ch{ch}")
        m = re.fullmatch(base + r"/cover", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("cover", lambda: studio_flow.make_cover(sid, ch), sid, ch,
                       f"Cover · {sid} ch{ch}")
        m = re.fullmatch(base + r"/publish", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("publish", lambda: studio_flow.publish(sid, ch), sid, ch,
                       f"Publish · {sid} ch{ch}")
        m = re.fullmatch(base + r"/update-sheets", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            return job("sheets", lambda: studio_flow.update_character_sheets(sid, ch), sid, ch,
                       f"Update character sheets · {sid} ch{ch}")

        return None

    # ---- generation management: POST actions -----------------------------
    def _do_gen_post(self, path: str):
        continuity.init()
        body = {}
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n:
            with contextlib.suppress(Exception):
                body = json.loads(self.rfile.read(n) or b"{}")

        # ---- studio per-chapter wizard actions ----
        handled = self._do_studio_post(path, body)
        if handled is not None:
            return handled

        # ---- add a new character to the series roster ----
        m = re.fullmatch(r"/api/gen/series/([^/]+)/character/add", path)
        if m:
            from . import studio_flow
            sid = m.group(1)
            name = (body.get("name") or "").strip()
            if not name:
                return self._send_json({"error": "name is required"}, 400)
            fields = body.get("fields") if isinstance(body.get("fields"), dict) else body
            try:
                c = studio_flow.add_character(sid, name, fields)
            except ValueError as e:
                return self._send_json({"error": str(e)}, 409)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send_json({"ok": True, "character": c})

        # ---- remove a character from the series roster ----
        m = re.fullmatch(r"/api/gen/series/([^/]+)/character/remove", path)
        if m:
            from . import studio_flow
            sid = m.group(1)
            name = (body.get("name") or "").strip()
            if not name:
                return self._send_json({"error": "name is required"}, 400)
            try:
                result = studio_flow.remove_character(sid, name)
            except ValueError as e:
                return self._send_json({"error": str(e)}, 404)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send_json({"ok": True, **result})

        # ---- rename a character across the whole series ----
        m = re.fullmatch(r"/api/gen/series/([^/]+)/character/rename", path)
        if m:
            from . import studio_flow
            sid = m.group(1)
            old_name = (body.get("old_name") or body.get("name") or "").strip()
            new_name = (body.get("new_name") or "").strip()
            if not old_name or not new_name:
                return self._send_json(
                    {"error": "old_name and new_name are required"}, 400)
            try:
                result = studio_flow.rename_character(sid, old_name, new_name)
            except ValueError as e:
                return self._send_json({"error": str(e)}, 409)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send_json({"ok": True, **result})

        # ---- save edits to a character's base sheet (series-level) ----
        m = re.fullmatch(r"/api/gen/series/([^/]+)/character", path)
        if m:
            from . import studio_flow
            sid = m.group(1)
            name = (body.get("name") or "").strip()
            if not name:
                return self._send_json({"error": "name is required"}, 400)
            fields = body.get("fields") if isinstance(body.get("fields"), dict) else body
            try:
                c = studio_flow.set_character_sheet(sid, name, fields)
            except ValueError as e:
                return self._send_json({"error": str(e)}, 404)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send_json({"ok": True, "character": c})

        if path == "/api/gen/reload-lore":
            # Rebuild the DB (+ vector index) from the lore on disk. With a
            # "series" in the body, reload just that one; otherwise reload all.
            sid = (body.get("series") or "").strip()
            try:
                if sid:
                    res = _reload_lore_from_disk(sid)
                    return self._send_json(res, 200 if res.get("ok") else 404)
                return self._send_json(_reload_all_lore())
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        if path == "/api/gen/series":
            sid = re.sub(r"[^A-Za-z0-9_-]+", "", (body.get("series_id") or "").strip())
            if not sid:
                return self._send_json({"error": "series_id required"}, 400)
            if continuity.load_series(sid) or continuity.list_episodes(sid):
                return self._send_json({"error": "series already exists"}, 409)
            job = RUNNER.enqueue("new", lambda: _run_new(sid), series_id=sid,
                                 chapter=1, label=f"New series · {sid}")
            return self._send_json({"job": job}, 202)

        m = re.fullmatch(r"/api/gen/series/([^/]+)/framework", path)
        if m:
            sid = m.group(1)
            if not continuity.load_series(sid):
                return self._send_json(
                    {"error": "no world bible yet — run a braindump first"}, 400)
            count = int(body.get("count") or 6)
            try:
                result = braindumper.build_framework(sid, count=count)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send_json({
                "series": sid,
                "chapter_outline": result.get("chapter_outline", []),
                "mock": settings.mock_mode(),
            })

        m = re.fullmatch(r"/api/gen/series/([^/]+)/next", path)
        if m:
            sid = m.group(1)
            job = RUNNER.enqueue("next", lambda: _run_next(sid), series_id=sid,
                                 label=f"Next chapter · {sid}")
            return self._send_json({"job": job}, 202)

        m = re.fullmatch(r"/api/gen/series/([^/]+)/chapter/(\d+)/insert", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            s = continuity.load_series(sid)
            if not s:
                return self._send_json(
                    {"error": "no world bible yet — run a braindump first"}, 400)
            position = (body.get("position") or "after").strip().lower()
            if position not in ("before", "after"):
                return self._send_json(
                    {"error": "position must be 'before' or 'after'"}, 400)
            new_index = ch if position == "before" else ch + 1
            # Inserting here would renumber an already-generated chapter — refuse
            # so produced text/audio never falls out of sync with its number.
            if new_index <= (s.get("current_chapter") or 0):
                return self._send_json(
                    {"error": f"can't insert {position} chapter {ch}: chapter "
                              f"{new_index} is already generated. Only planned "
                              f"chapters can be reordered."}, 409)
            beat = {
                "title": (body.get("title") or "").strip() or f"Chapter {new_index}",
                "synopsis": (body.get("synopsis") or "").strip(),
                "inserted": True,
            }
            try:
                outline = continuity.insert_outline_beat(sid, new_index, beat)
            except ValueError as e:
                return self._send_json({"error": str(e)}, 404)
            return self._send_json({
                "series": sid,
                "inserted_index": new_index,
                "chapter_outline": outline,
            })

        m = re.fullmatch(r"/api/gen/series/([^/]+)/chapter/(\d+)/regenerate", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))

            def _regen():
                _delete_chapter_files(sid, ch)
                continuity.delete_episode(sid, ch)
                continuity.set_current_chapter(sid, max(0, ch - 1))
                _run_new(sid, fresh=True) if ch <= 1 else _run_next(sid, fresh=True)
                _rebuild_feed(sid)

            job = RUNNER.enqueue("regenerate", _regen, series_id=sid, chapter=ch,
                                 label=f"Regenerate ch{ch:02d} · {sid}")
            return self._send_json({"job": job}, 202)

        return self._send_json({"error": "unknown route"}, 404)

    # ---- generation management: DELETE -----------------------------------
    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/gen/"):
            return self._send_json({"error": "not found"}, 404)
        continuity.init()

        m = re.fullmatch(r"/api/gen/series/([^/]+)/chapter/(\d+)", path)
        if m:
            sid, ch = m.group(1), int(m.group(2))
            removed = continuity.delete_episode(sid, ch)
            _delete_chapter_files(sid, ch)
            s = continuity.load_series(sid)
            if s and s["current_chapter"] == ch:
                remaining = [e["chapter"] for e in continuity.list_episodes(sid)]
                continuity.set_current_chapter(sid, max(remaining, default=0))
            _rebuild_feed(sid)
            return self._send_json({"deleted": removed, "series_id": sid, "chapter": ch})

        m = re.fullmatch(r"/api/gen/series/([^/]+)", path)
        if m:
            sid = m.group(1)
            continuity.delete_series(sid)
            _delete_series_files(sid)
            _rebuild_feed(None)
            return self._send_json({"deleted": True, "series_id": sid})

        return self._send_json({"error": "unknown route"}, 404)


def serve(port: int = 8000) -> None:
    settings.ensure_dirs()
    os.chdir(settings.WEB_PLAYER_DIR)
    ThreadingTCPServer.allow_reuse_address = True
    with ThreadingTCPServer(("", port), Handler) as httpd:
        mode = "MOCK (no keys)" if settings.mock_mode() else "LIVE"
        print(f"Loreweaver [{mode}]")
        print(f"  player             : http://localhost:{port}/")
        print(f"  studio (braindump) : http://localhost:{port}/studio.html")
        print(f"  manage generations : http://localhost:{port}/manage.html")
        httpd.serve_forever()
