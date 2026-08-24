"""Drive OBS Studio's recorder over obs-websocket v5.

OBS 28 and later ship obs-websocket built in (Tools -> WebSocket Server
Settings). Once it is enabled, OBS listens on ws://127.0.0.1:4455 and accepts a
small JSON-RPC-ish protocol, which is all this module speaks:

    <- op 0  Hello           (carries an auth challenge iff a password is set)
    -> op 1  Identify        (auth response + rpcVersion)
    <- op 2  Identified
    -> op 6  Request         (requestType + requestId + requestData)
    <- op 7  RequestResponse (requestStatus + responseData)

The one request that makes this feature possible is **StopRecord**: its
response carries `outputPath`, the file OBS just finished writing. That is why
the app can know exactly which video belongs to a coaching session without
watching a folder and guessing by modification time.

Two rules carried over from `server/recordings.py`, for the same reasons:
video files are never copied or moved (only paths are stored), and "forget
this recording" forgets the row, not the file — with ONE exception: the
forget endpoint deletes the file when, and only when, the user explicitly
confirmed that in a second, separate prompt (`?delete_file=true`).

`websocket-client` is imported lazily, and `libraries_available()` reports
whether it is installed — same shape as `server/youtube.py`, so a stale
virtualenv degrades to a clear message instead of an import error at startup.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
RPC_VERSION = 1
CONNECT_TIMEOUT = 4.0  # localhost — if OBS is there it answers immediately
OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_EVENT, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 5, 6, 7
# Only Outputs events (RecordStateChanged et al). The stop event carries
# outputPath, which is how a recording stopped IN OBS (hotkey, its own Stop
# button) still hands us its file — our own StopRecord response only covers
# stops we initiated. Events are captured opportunistically while reading
# request responses; nothing ever blocks waiting for one.
EVENT_SUBSCRIPTION_OUTPUTS = 1 << 6
RECORD_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"

# Containers preferred for playback. .mkv is NOT here on purpose even though
# Chromium (the packaged app's WebView2, and most users' browsers) plays
# OBS-flavoured Matroska fine: keeping it out makes playable_path() prefer a
# remuxed .mp4 sibling when one exists, which seeks more reliably.
PLAYABLE_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")
# ...while these are served to the player optimistically. The UI keeps an
# onerror fallback for the codec combinations Chromium genuinely can't play.
BROWSER_OK_EXTENSIONS = PLAYABLE_EXTENSIONS + (".mkv",)


class ObsError(RuntimeError):
    """Anything the caller should show the user verbatim."""


class ObsConnectionError(ObsError):
    """The transport failed (OBS not running, restarted, socket dropped). The
    caller may usefully reconnect and retry; a plain ObsError — OBS answering
    "no" to a request — will just fail again."""


def libraries_available():
    """False when `websocket-client` is not installed."""
    try:
        import websocket  # noqa: F401
    except ImportError:
        return False
    return True


def auth_response(password, salt, challenge):
    """obs-websocket v5's auth string:
    base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode()


