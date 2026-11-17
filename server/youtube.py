"""Upload a recorded VOD to YouTube via the YouTube Data API v3.

Plain HTTP over httpx — no Google client libraries. Those pull in ~150 MB
(mostly API discovery documents nobody here uses) and kept one-click upload
out of the packaged build; the two flows we actually need are small:

  * **Sign-in** is Google's "installed app" OAuth flow: open the consent page
    in the user's browser with a loopback redirect (http://127.0.0.1:<port>/)
    and PKCE, catch the code on a one-shot local HTTP server, exchange it for
    a refresh token. Google's account chooser — and its channel chooser when an
    account manages several (brand accounts) — decides which channel the token
    uploads to; signing in again is how you switch.
  * **Upload** is the documented resumable protocol: POST the metadata to get
    a session URL, PUT the file in 8 MiB chunks, resume from Google's `Range`
    on a 308.

What the user has to do once (cannot be automated — the OAuth client is theirs
and tied to their Google account): create a Google Cloud project with the
YouTube Data API v3 enabled, an External consent screen with themselves as a
test user, and a *Desktop app* OAuth client whose JSON they load in Settings.
docs/youtube-upload.md has the click-by-click version; `check_setup()` is the
"Test configuration" button that tells them what is still wrong.

Google's own constraints, not worked around here:
  * an unaudited project can only produce **private** videos (hence
    `DEFAULT_PRIVACY`), and
  * an upload costs ~1600 of the default 10,000 daily quota units (~6 a day),
  * an app left in "Testing" gets refresh tokens that expire after 7 days.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

# upload, plus read-only so we can say WHICH channel the uploads land on
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_FILENAME = "youtube_token.json"
CLIENT_FILENAME = "youtube_client_secret.json"
PRIVACY_VALUES = ("private", "unlisted", "public")
DEFAULT_PRIVACY = "private"
# YouTube rejects titles over 100 chars and descriptions over 5000
MAX_TITLE = 100
MAX_DESCRIPTION = 5000

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
# How long the browser sign-in waits for the user before giving up
SIGN_IN_TIMEOUT_S = 300

# Google Cloud console pages the fixes point at (the "Google Auth Platform"
# pages replaced "APIs & Services > OAuth consent screen" in 2025).
CONSOLE = {
    "project": "https://console.cloud.google.com/projectcreate",
    "api": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
    "consent": "https://console.cloud.google.com/auth/overview",
    "audience": "https://console.cloud.google.com/auth/audience",
    "clients": "https://console.cloud.google.com/auth/clients",
}


class YouTubeError(RuntimeError):
    """Anything the caller should show the user verbatim."""


class SignInRequired(YouTubeError):
    """No usable sign-in: never signed in, expired (7-day Testing limit),
    revoked, or made with a different OAuth client."""


def _http(transport=None):
    return httpx.Client(transport=transport, timeout=60)


def clean_path(value):
    """A path as the user pasted it. Windows' "Copy as path" wraps it in double
    quotes, which then reads as "no such file" — strip those and whitespace."""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def token_path(db_dir):
    return Path(db_dir) / TOKEN_FILENAME


def client_path(db_dir):
    """Where an uploaded client-secrets file is kept (beside the db)."""
    return Path(db_dir) / CLIENT_FILENAME


def _write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best-effort; no-op on some filesystems
    except OSError:
        pass


# ---------- client secrets ----------

def _check(key, status, label, detail="", fix="", link=None):
    return {"key": key, "status": status, "label": label, "detail": detail,
            "fix": fix, "link": link}


def parse_client_secrets(text):
    """(client dict, None) or (None, failed check) for client-secrets JSON."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return None, _check(
            "secrets", "fail", "OAuth client file", f"Not readable as JSON: {exc}",
            "Download the client's JSON again from Google Cloud (Clients → your "
            "client → Download JSON) and choose that file.", CONSOLE["clients"])
    if not isinstance(data, dict):
        data = {}
    if "installed" not in data:
        if "web" in data:
            return None, _check(
                "secrets", "fail", "OAuth client file",
                "This is a Web application client.",
                "Coach Potato signs in like a desktop app. Create a new OAuth "
                "client with Application type = Desktop app and choose its JSON "
                "instead (the web client can be deleted).", CONSOLE["clients"])
        if data.get("type") == "service_account":
            return None, _check(
                "secrets", "fail", "OAuth client file",
                "This is a service-account key, not an OAuth client.",
                "YouTube uploads need a person's sign-in. Create an OAuth client "
                "of type Desktop app and choose that JSON instead.",
                CONSOLE["clients"])
        return None, _check(
            "secrets", "fail", "OAuth client file",
            "This JSON isn't an OAuth client file.",
            "Download the JSON from Google Cloud → Clients → your Desktop app "
            "client (the download icon).", CONSOLE["clients"])
    client = data["installed"] or {}
    if not client.get("client_id") or not client.get("client_secret"):
        return None, _check(
            "secrets", "fail", "OAuth client file",
            "The file has no client_id/client_secret.",
            "Download the JSON again from Google Cloud → Clients.",
            CONSOLE["clients"])
    return client, None


