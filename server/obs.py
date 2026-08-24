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
video files are never copied, moved or deleted (only paths are stored), and
"forget this recording" forgets the row, not the file.

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
# Ops we send/receive. Events (op 5) are never subscribed to (eventSubscriptions
# 0) but are skipped defensively anyway, since a future OBS could send them.
OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 6, 7

# Containers a <video> element can actually play. OBS's DEFAULT recording
# format is .mkv (it survives a crash mid-recording), which browsers do not
# play — hence `playable_path()` below, and the hint the UI shows.
PLAYABLE_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")


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

    def _recv_op(self, wanted):
        """Next message with the given op, skipping events."""
        while True:
            message = self._recv()
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
        identify = {"rpcVersion": RPC_VERSION, "eventSubscriptions": 0}
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
    for extension in PLAYABLE_EXTENSIONS:
        sibling = original.with_suffix(extension)
        if sibling.exists():
            return str(sibling)
    return str(original)


def is_playable(path):
    return bool(path) and Path(path).suffix.lower() in PLAYABLE_EXTENSIONS


def media_type(path):
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed if (guessed or "").startswith("video/") else "video/mp4"
