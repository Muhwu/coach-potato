"""Riot API client with a sliding-window rate limiter.

Dev-key limits: 20 requests / 1 s and 100 requests / 2 min. Keys expire
every 24 h; a 403 raises ApiKeyExpiredError with a hint to refresh.
"""
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

# Platform (league-v4 host) -> regional routing for match-v5.
# account-v1 only exists on americas/asia/europe, so sea platforms use asia.
PLATFORM_ROUTING = {
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
}

DEV_KEY_LIMITS = [(20, 1.0), (100, 120.0)]

# headers whose value is a credential and must never leave this machine
SECRET_HEADERS = {"x-riot-token", "authorization"}
MAX_BODY_CHARS = 2000  # Riot error bodies are one line; this is room to spare


def scrub_secret(value, secret):
    """Replace `secret` wherever it appears in a nested dict/list/string.
    Belt-and-braces on top of redacting the known header: a key that somehow
    ended up in a URL, a body or an exception message must not be shareable."""
    if not secret or len(secret) < 8:
        return value
    if isinstance(value, str):
        return value.replace(secret, "<redacted>")
    if isinstance(value, dict):
        return {k: scrub_secret(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_secret(v, secret) for v in value]
    return value


class RiotApiError(Exception):
    """A Riot API call failed.

    `detail` is a shareable diagnostic dump of exactly what was sent and what
    came back — request URL/params/headers, response status/headers/body — with
    the API key redacted. The UI offers it as copy-to-clipboard JSON so a user
    hitting an error only some users hit can hand over the whole exchange
    without also handing over their key."""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}

    @property
    def status_code(self):
        return (self.detail.get("response") or {}).get("status")


class ApiKeyExpiredError(RiotApiError):
    pass


class NotFoundError(RiotApiError):
    pass


class RateLimiter:
    """Sliding-window limiter over one or more (max_requests, window_s) limits.

    on_wait(seconds) is called before any throttling sleep so callers can
    surface "we're rate limited" to the UI."""

    def __init__(self, limits=DEV_KEY_LIMITS, clock=time.monotonic, sleep=time.sleep,
                 on_wait=None):
        self.limits = limits
        self.clock = clock
        self.sleep = sleep
        self.on_wait = on_wait
        self._history = [deque() for _ in limits]

    def acquire(self):
        while True:
            now = self.clock()
            wait = 0.0
            for (max_req, window), history in zip(self.limits, self._history):
                while history and history[0] <= now - window:
                    history.popleft()
                if len(history) >= max_req:
                    wait = max(wait, history[0] + window - now)
            if wait <= 0:
                break
            if self.on_wait:
                self.on_wait(wait)
            self.sleep(wait)
        now = self.clock()
        for history in self._history:
            history.append(now)


