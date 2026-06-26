"""Self-hosted feed + web player 'deploy'.

For the hackathon this copies artifacts into the web_player directory and
returns a public URL. Swap the body of `deploy()` for a real Vercel/Netlify/S3
push (or a sponsor hosting provider) to publish to the open web.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import settings
from .util import log


def publish_assets(audio_path: str, cover_path: str, series_id: str, chapter: int) -> dict:
    """Copy chapter assets into the web_player dir; return their public URLs."""
    web = settings.WEB_PLAYER_DIR
    assets = web / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    base = settings.PUBLIC_BASE_URL.rstrip("/")

    urls: dict[str, str] = {}
    if audio_path and Path(audio_path).exists():
        name = f"{series_id}_ch{chapter:02d}{Path(audio_path).suffix}"
        shutil.copy(audio_path, assets / name)
        urls["audio_url"] = f"{base}/assets/{name}"
    if cover_path and Path(cover_path).exists():
        name = f"{series_id}_ch{chapter:02d}{Path(cover_path).suffix}"
        shutil.copy(cover_path, assets / name)
        urls["image_url"] = f"{base}/assets/{name}"
    return urls


def deploy() -> str:
    """'Deploy' the web player. Mock returns the local URL; wire a real host here."""
    if settings.mock_mode():
        url = settings.PUBLIC_BASE_URL.rstrip("/")
        log("deploy", f"(mock) web player served from {settings.WEB_PLAYER_DIR} at {url}")
        return url
    raise NotImplementedError("Wire Vercel/Netlify/S3 deploy in deploy().")
