"""No-looks: skip the LP screen after a ranked Solo/Duo game.

Ported from NoLooksies (https://github.com/WoedWoef/NoLooksies, MIT), itself
based on Timmifer's "no look challenge": you play for improvement, not for the
LP number. When a Solo/Duo game reaches the client's end-of-game phase, we ask
the League client to dismiss its stats screen before you've read your LP gain
or loss.

This talks to the League CLIENT's own local API (the "LCU", on 127.0.0.1 with
a self-signed certificate and a per-launch password from its `lockfile`) — not
the Riot web API, so it needs no API key and has nothing to do with the
crawler's rate limits. NoLooksies subscribed to the client's websocket; we
poll `gameflow-phase` instead (one tiny local GET a second or two, faster near
the end of a game), which needs no extra dependency and survives client
restarts without reconnect logic.

Nothing here may ever raise into the app: the client not running, a stale
lockfile or an odd response just mean "not connected" and the watcher keeps
looking.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx

RANKED_SOLO_QUEUE_ID = 420
RANKED_SOLO_QUEUE_TYPE = "RANKED_SOLO_5x5"

# phases during which a game exists whose queue we want to remember, so the
# end-of-game check never has to guess
GAME_PHASES = {"ChampSelect", "GameStart", "InProgress", "Reconnect",
               "WaitingForStats", "PreEndOfGame", "EndOfGame"}
# near the end of a game, poll fast so the LP screen is gone before it's read
FAST_PHASES = {"WaitingForStats", "PreEndOfGame", "EndOfGame"}

FAST_POLL_S = 0.5
POLL_S = 2.0
IDLE_POLL_S = 5.0   # client not running / not found


def _windows_install_dirs():
    """League install folders the Riot Client knows about, from its own
    RiotClientInstalls.json, plus the default location."""
    dirs = []
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    installs = Path(program_data) / "Riot Games" / "RiotClientInstalls.json"
    try:
        data = json.loads(installs.read_text(encoding="utf-8"))
        for key in (data.get("associated_client") or {}):
            dirs.append(Path(key))
    except (OSError, ValueError, AttributeError):
        pass
    dirs.append(Path(r"C:\Riot Games\League of Legends"))
    return dirs


def lockfile_candidates():
    if sys.platform == "darwin":
        return [Path("/Applications/League of Legends.app/Contents/LoL/lockfile")]
    if os.name == "nt":
        return [d / "lockfile" for d in _windows_install_dirs()]
    return []  # the League client doesn't run elsewhere


def parse_lockfile(text):
    """`LeagueClient:<pid>:<port>:<password>:<protocol>` -> (port, password),
    or None if it doesn't look like one."""
    parts = text.strip().split(":")
    if len(parts) < 5 or not parts[2].isdigit() or not parts[3]:
        return None
    return int(parts[2]), parts[3]


def find_credentials(candidates=None):
    for path in candidates if candidates is not None else lockfile_candidates():
        try:
            creds = parse_lockfile(Path(path).read_text(encoding="utf-8"))
        except OSError:
            continue
        if creds:
            return creds
    return None


class LcuClient:
    """Minimal League-client API wrapper. `transport` is injectable for tests."""

    def __init__(self, port, password, transport=None):
        self._http = httpx.Client(
            base_url=f"https://127.0.0.1:{port}", auth=("riot", password),
            verify=False,  # the client serves a self-signed certificate
            timeout=3.0, transport=transport)

    def get(self, path):
        response = self._http.get(path)
        response.raise_for_status()
        return response.json()

    def post(self, path):
        response = self._http.post(path)
        response.raise_for_status()

    def close(self):
        self._http.close()


def connect():
    creds = find_credentials()
    return LcuClient(*creds) if creds else None


def _is_ranked_solo(queue_id, queue_type):
    return queue_id == RANKED_SOLO_QUEUE_ID or queue_type == RANKED_SOLO_QUEUE_TYPE


class NoLooksWatcher:
    """Background poller. `tick()` is one step (tests drive it directly);
    `set_enabled()` starts/stops the daemon thread that loops it."""

    def __init__(self, connect_fn=connect, clock=time.time):
        self._connect = connect_fn
        self._clock = clock
        self._client = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self.enabled = False
        self.connected = False
        self.phase = None
        self.skipped = 0
        self.last_skipped_ms = None
        self._game = None           # (game_id, queue_id, queue_type) of the game seen
        self._dismissed_game = None  # game id we already skipped the screen for

    # ---- one poll step -------------------------------------------------
    def _drop_client(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self.connected = False
        self.phase = None

    def tick(self):
        """Poll once; returns how long to wait before the next poll."""
        if self._client is None:
            self._client = self._connect()
            if self._client is None:
                self.connected = False
                return IDLE_POLL_S
        try:
            phase = self._client.get("/lol-gameflow/v1/gameflow-phase")
            self.connected = True
            self.phase = phase
            if phase in GAME_PHASES:
                self._remember_game()
            if phase == "EndOfGame":
                self._maybe_dismiss()
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError):
            # client closed (stale lockfile / new port) or mid-restart:
            # re-read the lockfile next time
            self._drop_client()
            return IDLE_POLL_S
        return FAST_POLL_S if phase in FAST_PHASES else POLL_S

    def _remember_game(self):
        try:
            session = self._client.get("/lol-gameflow/v1/session") or {}
        except (httpx.HTTPStatusError, ValueError):
            return
        game = session.get("gameData") or {}
        queue = game.get("queue") or {}
        if game.get("gameId"):
            self._game = (game["gameId"], queue.get("id"), queue.get("type"))

    def _maybe_dismiss(self):
        game_id, queue_id, queue_type = self._game or (None, None, None)
        if queue_id is None and queue_type is None:
            # never saw the game (app started on the EOG screen): ask the
            # end-of-game block itself
            try:
                block = self._client.get("/lol-end-of-game/v1/eog-stats-block") or {}
            except httpx.HTTPStatusError:
                return  # stats not loaded yet; retry on the next tick
            game_id = block.get("gameId")
            queue_type = block.get("queueType")
        if not _is_ranked_solo(queue_id, queue_type):
            return
        if game_id is not None and game_id == self._dismissed_game:
            return
        try:
            self._client.post("/lol-end-of-game/v1/state/dismiss-stats")
        except httpx.HTTPStatusError:
            return  # too early; the next (fast) tick tries again
        self._dismissed_game = game_id
        self.skipped += 1
        self.last_skipped_ms = int(self._clock() * 1000)

    # ---- thread ----------------------------------------------------------
    def _run(self):
        while self.enabled:
            try:
                delay = self.tick()
            except Exception:  # never let one odd response kill the watcher
                self._drop_client()
                delay = IDLE_POLL_S
            self._wake.wait(delay)
            self._wake.clear()
        self._drop_client()

    def set_enabled(self, enabled):
        with self._lock:
            self.enabled = bool(enabled)
            if self.enabled and not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(
                    target=self._run, name="no-looks", daemon=True)
                self._thread.start()
            elif not self.enabled:
                self._wake.set()  # let the loop notice promptly

    def status(self):
        return {
            "enabled": self.enabled,
            "client_connected": self.enabled and self.connected,
            "phase": self.phase if self.enabled else None,
            "skipped": self.skipped,
            "last_skipped_ms": self.last_skipped_ms,
        }
