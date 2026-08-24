"""obs-websocket v5 protocol, driven against a scripted fake socket — no
network, same idea as the FakeClient/MockTransport used for Riot."""
import base64
import hashlib
import json

import pytest

from server import obs


class FakeSocket:
    """Hands back scripted server messages; records what the client sent.
    A scripted Exception is raised instead of returned (dropped connection)."""

    def __init__(self, *script):
        self.script = list(script)
        self.sent = []
        self.closed = False

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        if not self.script:
            raise ConnectionError("socket closed")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    def close(self):
        self.closed = True


HELLO = {"op": 0, "d": {"obsWebSocketVersion": "5.4.2", "rpcVersion": 1}}
HELLO_AUTH = {"op": 0, "d": {
    "obsWebSocketVersion": "5.4.2", "rpcVersion": 1,
    "authentication": {"challenge": "chal", "salt": "salt"}}}
IDENTIFIED = {"op": 2, "d": {"negotiatedRpcVersion": 1}}


def response(request_id, data=None, ok=True, comment=None):
    return {"op": 7, "d": {"requestType": "X", "requestId": request_id,
                           "requestStatus": {"result": ok, "code": 100 if ok else 604,
                                             "comment": comment},
                           "responseData": data or {}}}


def status_response(request_id, active=False, paused=False, duration=0):
    return response(request_id, {"outputActive": active, "outputPaused": paused,
                                 "outputDuration": duration})


# ---------- auth ----------

def test_auth_response_follows_the_documented_steps_in_order():
    """obs-websocket v5 spells the auth string out as four steps: sha256 the
    password+salt, base64 it, sha256 THAT + challenge, base64 again. Written
    longhand here so a reordering (a classic way to get this subtly wrong)
    fails loudly rather than just producing a different-looking string."""
    password, salt, challenge = "supersecretpassword", "s4lt=", "ch4llenge="
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode()).digest()).decode()
    expected = base64.b64encode(
        hashlib.sha256((secret + challenge).encode()).digest()).decode()
    assert obs.auth_response(password, salt, challenge) == expected
    # salt and challenge are not interchangeable — swapping them must differ
    assert obs.auth_response(password, challenge, salt) != expected


def test_auth_response_depends_on_every_input():
    base = obs.auth_response("pw", "salt", "chal")
    assert base != obs.auth_response("pw2", "salt", "chal")
    assert base != obs.auth_response("pw", "salt2", "chal")
    assert base != obs.auth_response("pw", "salt", "chal2")


# ---------- handshake ----------

def test_handshake_without_a_password_sends_a_bare_identify():
    socket = FakeSocket(HELLO, IDENTIFIED)
    client = obs.ObsClient(socket).handshake()
    assert client.websocket_version == "5.4.2"
    identify = socket.sent[0]
    assert identify["op"] == obs.OP_IDENTIFY
    assert identify["d"]["rpcVersion"] == obs.RPC_VERSION
    assert "authentication" not in identify["d"]


def test_handshake_answers_the_challenge_when_one_is_offered():
    socket = FakeSocket(HELLO_AUTH, IDENTIFIED)
    obs.ObsClient(socket).handshake("hunter2")
    assert socket.sent[0]["d"]["authentication"] == obs.auth_response(
        "hunter2", "salt", "chal")


def test_handshake_explains_a_missing_password():
    with pytest.raises(obs.ObsError, match="password"):
        obs.ObsClient(FakeSocket(HELLO_AUTH, IDENTIFIED)).handshake()


def test_handshake_reads_a_dropped_socket_as_a_wrong_password():
    # OBS closes the connection (code 4009) instead of replying to a bad auth
    socket = FakeSocket(HELLO_AUTH, ConnectionError("closed"))
    with pytest.raises(obs.ObsError, match="does not match"):
        obs.ObsClient(socket).handshake("wrong")


# ---------- requests ----------

def connected(*script):
    return obs.ObsClient(FakeSocket(HELLO, IDENTIFIED, *script)).handshake()


def test_request_returns_the_matching_response_data():
    client = connected(response("1", {"outputPath": "C:/vods/a.mkv"}))
    assert client.request("StopRecord") == {"outputPath": "C:/vods/a.mkv"}


def test_request_skips_events_and_responses_meant_for_someone_else():
    client = connected({"op": 5, "d": {"eventType": "SceneChanged"}},
                       response("99", {"stale": True}),
                       response("1", {"fresh": True}))
    assert client.request("GetVersion") == {"fresh": True}


def test_request_raises_with_obs_own_comment():
    client = connected(response("1", ok=False, comment="Output not running"))
    with pytest.raises(obs.ObsError, match="Output not running"):
        client.request("StopRecord")


def test_a_dropped_socket_is_a_connection_error_so_callers_can_reconnect():
    client = connected(ConnectionError("boom"))
    with pytest.raises(obs.ObsConnectionError):
        client.request("GetVersion")
    # ...and a plain refusal is NOT, since retrying it would just fail again
    client = connected(response("1", ok=False, comment="nope"))
    with pytest.raises(obs.ObsError) as excinfo:
        client.request("StartRecord")
    assert not isinstance(excinfo.value, obs.ObsConnectionError)


# ---------- recording ----------

def test_record_status_normalises_obs_fields():
    client = connected(status_response("1", active=True, paused=True, duration=61_000))
    assert client.record_status() == {"recording": True, "paused": True,
                                      "duration_ms": 61_000}


def test_start_record_checks_first_and_refuses_to_double_start():
    client = connected(status_response("1", active=True))
    with pytest.raises(obs.ObsError, match="already recording"):
        client.start_record()