class RiotClient:
    MAX_429_RETRIES = 5
    MAX_5XX_RETRIES = 3

    def __init__(self, api_key, platform="euw1", limiter=None, transport=None):
        platform = platform.lower()
        if platform not in PLATFORM_ROUTING:
            raise ValueError(
                f"Unknown platform {platform!r}. Valid: {', '.join(sorted(PLATFORM_ROUTING))}"
            )
        region = PLATFORM_ROUTING[platform]
        self.platform_host = f"https://{platform}.api.riotgames.com"
        self.match_host = f"https://{region}.api.riotgames.com"
        account_region = "asia" if region == "sea" else region
        self.account_host = f"https://{account_region}.api.riotgames.com"
        self.platform = platform
        self.limiter = limiter if limiter is not None else RateLimiter()
        self._api_key = api_key
        self._http = httpx.Client(
            headers={"X-Riot-Token": api_key},
            timeout=15.0,
            transport=transport,
        )

    def diagnostics(self, response, params=None, attempts_429=0, attempts_5xx=0):
        """Everything about one failed exchange, minus the API key: what we
        sent (method, final URL, params, headers) and what came back (status,
        headers, body). Response headers are kept because Riot's rate-limit
        counters live there and they are often the whole story."""
        request = response.request
        headers = {name: ("<redacted>" if name.lower() in SECRET_HEADERS else value)
                   for name, value in request.headers.items()}
        body = response.text or ""
        if len(body) > MAX_BODY_CHARS:
            body = f"{body[:MAX_BODY_CHARS]}… (+{len(response.text) - MAX_BODY_CHARS} chars)"
        try:
            elapsed_ms = round(response.elapsed.total_seconds() * 1000)
        except RuntimeError:  # not available if the response wasn't read
            elapsed_ms = None
        detail = {
            "when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platform": self.platform,
            "request": {"method": request.method, "url": str(request.url),
                        "params": dict(params or {}), "headers": headers},
            "response": {"status": response.status_code,
                         "reason": response.reason_phrase,
                         "headers": dict(response.headers),
                         "body": body,
                         "elapsed_ms": elapsed_ms},
            "attempts": {"rate_limited": attempts_429, "server_error": attempts_5xx},
            "hosts": {"platform": self.platform_host, "match": self.match_host,
                      "account": self.account_host},
        }
        return scrub_secret(detail, self._api_key)

    def _get(self, url, params=None):
        attempts_429 = 0
        attempts_5xx = 0
        while True:
            self.limiter.acquire()
            response = self._http.get(url, params=params)
            if response.status_code == 200:
                return response.json()
            code = response.status_code
            detail = self.diagnostics(response, params, attempts_429, attempts_5xx)
            if code in (401, 403):
                raise ApiKeyExpiredError(
                    f"An error occurred ({code}) — the dev key has likely expired. "
                    "Refresh it at https://developer.riotgames.com and update it in "
                    "Settings.", detail)
            if code == 404:
                raise NotFoundError(url, detail)
            if code == 429:
                attempts_429 += 1
                if attempts_429 > self.MAX_429_RETRIES:
                    raise RiotApiError(
                        f"An error occurred ({code}) — rate limited too many times.", detail)
                retry_after = int(response.headers.get("Retry-After", "10"))
                if self.limiter.on_wait:
                    self.limiter.on_wait(retry_after)
                self.limiter.sleep(retry_after)
                continue
            if code >= 500:
                attempts_5xx += 1
                if attempts_5xx > self.MAX_5XX_RETRIES:
                    raise RiotApiError(
                        f"An error occurred ({code}) — Riot's servers keep failing.", detail)
                self.limiter.sleep(2 * attempts_5xx)
                continue
            raise RiotApiError(f"An error occurred ({code}).", detail)

    def get_account(self, game_name, tag_line):
        url = (
            f"{self.account_host}/riot/account/v1/accounts/by-riot-id/"
            f"{quote(game_name)}/{quote(tag_line)}"
        )
        return self._get(url)

    def get_match_ids(self, puuid, queue=None, start=0, count=100, start_time=None, end_time=None):
        params = {"start": start, "count": count}
        if queue is not None:
            params["queue"] = queue
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        url = f"{self.match_host}/lol/match/v5/matches/by-puuid/{puuid}/ids"
        return self._get(url, params=params)

    def _match_host_for(self, match_id):
        """Match IDs are prefixed with the platform the game was played on
        (e.g. 'KR_123', 'EUW1_123'), and match-v5 routes by region. Derive the
        host from that prefix so a match from another region (e.g. a comparison
        player on a different server) is always fetched from the right regional
        host — not the client's configured platform region."""
        prefix = match_id.split("_", 1)[0].lower()
        region = PLATFORM_ROUTING.get(prefix)
        return f"https://{region}.api.riotgames.com" if region else self.match_host

    def get_match(self, match_id):
        return self._get(f"{self._match_host_for(match_id)}/lol/match/v5/matches/{match_id}")

    def get_match_timeline(self, match_id):
        return self._get(
            f"{self._match_host_for(match_id)}/lol/match/v5/matches/{match_id}/timeline")

    def get_league_entries(self, puuid):
        return self._get(f"{self.platform_host}/lol/league/v4/entries/by-puuid/{puuid}")

    def get_active_game(self, puuid):
        """Current live game for a puuid (spectator-v5, platform host). Riot
        returns 404 when the player isn't in a game — the caller treats that
        as 'not live' rather than an error."""
        return self._get(
            f"{self.platform_host}/lol/spectator/v5/active-games/by-puuid/{puuid}")
