"""Upload a recorded VOD to YouTube via the YouTube Data API v3.

What the user has to do once (this cannot be automated — the credentials are
theirs and are tied to their Google account):

  1. Create a project at console.cloud.google.com and enable "YouTube Data
     API v3".
  2. Create an OAuth 2.0 Client ID of type *Desktop app* and download the
     client-secrets JSON.
  3. Point Settings -> "YouTube client secrets" at that file.

The first upload opens a browser for consent; the resulting token is cached
next to the database (`youtube_token.json`) so later uploads are silent.

Two constraints worth knowing, both Google's and neither worked around here:

  * An OAuth project that has not been through Google's audit can only produce
    **private** videos. Setting "public" on an unaudited project results in a
    video that stays private regardless. That is why `DEFAULT_PRIVACY` is
    "private" — it matches what actually happens.
  * An upload costs ~1600 units of the default 10,000/day quota, i.e. roughly
    six uploads per day before it starts failing with quotaExceeded.

The Google libraries are imported lazily so the rest of the app — and the
tests — run without them installed.
"""
from __future__ import annotations

import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILENAME = "youtube_token.json"
PRIVACY_VALUES = ("private", "unlisted", "public")
DEFAULT_PRIVACY = "private"
# YouTube rejects titles over 100 chars and descriptions over 5000
MAX_TITLE = 100
MAX_DESCRIPTION = 5000


class YouTubeError(RuntimeError):
    """Anything the caller should show the user verbatim."""


def _require_libraries():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise YouTubeError(
            "YouTube upload needs the google-api-python-client and "
            "google-auth-oauthlib packages — reinstall requirements.txt."
        ) from exc
    return Request, Credentials, InstalledAppFlow, build, MediaFileUpload


def token_path(db_dir):
    return Path(db_dir) / TOKEN_FILENAME


def has_credentials(client_secrets_path, db_dir):
    """Whether an upload could proceed without further setup."""
    return bool(client_secrets_path and Path(client_secrets_path).exists()) \
        or token_path(db_dir).exists()


def _credentials(client_secrets_path, db_dir):
    Request, Credentials, InstalledAppFlow, _, _ = _require_libraries()
    token_file = token_path(db_dir)
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not client_secrets_path or not Path(client_secrets_path).exists():
            raise YouTubeError(
                "No YouTube client secrets configured. Create an OAuth 'Desktop "
                "app' client in Google Cloud with the YouTube Data API enabled, "
                "then set its JSON file in Settings.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path), SCOPES)
        # opens a browser on this machine; the desktop app is local anyway
        creds = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)  # best-effort; no-op on some filesystems
    except OSError:
        pass
    return creds


def upload(video_path, title, description="", privacy=DEFAULT_PRIVACY, *,
           client_secrets_path=None, db_dir=".", tags=None, on_progress=None):
    """Upload one file, resumably. Returns the new YouTube video id."""
    if privacy not in PRIVACY_VALUES:
        raise YouTubeError(f"privacy must be one of {', '.join(PRIVACY_VALUES)}")
    video = Path(video_path)
    if not video.exists():
        raise YouTubeError(f"video file is gone: {video}")

    _, _, _, build, MediaFileUpload = _require_libraries()
    creds = _credentials(client_secrets_path, db_dir)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": (title or video.stem)[:MAX_TITLE],
            "description": (description or "")[:MAX_DESCRIPTION],
            "tags": list(tags or []),
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    # resumable: these are 1-2 GB files and a retried whole upload is painful
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except Exception as exc:  # pragma: no cover - network
            raise YouTubeError(f"upload failed: {exc}") from exc
        if status and on_progress:
            on_progress(status.progress())
    video_id = (response or {}).get("id")
    if not video_id:
        raise YouTubeError(f"upload finished but YouTube returned no id: {response}")
    return video_id


def watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"