def read_client(path):
    """(client dict or None, check) for the configured client-secrets file."""
    path = clean_path(path)
    if not path:
        return None, _check(
            "secrets", "fail", "OAuth client file", "None loaded.",
            "Create an OAuth client of type Desktop app in Google Cloud, download "
            "its JSON and choose it with “Choose client file…”.", CONSOLE["clients"])
    p = Path(path)
    if not p.is_file():
        return None, _check(
            "secrets", "fail", "OAuth client file", f"No file at {p}.",
            "Choose the downloaded JSON again with “Choose client file…”.")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return None, _check("secrets", "fail", "OAuth client file",
                            f"Can't read {p}: {exc}",
                            "Choose the downloaded JSON again.")
    client, failure = parse_client_secrets(text)
    if failure:
        return None, failure
    return client, _check(
        "secrets", "ok", "OAuth client file",
        f"Desktop app client from project {client.get('project_id') or '(unknown)'}.")


# ---------- tokens ----------

def load_token(db_dir):
    """The cached sign-in, or None. Also reads the format the earlier
    google-auth based version wrote (`token`/`expiry`), so nobody who was
    already signed in has to sign in again just because of the rewrite."""
    path = token_path(db_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "access_token" not in data and "token" in data:
        data["access_token"] = data.get("token")
        data["expires_at"] = 0  # unknown expiry -> refresh before first use
    return data


def save_token(db_dir, token):
    _write_private(token_path(db_dir), json.dumps(token, indent=1))


def forget_token(db_dir):
    token_path(db_dir).unlink(missing_ok=True)


def has_credentials(client_secrets_path, db_dir):
    """Whether an upload could proceed without further setup: a client file and
    a sign-in made with that client. (An expired sign-in still counts; the
    upload then says so, and Test configuration offers to sign in again.)"""
    client, _ = read_client(client_secrets_path)
    token = load_token(db_dir)
    return bool(client and token and token.get("refresh_token")
                and token.get("client_id") in (None, client["client_id"]))


def _token_uri(client):
    return client.get("token_uri") or TOKEN_URL


def _refresh(client, token, transport=None):
    with _http(transport) as http:
        r = http.post(_token_uri(client), data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": token.get("refresh_token") or "",
            "grant_type": "refresh_token",
        })
    if r.status_code != 200:
        try:
            err = r.json()
        except ValueError:
            err = {}
        reason = err.get("error") or f"HTTP {r.status_code}"
        raise SignInRequired(
            f"Your YouTube sign-in no longer works ({reason}) — sign in again "
            "in Settings → Recordings.")
    body = r.json()
    token = dict(token)
    token["access_token"] = body["access_token"]
    token["expires_at"] = time.time() + int(body.get("expires_in") or 3600)
    if body.get("refresh_token"):
        token["refresh_token"] = body["refresh_token"]
    return token


def access_token(client, db_dir, transport=None, *, force_refresh=False):
    """A valid access token, refreshing (and re-saving) when needed."""
    token = load_token(db_dir)
    if not token or not token.get("refresh_token"):
        raise SignInRequired("Not signed in to YouTube yet — sign in in "
                             "Settings → Recordings.")
    if token.get("client_id") and token["client_id"] != client["client_id"]:
        raise SignInRequired("The YouTube sign-in was made with a different "
                             "OAuth client — sign in again in Settings → Recordings.")
    if force_refresh or not token.get("access_token") \
            or time.time() > float(token.get("expires_at") or 0) - 60:
        token = _refresh(client, token, transport)
        token["client_id"] = client["client_id"]
        save_token(db_dir, token)
    return token["access_token"]


# ---------- sign-in (browser, loopback redirect, PKCE) ----------