class ObsClient:
    """One live connection. Not thread-safe: callers serialise on their own
    lock (app.py holds one, since FastAPI runs sync endpoints in a threadpool)."""

    def __init__(self, socket):
        self._socket = socket
        self._counter = 0
        self.obs_version = ""
        self.websocket_version = ""
        # set by _note_event when a RecordStateChanged(STOPPED) event goes by;
        # collected via take_last_output_path() and used to close a recording
        # row whose stop we did not initiate
        self._last_output_path = ""

    # ---------- wire ----------

    def _send(self, payload):
        try:
            self._socket.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - any transport failure reads the same
            raise ObsConnectionError(f"lost the connection to OBS ({exc})") from exc

    def _recv(self):
        try:
            raw = self._socket.recv()
        except Exception as exc:  # noqa: BLE001
            raise ObsConnectionError(f"lost the connection to OBS ({exc})") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not raw:
            raise ObsConnectionError("OBS closed the connection")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ObsError(f"unexpected reply from OBS: {raw[:120]!r}") from exc

    def _note_event(self, message):
        """Opportunistic: any event that goes by while we wait for a response.
        The one we care about is the recording stopping — its outputPath is
        the file, even when the stop came from OBS itself."""
        if message.get("op") != OP_EVENT:
            return
        body = message.get("d") or {}
        if body.get("eventType") != "RecordStateChanged":
            return
        data = body.get("eventData") or {}
        if data.get("outputState") == RECORD_STOPPED and data.get("outputPath"):
            self._last_output_path = data["outputPath"]

    def take_last_output_path(self):
        """-> the path from the most recent stop event, once. '' when no stop
        was observed on THIS connection — an app restarted mid-recording has a
        fresh connection and missed the event, so the manual attach fallback
        stays necessary."""
        path, self._last_output_path = self._last_output_path, ""
        return path

    def _recv_op(self, wanted):
        """Next message with the given op, skipping (but noting) events."""
        while True:
            message = self._recv()
            self._note_event(message)
            op = message.get("op")
            if op == wanted:
                return message.get("d") or {}
            if op in (OP_HELLO, OP_IDENTIFIED, OP_RESPONSE):
                raise ObsError(f"OBS sent op {op} while expecting op {wanted}")

    # ---------- handshake ----------

    def handshake(self, password=""):
        hello = self._recv_op(OP_HELLO)
        self.obs_version = hello.get("obsStudioVersion") or ""
        self.websocket_version = hello.get("obsWebSocketVersion") or ""
        identify = {"rpcVersion": RPC_VERSION,
                    "eventSubscriptions": EVENT_SUBSCRIPTION_OUTPUTS}
        challenge = hello.get("authentication")
        if challenge:
            if not password:
                raise ObsError(
                    "OBS's WebSocket server has a password set — put it in "
                    "Settings -> OBS recording.")
            identify["authentication"] = auth_response(
                password, challenge.get("salt", ""), challenge.get("challenge", ""))
        self._send({"op": OP_IDENTIFY, "d": identify})
        try:
            self._recv_op(OP_IDENTIFIED)
        except ObsError as exc:
            # OBS closes the socket (code 4009) rather than replying when the
            # auth response is wrong, so a drop here has exactly one likely cause
            if challenge:
                raise ObsError(
                    "OBS rejected the connection — the password in Settings "
                    "does not match OBS's WebSocket server password.") from exc
            raise
        return self

    # ---------- requests ----------

    def request(self, request_type, data=None):
        self._counter += 1
        request_id = str(self._counter)
        self._send({"op": OP_REQUEST, "d": {"requestType": request_type,
                                            "requestId": request_id,
                                            "requestData": data or {}}})
        while True:
            message = self._recv()
            self._note_event(message)
            if message.get("op") != OP_RESPONSE:
                continue
            body = message.get("d") or {}
            if body.get("requestId") != request_id:
                continue  # a response to something else; keep reading
            status = body.get("requestStatus") or {}
            if not status.get("result"):
                comment = status.get("comment") or f"code {status.get('code')}"
                raise ObsError(f"OBS refused {request_type}: {comment}")
            return body.get("responseData") or {}

    def version(self):
        data = self.request("GetVersion")
        return {"obs_version": data.get("obsVersion") or self.obs_version,
                "websocket_version": (data.get("obsWebSocketVersion")
                                      or self.websocket_version)}

    def record_status(self):
        data = self.request("GetRecordStatus")
        return {
            "recording": bool(data.get("outputActive")),
            "paused": bool(data.get("outputPaused")),
            # outputDuration is milliseconds of recorded video, and it does not
            # advance while paused — which is exactly what a bookmark offset
            # into the finished file needs.
            "duration_ms": int(data.get("outputDuration") or 0),
        }

    def record_format(self):
        """The container OBS would record into, e.g. 'mkv', 'mp4',
        'fragmented_mp4' — read from the profile the same way OBS's own
        Settings dialog stores it (Simple and Advanced output modes keep it
        under different categories). '' when the profile can't be read; the
        caller treats that as "don't know", never as an error."""
        try:
            mode = (self.request("GetProfileParameter", {
                "parameterCategory": "Output", "parameterName": "Mode",
            }) or {}).get("parameterValue") or ""
            category = "AdvOut" if mode == "Advanced" else "SimpleOutput"
            value = (self.request("GetProfileParameter", {
                "parameterCategory": category, "parameterName": "RecFormat",
            }) or {}).get("parameterValue") or ""
            return value.lower()
        except ObsConnectionError:
            raise
        except ObsError:
            return ""

    def start_record(self):
        if self.record_status()["recording"]:
            raise ObsError("OBS is already recording — stop that recording first.")
        self.request("StartRecord")

    def stop_record(self):
        """-> the path OBS wrote, or '' if it was not recording after all."""
        if not self.record_status()["recording"]:
            return ""
        return (self.request("StopRecord") or {}).get("outputPath") or ""

    def close(self):
        try:
            self._socket.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not an error
            pass


