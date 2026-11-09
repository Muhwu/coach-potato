"""No-looks watcher against a scripted fake League client — no network."""
import httpx

from server import lcu


class FakeLcu:
    """Answers like the League client's local API. `phase`/`session`/`eog`
    are mutable so a test can walk a game through its phases."""

    def __init__(self, phase="None", session=None, eog=None, dismiss_status=204):
        self.phase = phase
        self.session = session or {}
        self.eog = eog
        self.dismiss_status = dismiss_status
        self.dismissed = 0
        self.alive = True

    def handler(self, request):
        if not self.alive:
            raise httpx.ConnectError("client closed", request=request)
        path = request.url.path
        if path == "/lol-gameflow/v1/gameflow-phase":
            return httpx.Response(200, json=self.phase)
        if path == "/lol-gameflow/v1/session":
            return httpx.Response(200, json=self.session)
        if path == "/lol-end-of-game/v1/eog-stats-block":
            return httpx.Response(200, json=self.eog) if self.eog else httpx.Response(404)
        if path == "/lol-end-of-game/v1/state/dismiss-stats" and request.method == "POST":
            if self.dismiss_status < 300:
                self.dismissed += 1
            return httpx.Response(self.dismiss_status)
        return httpx.Response(404)

    def client(self):
        return lcu.LcuClient(1234, "pw", transport=httpx.MockTransport(self.handler))


def session(game_id, queue_id, queue_type):
    return {"gameData": {"gameId": game_id, "queue": {"id": queue_id, "type": queue_type}}}


def watcher_for(fake):
    return lcu.NoLooksWatcher(connect_fn=fake.client, clock=lambda: 1_700_000_000)


def test_parse_lockfile():
    assert lcu.parse_lockfile("LeagueClient:1234:54321:s3cret:https") == (54321, "s3cret")
    assert lcu.parse_lockfile("") is None
    assert lcu.parse_lockfile("garbage") is None


def test_find_credentials_skips_missing_and_bad_lockfiles(tmp_path):
    bad = tmp_path / "bad"
    bad.write_text("nope")
    good = tmp_path / "good"
    good.write_text("LeagueClient:1:2999:pw:https")
    assert lcu.find_credentials([tmp_path / "missing", bad, good]) == (2999, "pw")
    assert lcu.find_credentials([tmp_path / "missing"]) is None


def test_solo_duo_end_of_game_screen_is_dismissed_once():
    fake = FakeLcu("InProgress", session(77, 420, "RANKED_SOLO_5x5"))
    w = watcher_for(fake)
    assert w.tick() == lcu.POLL_S and w.connected
    fake.phase = "PreEndOfGame"
    assert w.tick() == lcu.FAST_POLL_S
    fake.phase = "EndOfGame"
    w.tick()
    w.tick()  # still on the screen for a moment: no second dismiss
    assert fake.dismissed == 1
    assert w.skipped == 1 and w.last_skipped_ms == 1_700_000_000_000


def test_other_queues_are_left_alone():
    fake = FakeLcu("InProgress", session(5, 440, "RANKED_FLEX_SR"))
    w = watcher_for(fake)
    w.tick()
    fake.phase = "EndOfGame"
    w.tick()
    assert fake.dismissed == 0 and w.skipped == 0


def test_started_on_the_eog_screen_asks_the_stats_block():
    fake = FakeLcu("EndOfGame", eog={"gameId": 9, "queueType": "RANKED_SOLO_5x5"})
    w = watcher_for(fake)
    w.tick()
    assert fake.dismissed == 1


def test_dismiss_too_early_is_retried():
    fake = FakeLcu("InProgress", session(3, 420, "RANKED_SOLO_5x5"), dismiss_status=404)
    w = watcher_for(fake)
    w.tick()
    fake.phase = "EndOfGame"
    w.tick()
    assert w.skipped == 0
    fake.dismiss_status = 204
    w.tick()
    assert fake.dismissed == 1 and w.skipped == 1


def test_client_not_running_or_closing_never_raises():
    w = lcu.NoLooksWatcher(connect_fn=lambda: None)
    assert w.tick() == lcu.IDLE_POLL_S and not w.connected
    fake = FakeLcu("Lobby")
    w = watcher_for(fake)
    w.tick()
    assert w.connected
    fake.alive = False
    assert w.tick() == lcu.IDLE_POLL_S and not w.connected
    fake.alive = True
    w.tick()  # reconnects by re-reading the lockfile
    assert w.connected


def test_status_reports_nothing_live_while_off():
    fake = FakeLcu("Lobby")
    w = watcher_for(fake)
    w.tick()
    assert w.status()["enabled"] is False and w.status()["client_connected"] is False