def _pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Coach Potato</title>
<body style="font-family:system-ui,sans-serif;padding:3em;text-align:center">
<h2>{title}</h2><p>{text}</p></body>"""


class SignIn:
    """One browser sign-in. `start()` binds the loopback port and returns the
    consent URL (and opens it); `wait()` blocks until Google redirects back,
    the user denies, or the timeout passes, then stores the token."""

    def __init__(self, client, db_dir, *, transport=None, open_browser=True,
                 timeout_s=SIGN_IN_TIMEOUT_S):
        self.client = client
        self.db_dir = db_dir
        self.transport = transport
        self.open_browser = open_browser
        self.timeout_s = timeout_s
        self.result = {}
        self._server = None

    def start(self):
        result = self.result
        expected_state = secrets.token_urlsafe(16)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib name
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" not in query and "error" not in query:
                    self.send_response(404)
                    self.end_headers()
                    return
                if query.get("state", [""])[0] != expected_state:
                    result["error"] = "state_mismatch"
                elif "error" in query:
                    result["error"] = query["error"][0]
                else:
                    result["code"] = query["code"][0]
                ok = "code" in result
                page = _DONE_PAGE.format(
                    title="Signed in ✓" if ok else "Sign-in didn't complete",
                    text=("You can close this tab and go back to Coach Potato."
                          if ok else "Go back to Coach Potato to see what to fix."))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.encode())

            def log_message(self, *args):  # keep the console quiet
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = 0.5
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_port}/"
        self.verifier, challenge = _pkce()
        params = {
            "client_id": self.client["client_id"],
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            # always show the account (and channel) chooser, and always get a
            # refresh token — even when this Google account consented before
            "prompt": "select_account consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": expected_state,
        }
        base = self.client.get("auth_uri") or AUTH_URL
        self.auth_url = f"{base}?{urllib.parse.urlencode(params)}"
        if self.open_browser:
            webbrowser.open(self.auth_url)
        return self.auth_url

    def wait(self):
        deadline = time.time() + self.timeout_s
        try:
            while not self.result and time.time() < deadline:
                self._server.handle_request()
        finally:
            self._server.server_close()
        if not self.result:
            raise YouTubeError("sign-in timed out — the browser sign-in wasn't "
                               "completed")
        if "error" in self.result:
            raise YouTubeError(f"sign-in failed: {self.result['error']}")
        with _http(self.transport) as http:
            r = http.post(_token_uri(self.client), data={
                "code": self.result["code"],
                "client_id": self.client["client_id"],
                "client_secret": self.client["client_secret"],
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": self.verifier,
            })
        if r.status_code != 200:
            raise YouTubeError(f"sign-in failed: Google answered {r.status_code} "
                               f"{r.text[:300]}")
        body = r.json()
        token = {
            "client_id": self.client["client_id"],
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "expires_at": time.time() + int(body.get("expires_in") or 3600),
            "scope": body.get("scope", ""),
        }
        try:  # which channel did they pick? best-effort, never fails sign-in
            token.update(_channel(token["access_token"], self.transport))
        except YouTubeError:
            pass
        save_token(self.db_dir, token)
        return token


# ---------- API calls ----------

def _error_reasons(response):
    try:
        err = response.json().get("error") or {}
    except ValueError:
        return [], response.text[:300]
    if isinstance(err, str):  # OAuth endpoints: {"error": "invalid_grant"}
        return [err], err
    reasons = [e.get("reason") for e in err.get("errors") or []]
    reasons += [d.get("reason") for d in err.get("details") or []]
    return [r for r in reasons if r], err.get("message") or response.text[:300]


class ApiError(YouTubeError):
    def __init__(self, response):
        self.status = response.status_code
        self.reasons, message = _error_reasons(response)
        super().__init__(f"YouTube answered {self.status}: {message}")


def _channel(access, transport=None):
    """{channel_id, channel_title} of the signed-in channel."""
    with _http(transport) as http:
        r = http.get(f"{API_URL}/channels",
                     params={"part": "snippet", "mine": "true"},
                     headers={"Authorization": f"Bearer {access}"})
    if r.status_code != 200:
        raise ApiError(r)
    items = r.json().get("items") or []
    if not items:
        return {"channel_id": "", "channel_title": ""}
    return {"channel_id": items[0]["id"],
            "channel_title": items[0].get("snippet", {}).get("title", "")}


def upload(video_path, title, description="", privacy=DEFAULT_PRIVACY, *,
           client_secrets_path=None, db_dir=".", tags=None, on_progress=None,
           transport=None):
    """Upload one file, resumably. Returns the new YouTube video id."""
    if privacy not in PRIVACY_VALUES:
        raise YouTubeError(f"privacy must be one of {', '.join(PRIVACY_VALUES)}")
    video = Path(video_path)
    if not video.exists():
        raise YouTubeError(f"video file is gone: {video}")
    client, failure = read_client(client_secrets_path)
    if failure and failure["status"] == "fail":
        raise YouTubeError(f"{failure['detail']} {failure['fix']}")
    access = access_token(client, db_dir, transport)

    total = video.stat().st_size
    body = {
        "snippet": {
            "title": (title or video.stem)[:MAX_TITLE],
            "description": (description or "")[:MAX_DESCRIPTION],
            "tags": list(tags or []),
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    with _http(transport) as http:
        def auth():
            return {"Authorization": f"Bearer {access}"}

        r = http.post(UPLOAD_URL, params={"uploadType": "resumable",
                                          "part": "snippet,status"},
                      headers={**auth(), "X-Upload-Content-Length": str(total),
                               "X-Upload-Content-Type": "video/*"},
                      json=body)
        if r.status_code == 401:
            access = access_token(client, db_dir, transport, force_refresh=True)
            r = http.post(UPLOAD_URL, params={"uploadType": "resumable",
                                              "part": "snippet,status"},
                          headers={**auth(), "X-Upload-Content-Length": str(total),
                                   "X-Upload-Content-Type": "video/*"},
                          json=body)
        if r.status_code != 200 or not r.headers.get("location"):
            raise _upload_error(r)
        session = r.headers["location"]

        offset, failures = 0, 0
        with open(video, "rb") as fh:
            while True:
                fh.seek(offset)
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                try:
                    r = http.put(session, content=chunk, headers={
                        **auth(), "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total}"})
                except httpx.HTTPError as exc:
                    failures += 1
                    if failures > 5:
                        raise YouTubeError(f"upload failed: {exc}") from exc
                    time.sleep(min(30, 2 ** failures))
                    offset = _resume_offset(http, session, total, auth())
                    continue
                if r.status_code in (200, 201):
                    video_id = (r.json() or {}).get("id")
                    if not video_id:
                        raise YouTubeError(
                            f"upload finished but YouTube returned no id: {r.text[:300]}")
                    if on_progress:
                        on_progress(1.0)
                    return video_id
                if r.status_code == 308:
                    offset = _range_end(r)
                    failures = 0
                elif r.status_code == 401:
                    access = access_token(client, db_dir, transport, force_refresh=True)
                elif r.status_code >= 500:
                    failures += 1
                    if failures > 5:
                        raise _upload_error(r)
                    time.sleep(min(30, 2 ** failures))
                    offset = _resume_offset(http, session, total, auth())
                else:
                    raise _upload_error(r)
                if on_progress and total:
                    on_progress(offset / total)


def _range_end(response):
    """Next byte to send after a 308: one past the end of Google's `Range`."""
    rng = response.headers.get("range")  # "bytes=0-12345"
    if not rng:
        return 0
    return int(rng.rsplit("-", 1)[1]) + 1


