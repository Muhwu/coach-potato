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

import json
import os
import sys
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
    if not libraries_available():
        raise YouTubeError(
            "YouTube upload needs the google-api-python-client and "
            "google-auth-oauthlib packages — pip install -r requirements-youtube.txt.")
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


def libraries_available():
    """Whether the optional Google client libraries are installed. They are NOT
    in requirements.txt (see requirements-youtube.txt) — everything else works
    without them, so this is what decides whether the app offers one-click
    upload or the manual reveal-the-file path."""
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
    except ImportError:
        return False
    return True


def has_credentials(client_secrets_path, db_dir):
    """Whether an upload could proceed without further setup — which includes
    the optional libraries being installed, so the UI never shows an upload
    button that can only fail."""
    if not libraries_available():
        return False
    return bool(client_secrets_path and Path(client_secrets_path).exists()) \
        or token_path(db_dir).exists()


def clean_path(value):
    """A path as the user pasted it. Windows' "Copy as path" wraps it in double
    quotes, which then reads as "no such file" — strip those and whitespace."""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


# How long the "Sign in" browser flow waits for the user before giving up, so a
# closed tab can't hang the request forever.
SIGN_IN_TIMEOUT_S = 300


def _sign_in(client_secrets_path):
    _, _, InstalledAppFlow, _, _ = _require_libraries()
    if not client_secrets_path or not Path(client_secrets_path).exists():
        raise YouTubeError(
            "No YouTube client secrets configured. Create an OAuth 'Desktop "
            "app' client in Google Cloud with the YouTube Data API enabled, "
            "then set its JSON file in Settings.")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets_path), SCOPES)
    # opens a browser on this machine; the desktop app is local anyway
    return flow.run_local_server(port=0, timeout_seconds=SIGN_IN_TIMEOUT_S)


def _credentials(client_secrets_path, db_dir, *, force_sign_in=False):
    Request, Credentials, _, _, _ = _require_libraries()
    token_file = token_path(db_dir)
    creds = None
    if token_file.exists() and not force_sign_in:
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except (ValueError, KeyError, json.JSONDecodeError):
            creds = None  # corrupt/foreign token file: just sign in again
    if creds and creds.valid:
        return creds
    refreshed = False
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            refreshed = True
        except Exception:  # noqa: BLE001 - RefreshError/invalid_grant etc.
            # An app left in Google's "Testing" status gets refresh tokens that
            # die after 7 days, and a user can revoke access at any time. Either
            # way the fix is a fresh sign-in, not an error the user has to
            # decode.
            refreshed = False
    if not refreshed:
        creds = _sign_in(client_secrets_path)
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


# ---------- "Test configuration" ----------

# Google Cloud console pages the fixes point at. The new "Google Auth Platform"
# pages (auth/...) replaced "APIs & Services > OAuth consent screen" in 2025.
CONSOLE = {
    "project": "https://console.cloud.google.com/projectcreate",
    "api": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
    "audience": "https://console.cloud.google.com/auth/audience",
    "clients": "https://console.cloud.google.com/auth/clients",
}


def _check(key, status, label, detail="", fix="", link=None):
    return {"key": key, "status": status, "label": label, "detail": detail,
            "fix": fix, "link": link}