def test_start_record_sends_startrecord_when_idle():
    client = connected(status_response("1"), response("2"))
    client.start_record()
    assert [m["d"]["requestType"] for m in client._socket.sent
            if m["op"] == obs.OP_REQUEST] == ["GetRecordStatus", "StartRecord"]


def test_stop_record_returns_the_output_path():
    client = connected(status_response("1", active=True),
                       response("2", {"outputPath": "D:/obs/session.mp4"}))
    assert client.stop_record() == "D:/obs/session.mp4"


def test_stop_record_is_a_no_op_when_obs_is_not_recording():
    client = connected(status_response("1", active=False))
    assert client.stop_record() == ""


# ---------- connect ----------

def test_connect_wraps_an_unreachable_obs_with_a_useful_hint():
    def refuse(url, timeout):
        raise ConnectionRefusedError(url)

    with pytest.raises(obs.ObsConnectionError, match="WebSocket Server Settings"):
        obs.connect(factory=refuse)


def test_connect_identifies_and_returns_a_live_client():
    captured = {}

    def factory(url, timeout):
        captured["url"] = url
        return FakeSocket(HELLO, IDENTIFIED)

    client = obs.connect(host="1.2.3.4", port=1234, factory=factory)
    assert captured["url"] == "ws://1.2.3.4:1234"
    assert isinstance(client, obs.ObsClient)


# ---------- files ----------

def test_playable_path_prefers_a_remux_sitting_beside_an_mkv(tmp_path):
    mkv = tmp_path / "session.mkv"
    mkv.write_bytes(b"x")
    assert obs.playable_path(str(mkv)) == str(mkv)  # nothing better yet
    mp4 = tmp_path / "session.mp4"
    mp4.write_bytes(b"x")
    assert obs.playable_path(str(mkv)) == str(mp4)


def test_playable_path_leaves_an_already_playable_file_alone(tmp_path):
    mp4 = tmp_path / "session.mp4"
    mp4.write_bytes(b"x")
    assert obs.playable_path(str(mp4)) == str(mp4)
    assert obs.playable_path("") == ""


def test_is_playable_knows_which_containers_a_browser_can_play():
    assert obs.is_playable("a.mp4") and obs.is_playable("A.WEBM")
    # mkv IS handed to the player — Chromium demuxes OBS's Matroska in
    # practice, and the UI keeps an onerror fallback for the codecs it can't
    assert obs.is_playable("a.mkv")
    assert not obs.is_playable("a.flv") and not obs.is_playable("")


def test_media_type_falls_back_to_mp4_for_unknown_containers():
    assert obs.media_type("a.mp4") == "video/mp4"
    assert obs.media_type("a.weird").startswith("video/")


def profile_response(request_id, value):
    return response(request_id, {"parameterValue": value})


def test_record_format_reads_simple_output_mode():
    client = connected(profile_response("1", "Simple"), profile_response("2", "MKV"))
    assert client.record_format() == "mkv"
    categories = [m["d"]["requestData"]["parameterCategory"]
                  for m in client._socket.sent if m["op"] == obs.OP_REQUEST]
    assert categories == ["Output", "SimpleOutput"]


def test_record_format_reads_advanced_output_mode():
    client = connected(profile_response("1", "Advanced"), profile_response("2", "mp4"))
    assert client.record_format() == "mp4"
    categories = [m["d"]["requestData"]["parameterCategory"]
                  for m in client._socket.sent if m["op"] == obs.OP_REQUEST]
    assert categories == ["Output", "AdvOut"]


def test_record_format_is_blank_when_the_profile_cannot_be_read():
    # an OBS refusing GetProfileParameter must not break the Record button
    client = connected(response("1", ok=False, comment="unknown request"))
    assert client.record_format() == ""


def test_format_playable_knows_the_fragmented_variants():
    assert obs.format_playable("mp4")
    assert obs.format_playable("fragmented_mp4")
    assert obs.format_playable("hybrid_mp4")
    assert obs.format_playable("mov") and obs.format_playable("webm")
    assert obs.format_playable("mkv")   # plays in Chromium — no warning
    assert obs.format_playable("")      # unknown = no warning, not a warning
    assert not obs.format_playable("flv")
    assert not obs.format_playable("mpegts")


# ---------- events (recording stopped in OBS itself) ----------

def record_stopped_event(path):
    return {"op": 5, "d": {"eventType": "RecordStateChanged",
                           "eventData": {"outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                                         "outputActive": False, "outputPath": path}}}


def test_handshake_subscribes_to_output_events():
    socket = FakeSocket(HELLO, IDENTIFIED)
    obs.ObsClient(socket).handshake()
    assert socket.sent[0]["d"]["eventSubscriptions"] == obs.EVENT_SUBSCRIPTION_OUTPUTS


def test_a_stop_event_passing_by_hands_over_the_file_path():
    # the event is queued BEFORE the poll's response — reading the response
    # drains it, which is exactly how a stop-in-OBS gets its path to us
    client = connected(record_stopped_event("D:/obs/stopped-in-obs.mkv"),
                       status_response("1", active=False))
    assert client.record_status()["recording"] is False
    assert client.take_last_output_path() == "D:/obs/stopped-in-obs.mkv"
    assert client.take_last_output_path() == ""  # one-shot


def test_other_record_events_do_not_overwrite_the_path():
    started = {"op": 5, "d": {"eventType": "RecordStateChanged",
                              "eventData": {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED",
                                            "outputActive": True}}}
    client = connected(record_stopped_event("D:/keep.mkv"), started,
                       status_response("1", active=True))
    client.record_status()
    assert client.take_last_output_path() == "D:/keep.mkv"