def _resume_offset(http, session, total, headers):
    r = http.put(session, headers={**headers, "Content-Length": "0",
                                   "Content-Range": f"bytes */{total}"})
    return _range_end(r) if r.status_code == 308 else 0


def _upload_error(response):
    reasons, message = _error_reasons(response)
    if {"quotaExceeded", "uploadLimitExceeded"} & set(reasons):
        return YouTubeError("YouTube's daily upload quota is used up (about 6 "
                            "uploads a day) — try again tomorrow, or use Manual upload.")
    if {"accessNotConfigured", "SERVICE_DISABLED"} & set(reasons):
        return YouTubeError("The YouTube Data API isn't enabled for your Google "
                            "Cloud project — Settings → Recordings → Test "
                            "configuration shows how to fix it.")
    if "youtubeSignupRequired" in reasons:
        return YouTubeError("This Google account has no YouTube channel yet — "
                            "create one on youtube.com, then try again.")
    return YouTubeError(f"upload failed ({response.status_code}): {message}")


def watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


# ---------- "Test configuration" ----------

def _signin_failure(message):
    text = str(message)
    if "access_denied" in text:
        return _check(
            "signin", "fail", "Google sign-in",
            "Google refused the sign-in (access_denied).",
            "If Google said “Access blocked”, your account isn't a test user: "
            "open Audience → Test users → Add users, add the Google account that "
            "owns your channel, then sign in again. If you clicked Cancel, just "
            "sign in again.", CONSOLE["audience"])
    if "redirect_uri_mismatch" in text or "invalid_client" in text:
        return _check(
            "signin", "fail", "Google sign-in", text,
            "The client file isn't a Desktop app client, or it was deleted in "
            "Google Cloud. Create a Desktop app client and choose its JSON again.",
            CONSOLE["clients"])
    if "timed out" in text:
        return _check("signin", "fail", "Google sign-in",
                      "The browser sign-in wasn't completed in time.",
                      "Click Sign in with Google again and finish the steps in "
                      "the browser tab it opens.")
    return _check("signin", "fail", "Google sign-in", text,
                  "Click Sign in with Google to try again.")


