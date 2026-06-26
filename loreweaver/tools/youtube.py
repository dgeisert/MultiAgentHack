"""YouTube upload wrapper. Stubbed unless client secrets are configured."""
from __future__ import annotations

import hashlib

from .. import settings
from .util import log, retry


@retry(times=2)
def _live_upload(video_path: str, title: str, description: str, thumb_path: str) -> str:
    import google_auth_oauthlib.flow
    import googleapiclient.discovery
    import googleapiclient.http

    flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
        settings.YOUTUBE_CLIENT_SECRETS, ["https://www.googleapis.com/auth/youtube.upload"]
    )
    creds = flow.run_local_server(port=0)
    yt = googleapiclient.discovery.build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": title, "description": description, "categoryId": "24"},
        "status": {"privacyStatus": "public"},
    }
    media = googleapiclient.http.MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = req.execute()
    return f"https://youtu.be/{resp['id']}"


def upload(video_path: str, title: str, description: str, thumb_path: str = "") -> str:
    if settings.mock_mode() or not settings.YOUTUBE_CLIENT_SECRETS or not video_path:
        vid = hashlib.sha1(title.encode()).hexdigest()[:11]
        url = f"https://youtu.be/{vid}"
        log("youtube", f"(mock) upload skipped; would publish {title!r} -> {url}")
        return url
    log("youtube", f"uploading {title!r}")
    return _live_upload(video_path, title, description, thumb_path)