def _default_factory():
    def factory(url, timeout):
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise ObsError(
                "OBS control needs the websocket-client package — "
                "reinstall requirements.txt.") from exc
        return websocket.create_connection(url, timeout=timeout)
    return factory


def connect(host=DEFAULT_HOST, port=DEFAULT_PORT, password="",
            timeout=CONNECT_TIMEOUT, factory=None):
    """Open and identify a connection. `factory(url, timeout)` is injectable so
    tests can drive the protocol without a socket (same idea as riot_client's
    transport)."""
    factory = factory or _default_factory()
    url = f"ws://{host}:{port}"
    try:
        socket = factory(url, timeout)
    except ObsError:
        raise
    except Exception as exc:  # noqa: BLE001 - refused, timed out, DNS, ...
        raise ObsConnectionError(
            f"could not reach OBS at {host}:{port} — is OBS running with "
            f"Tools -> WebSocket Server Settings enabled? ({exc})") from exc
    return ObsClient(socket).handshake(password)


# ---------- files ----------

def playable_path(path):
    """OBS records .mkv by default and browsers cannot play it. If OBS (or the
    user) remuxed to a playable container beside it — which OBS's "Automatically
    remux to mp4" option and its Remux Recordings tool both do, same stem —
    prefer that file. Returns the original path when there is nothing better."""
    if not path:
        return path
    original = Path(path)
    if original.suffix.lower() in PLAYABLE_EXTENSIONS:
        return str(original)
    siblings = [candidate for candidate in
                (original.with_suffix(ext) for ext in PLAYABLE_EXTENSIONS)
                if candidate.exists()]
    if not siblings:
        return str(original)
    # more than one same-stem candidate (say an old .mp4 beside a fresh
    # remux): the most recently modified one is the remux of THIS recording,
    # not whichever extension happens to sort first
    return str(max(siblings, key=lambda candidate: candidate.stat().st_mtime))


def is_playable(path):
    """Worth handing to the <video> element. Includes .mkv — Chromium (the
    packaged app's WebView2, and most browsers this app meets) plays
    OBS-flavoured Matroska; the UI has an onerror fallback for the rest."""
    return bool(path) and Path(path).suffix.lower() in BROWSER_OK_EXTENSIONS


def format_playable(record_format):
    """Whether a profile RecFormat value produces a file the app can play.
    'fragmented_mp4'/'hybrid_mp4' still write .mp4, hence substring matching.
    mkv counts as playable — Chromium handles it in practice — so the
    pre-record warning fires only for flv/ts/m3u8-style formats that
    genuinely will not play. unknown/'' counts as playable too: the warning
    is for a format we KNOW is unplayable, not one we couldn't read."""
    if not record_format:
        return True
    return any(fmt in record_format.lower()
               for fmt in ("mp4", "mov", "webm", "m4v", "mkv"))


def media_type(path):
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed if (guessed or "").startswith("video/") else "video/mp4"