def check_setup(client_secrets_path, db_dir, *, transport=None):
    """Settings' "Test configuration". Returns {ok, needs_sign_in, channel,
    checks}; each check is {key, status ok|warn|fail|skip, label, detail, fix,
    link} — a failure says what to do about it, not just that it failed.
    Refreshes the cached sign-in (proving client + grant still work) and makes
    one 1-unit API read; never uploads, never opens a browser."""
    client, secrets_check = read_client(client_secrets_path)
    checks = [secrets_check]
    state = {"needs_sign_in": False, "channel": None}

    def done():
        statuses = [c["status"] for c in checks]
        return {"ok": "fail" not in statuses and not state["needs_sign_in"],
                **state, "checks": checks}

    if client is None:
        checks.append(_check("signin", "skip", "Google sign-in",
                             "Load the OAuth client file first."))
        return done()
    token = load_token(db_dir)
    if not token or not token.get("refresh_token"):
        state["needs_sign_in"] = True
        checks.append(_check(
            "signin", "warn", "Google sign-in", "Not signed in yet.",
            "Click Sign in with Google and pick the account (and channel) to "
            "upload to. Google warns it “hasn't verified this app” — it's your "
            "own app, so choose Continue."))
        return done()
    try:
        access = access_token(client, db_dir, transport, force_refresh=True)
    except SignInRequired as exc:
        state["needs_sign_in"] = True
        expired = "invalid_grant" in str(exc)
        checks.append(_check(
            "signin", "fail", "Google sign-in",
            "Your sign-in has expired or was revoked." if expired else str(exc),
            "Click Sign in with Google." + (
                " Apps left in Google's Testing status get sign-ins that expire "
                "after 7 days — Audience → Publish app stops that."
                if expired else ""),
            CONSOLE["audience"] if expired else None))
        return done()
    except httpx.HTTPError as exc:
        checks.append(_check("signin", "warn", "Google sign-in",
                             f"Couldn't reach Google: {exc}",
                             "Check your internet connection and try again."))
        return done()
    checks.append(_check("signin", "ok", "Google sign-in", "Signed in."))

    try:
        channel = _channel(access, transport)
    except ApiError as exc:
        reasons = set(exc.reasons)
        if {"accessNotConfigured", "SERVICE_DISABLED"} & reasons:
            checks.append(_check(
                "api", "fail", "YouTube Data API v3",
                "Not enabled for this Google Cloud project.",
                "Open the API page (with the same project selected at the top) "
                "and click Enable. It can take a few minutes to take effect.",
                CONSOLE["api"]))
        elif {"quotaExceeded", "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED"} & reasons:
            checks.append(_check(
                "api", "warn", "YouTube Data API v3",
                "Enabled, but today's quota is used up.",
                "The default quota allows ~6 uploads a day; it resets at midnight "
                "Pacific time."))
        elif {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"} & reasons:
            state["needs_sign_in"] = True
            checks.append(_check(
                "api", "warn", "Channel",
                "This sign-in predates the channel display.",
                "Sign in with Google once more so Coach Potato can show which "
                "channel uploads go to."))
        else:
            checks.append(_check("api", "warn", "YouTube Data API v3", str(exc),
                                 "This may be temporary — try again in a minute."))
        return done()
    except httpx.HTTPError as exc:
        checks.append(_check("api", "warn", "YouTube Data API v3",
                             f"Couldn't reach YouTube: {exc}",
                             "Check your internet connection and try again."))
        return done()
    if not channel["channel_id"]:
        checks.append(_check(
            "api", "fail", "Channel", "This Google account has no YouTube channel.",
            "Create a channel on youtube.com (or sign in with the account that "
            "has one), then test again."))
        return done()
    token = load_token(db_dir) or {}
    token.update(channel)
    save_token(db_dir, token)
    state["channel"] = channel
    checks.append(_check("api", "ok", "Channel",
                         f"Uploads go to “{channel['channel_title']}”."))
    return done()