def _read_client_secrets(path):
    """(client dict or None, check) for the client-secrets JSON."""
    if not path:
        return None, _check(
            "secrets", "fail", "Client secrets file",
            "No file configured.",
            "Create an OAuth client of type Desktop app, download its JSON and "
            "paste the file's full path into the field above.", CONSOLE["clients"])
    p = Path(path)
    if p.is_dir():
        return None, _check(
            "secrets", "fail", "Client secrets file", f"{p} is a folder.",
            "Point the field at the downloaded .json file itself, not the folder "
            "it is in.")
    if not p.exists():
        return None, _check(
            "secrets", "fail", "Client secrets file", f"No file at {p}.",
            "Check the path — if you moved the downloaded JSON, paste its new "
            "location. On Windows, Shift+right-click the file → Copy as path.")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _check(
            "secrets", "fail", "Client secrets file", f"Not readable as JSON: {exc}",
            "Download the client's JSON again from Google Cloud (Clients → your "
            "client → Download JSON) — don't copy-paste it into an editor.",
            CONSOLE["clients"])
    if not isinstance(data, dict):
        data = {}
    if "installed" not in data:
        if "web" in data:
            return None, _check(
                "secrets", "fail", "Client secrets file",
                "This is a Web application client.",
                "Coach Potato signs in like a desktop app. Create a new OAuth "
                "client with Application type = Desktop app and use its JSON "
                "instead (the web client can be deleted).", CONSOLE["clients"])
        if data.get("type") == "service_account":
            return None, _check(
                "secrets", "fail", "Client secrets file",
                "This is a service-account key, not an OAuth client.",
                "YouTube uploads need a person's sign-in. Create an OAuth client "
                "ID of type Desktop app and use that JSON instead.",
                CONSOLE["clients"])
        return None, _check(
            "secrets", "fail", "Client secrets file",
            "This JSON isn't an OAuth client-secrets file.",
            "Download the JSON from Google Cloud → Clients → your Desktop app "
            "client (the download icon).", CONSOLE["clients"])
    client = data["installed"] or {}
    if not client.get("client_id") or not client.get("client_secret"):
        return None, _check(
            "secrets", "fail", "Client secrets file",
            "The file has no client_id/client_secret.",
            "Download the JSON again from Google Cloud → Clients.",
            CONSOLE["clients"])
    return client, _check(
        "secrets", "ok", "Client secrets file",
        f"Desktop app client {client['client_id'].split('-')[0]}… "
        f"(project {client.get('project_id') or 'unknown'}).")


def _libraries_check():
    if libraries_available():
        return _check("libraries", "ok", "Google upload libraries", "Installed.")
    if getattr(sys, "frozen", False):
        return _check(
            "libraries", "fail", "Google upload libraries",
            "This downloadable build doesn't include them.",
            "One-click upload is only available when running Coach Potato from "
            "source (pip install -r requirements-youtube.txt). The 📁 Upload to "
            "YouTube button on each recording still works without any setup.")
    return _check(
        "libraries", "fail", "Google upload libraries", "Not installed.",
        "Run: .venv/bin/pip install -r requirements-youtube.txt "
        "(Windows: .venv\\Scripts\\pip install -r requirements-youtube.txt), "
        "then restart Coach Potato.")


def _google_error_reason(exc):
    """Google's machine-readable reason from an HttpError, best-effort."""
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        err = payload.get("error") or {}
        reasons = [e.get("reason") for e in err.get("errors") or []]
        for detail in err.get("details") or []:
            reasons.append(detail.get("reason"))
        return [r for r in reasons if r], err.get("message") or str(exc)
    except Exception:  # noqa: BLE001
        return [], str(exc)


