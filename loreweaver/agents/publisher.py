"""Publisher — distribution agent. The real open-web work.

Publishes the chapter to three targets: (1) a self-hosted RSS feed + web player,
(2) a podcast host, and (3) YouTube as an audio-+-cover video. Records the
episode in the continuity store so the feed accumulates over the season.
"""
from __future__ import annotations

import datetime as dt
import hashlib

from .. import settings
from ..state import SeriesState
from ..store import continuity, files
from ..tools import deploy, media, rss, youtube
from ..tools.util import log


def run(state: SeriesState) -> dict:
    sid = state["series_id"]
    chapter = state.get("current_chapter", 1)
    bible = state.get("world_bible") or {}
    title = f"{bible.get('title','Loreweaver')} — Chapter {chapter}"
    audio = state.get("audio_path", "")
    cover = state.get("covers", {}).get("square", "")
    thumb = state.get("covers", {}).get("thumb", cover)

    notes_sources = (state.get("world_concept") or {}).get("sources", [])

    # Per-chapter synopsis for the show notes / Summary tab. Prefer a fresh
    # synopsis of THIS chapter's text; fall back to the rolling summary, then the
    # premise, so the field is always populated even in trimmed/mock runs.
    synopsis = ""
    draft = state.get("chapter_draft", "")
    if draft:
        try:
            from ..tools import gemini
            synopsis = gemini.generate_text(
                "Write a 2-3 sentence spoiler-light synopsis of this audiobook chapter "
                "for podcast show notes. Present tense, no preamble.\n\n"
                f"CHAPTER {chapter} TEXT:\n{draft[:6000]}"
            ).strip()
        except Exception as e:  # noqa: BLE001
            log("publisher", f"synopsis generation failed ({e}); using fallback")
    if not synopsis:
        synopsis = state.get("rolling_summary", "") or bible.get("premise", "")

    show_notes = (
        f"{synopsis}\n\nAutonomously written, voiced, and published by "
        f"Loreweaver agents.\nInspiration: {', '.join(notes_sources)}"
    )

    results: dict[str, str] = {}

    # (1) Self-hosted: copy assets into the web player, build feed, deploy.
    asset_urls = deploy.publish_assets(audio, cover, sid, chapter)
    guid = hashlib.sha1(f"{sid}:{chapter}".encode()).hexdigest()
    episode = {
        "chapter": chapter, "title": title, "description": show_notes,
        "audio_url": asset_urls.get("audio_url", ""),
        "image_url": asset_urls.get("image_url", ""),
        "guid": guid,
        "pub_date": dt.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "duration": "00:15:00",
    }
    continuity.add_episode(sid, episode)
    episodes = continuity.list_episodes(sid)
    series_meta = {"title": bible.get("title", "Loreweaver"),
                   "premise": bible.get("premise", ""),
                   "cover_url": asset_urls.get("image_url", "")}
    feed_path = rss.build_feed(series_meta, episodes, settings.WEB_PLAYER_DIR)
    results["self_hosted"] = deploy.deploy() + "/index.html"
    results["feed"] = settings.PUBLIC_BASE_URL.rstrip("/") + "/feed.xml"
    results["cover_url"] = asset_urls.get("image_url", "")

    # (2) Podcast host.
    results["podcast"] = rss.push_to_host(feed_path, episode)

    # (3) YouTube (render still-image video, then upload).
    video_out = str(files.video_dir(sid) / f"ch{chapter:02d}.mp4")
    video = media.make_video(audio, thumb, video_out) if audio else ""
    results["youtube"] = youtube.upload(video, title, show_notes, thumb)

    log("publisher", "published to: " + ", ".join(f"{k}={v}" for k, v in results.items()))
    return {"publish_results": results, "show_notes": show_notes, "video_path": video}
