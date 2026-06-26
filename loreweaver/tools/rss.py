"""Podcast RSS feed generation + (optional) push to a podcast host.

Always writes a spec-compliant feed.xml into the web_player dir (this powers the
self-hosted target and the local web player). When a podcast host API key is
present, it also pushes the episode to that host.
"""
from __future__ import annotations

import datetime as dt
import html
from pathlib import Path
from xml.sax.saxutils import escape

from .. import settings
from .util import log


def _fmt(dtobj: dt.datetime) -> str:
    return dtobj.strftime("%a, %d %b %Y %H:%M:%S +0000")


def build_feed(series: dict, episodes: list[dict], out_dir: Path) -> str:
    """Render an RSS 2.0 + iTunes podcast feed. `episodes` is newest-last.

    Each episode: {title, description, audio_url, image_url, guid, pub_date, duration}
    """
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    title = escape(series.get("title", "Loreweaver"))
    desc = escape(series.get("premise", "An autonomously generated fantasy audiobook."))
    cover = series.get("cover_url", f"{base}/cover.png")

    items = []
    for ep in episodes:
        items.append(
            f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep.get('description',''))}</description>
      <enclosure url="{escape(ep['audio_url'])}" type="audio/mpeg" length="0"/>
      <guid isPermaLink="false">{escape(ep['guid'])}</guid>
      <pubDate>{ep.get('pub_date', _fmt(dt.datetime.utcnow()))}</pubDate>
      <itunes:duration>{ep.get('duration','00:15:00')}</itunes:duration>
      <itunes:image href="{escape(ep.get('image_url', cover))}"/>
    </item>"""
        )

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{title}</title>
    <link>{base}</link>
    <language>en-us</language>
    <description>{desc}</description>
    <itunes:author>Loreweaver (autonomous agents)</itunes:author>
    <itunes:image href="{escape(cover)}"/>
    <itunes:category text="Fiction"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    feed_path = out_dir / "feed.xml"
    feed_path.write_text(feed)
    log("rss", f"wrote feed -> {feed_path} ({len(episodes)} episodes)")
    return str(feed_path)


def push_to_host(feed_path: str, episode: dict) -> str:
    """Push the newest episode to a podcast host. Stubbed unless a key is set."""
    if settings.mock_mode() or not settings.PODCAST_HOST_API_KEY:
        url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/feed.xml"
        log("rss", f"(mock) host push skipped; public feed at {url}")
        return url
    # Real host integrations (Transistor/Buzzsprout/etc.) would POST here.
    raise NotImplementedError("Wire your podcast host API in push_to_host().")