def _api_check(creds):
    """Prove the YouTube Data API is enabled for the project with one 1-unit
    read (videoCategories.list). An upload-only token may not be allowed to
    read it — that says nothing about the upload, so it's a pass with a note."""
    _, _, _, build, _ = _require_libraries()
    from googleapiclient.errors import HttpError
    try:
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        yt.videoCategories().list(part="snippet", regionCode="US").execute()
    except HttpError as exc:
        reasons, message = _google_error_reason(exc)
        if {"accessNotConfigured", "SERVICE_DISABLED"} & set(reasons):
            return _check(
                "api", "fail", "YouTube Data API v3",
                "Not enabled for this Google Cloud project.",
                "Open the API page (make sure the project from your client "
                "secrets is selected at the top) and click Enable. It can take a "
                "few minutes to take effect.", CONSOLE["api"])
        if {"quotaExceeded", "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED"} & set(reasons):
            return _check(
                "api", "warn", "YouTube Data API v3",
                "Enabled, but today's quota is used up.",
                "Google's default quota allows ~6 uploads a day; it resets at "
                "midnight Pacific time.")
        if {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"} & set(reasons) \
                or exc.resp.status == 403:
            return _check("api", "ok", "YouTube Data API v3",
                          "Reachable (upload permission only, as intended).")
        return _check("api", "warn", "YouTube Data API v3",
                      f"Couldn't confirm: {message}",
                      "This may be temporary — try again in a minute.")
    except Exception as exc:  # noqa: BLE001 - network etc.
        return _check("api", "warn", "YouTube Data API v3",
                      f"Couldn't reach Google: {exc}",
                      "Check your internet connection and try again.")
    return _check("api", "ok", "YouTube Data API v3", "Enabled and reachable.")


def _sign_in_failure(exc):
    text = str(exc)
    if "access_denied" in text or "403" in text:
        return _check(
            "signin", "fail", "Google sign-in",
            "Google refused the sign-in (access_denied).",
            "While the app is in Testing, only listed test users may sign in. "
            "Open Audience → Test users → Add users and add the Google account "
            "that owns your YouTube channel, then sign in again.",
            CONSOLE["audience"])
    if "timed out" in text.lower() or "timeout" in text.lower():
        return _check(
            "signin", "fail", "Google sign-in",
            "The browser sign-in wasn't completed in time.",
            "Click Sign in with Google again and finish the steps in the browser "
            "tab it opens.")
    return _check("signin", "fail", "Google sign-in", f"Sign-in failed: {text}",
                  "Click Sign in with Google to try again.")


def check_setup(client_secrets_path, db_dir, *, sign_in=False):
    """Settings' "Test configuration". Returns {ok, needs_sign_in, checks}.

    Each check is {key, status ok|warn|fail|skip, label, detail, fix, link}: a
    failure says what to do about it, not just that something is wrong. Only
    `sign_in=True` opens a browser; otherwise the check uses the cached token
    (refreshing it proves the client and the grant are still good) and makes
    one 1-unit API read — never an upload.
    """
    secrets_path = clean_path(client_secrets_path)
    checks = [_libraries_check()]
    client, secrets_check = _read_client_secrets(secrets_path)
    checks.append(secrets_check)
    libs_ok = checks[0]["status"] == "ok"
    needs_sign_in = False

    def done():
        statuses = [c["status"] for c in checks]
        return {"ok": "fail" not in statuses and not needs_sign_in,
                "needs_sign_in": needs_sign_in, "checks": checks}

    if not libs_ok or client is None:
        checks.append(_check("signin", "skip", "Google sign-in",
                             "Fix the items above first."))
        return done()

    Request, Credentials, _, _, _ = _require_libraries()
    token_file = token_path(db_dir)
    creds = None
    if sign_in:
        try:
            creds = _credentials(secrets_path, db_dir, force_sign_in=True)
        except Exception as exc:  # noqa: BLE001
            checks.append(_sign_in_failure(exc))
            return done()
        checks.append(_check("signin", "ok", "Google sign-in",
                             "Signed in — uploads won't ask again."))
    else:
        if not token_file.exists():
            needs_sign_in = True
            checks.append(_check(
                "signin", "warn", "Google sign-in", "Not signed in yet.",
                "Click Sign in with Google. A browser tab opens; pick the "
                "account that owns your channel. Google warns that it \"hasn't "
                "verified this app\" — that's your own app, so choose Continue."))
            return done()
        try:
            token = json.loads(token_file.read_text(encoding="utf-8"))
            creds = Credentials.from_authorized_user_info(token, SCOPES)
        except (OSError, ValueError, KeyError) as exc:
            needs_sign_in = True
            checks.append(_check("signin", "fail", "Google sign-in",
                                 f"The saved sign-in is unreadable ({exc}).",
                                 "Click Sign in with Google to replace it."))
            return done()
        if token.get("client_id") and token["client_id"] != client["client_id"]:
            needs_sign_in = True
            checks.append(_check(
                "signin", "fail", "Google sign-in",
                "The saved sign-in belongs to a different OAuth client than the "
                "file above.",
                "Click Sign in with Google to sign in with this client."))
            return done()
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                needs_sign_in = True
                checks.append(_check(
                    "signin", "fail", "Google sign-in",
                    f"The saved sign-in no longer works ({exc}).",
                    "Click Sign in with Google. Apps left in Google's Testing "
                    "status get sign-ins that expire after 7 days — publishing "
                    "the app (Audience → Publish app) stops that.",
                    CONSOLE["audience"]))
                return done()
            token_file.write_text(creds.to_json(), encoding="utf-8")
        checks.append(_check("signin", "ok", "Google sign-in", "Signed in."))
    checks.append(_api_check(creds))
    return done()


def watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"
