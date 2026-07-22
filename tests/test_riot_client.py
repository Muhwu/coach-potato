import json

import httpx
import pytest

from server.riot_client import (
    ApiKeyExpiredError,
    NotFoundError,
    RateLimiter,
    RiotClient,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


# ---------- RateLimiter ----------

def test_limiter_allows_burst_within_limit():
    clock = FakeClock()
    rl = RateLimiter(limits=[(20, 1.0)], clock=clock.time, sleep=clock.sleep)
    for _ in range(20):
        rl.acquire()
    assert clock.slept == []


def test_limiter_sleeps_when_short_window_exhausted():
    clock = FakeClock()
    rl = RateLimiter(limits=[(20, 1.0)], clock=clock.time, sleep=clock.sleep)
    for _ in range(20):
        rl.acquire()
    rl.acquire()  # 21st within the same second must wait
    assert len(clock.slept) == 1
    assert clock.slept[0] > 0


def test_limiter_enforces_long_window():
    clock = FakeClock()
    rl = RateLimiter(limits=[(20, 1.0), (100, 120.0)], clock=clock.time, sleep=clock.sleep)
    for i in range(100):
        rl.acquire()
        clock.now = (i + 1) * 1.0  # spread out: never trips 20/1s
    total_slept_before = sum(clock.slept)
    rl.acquire()  # 101st within 120s window
    assert sum(clock.slept) > total_slept_before


def test_limiter_window_expires():
    clock = FakeClock()
    rl = RateLimiter(limits=[(2, 1.0)], clock=clock.time, sleep=clock.sleep)
    rl.acquire()
    rl.acquire()
    clock.now = 2.0
    rl.acquire()  # old window expired, no sleep
    assert clock.slept == []


def test_limiter_reports_waits_via_on_wait():
    clock = FakeClock()
    waits = []
    rl = RateLimiter(limits=[(2, 10.0)], clock=clock.time, sleep=clock.sleep,
                     on_wait=waits.append)
    rl.acquire()
    rl.acquire()
    assert waits == []
    rl.acquire()  # third call within the window must wait ~10s
    assert len(waits) == 1
    assert waits[0] == pytest.approx(10.0)


def test_client_429_reports_wait_via_limiter_on_wait():
    calls = {"n": 0}
    clock = FakeClock()
    waits = []
    limiter = RateLimiter(limits=[(1000, 1.0)], clock=clock.time, sleep=clock.sleep,
                          on_wait=waits.append)

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"puuid": "abc"})

    make_client(handler, limiter=limiter).get_account("PlayerOne", "EUW")
    assert 7 in waits


# ---------- RiotClient ----------

def make_client(handler, limiter=None, platform="euw1"):
    transport = httpx.MockTransport(handler)
    return RiotClient("RGAPI-test", platform=platform, limiter=limiter, transport=transport)


def url_seen(handler_urls):
    def handler(request):
        handler_urls.append(str(request.url))
        return httpx.Response(200, json=[])
    return handler


def test_na1_platform_routes_to_americas():
    urls = []
    client = make_client(url_seen(urls), platform="na1")
    client.get_match_ids("abc", queue=420)
    client.get_league_entries("abc")
    assert urls[0].startswith("https://americas.api.riotgames.com/lol/match/v5/")
    assert urls[1].startswith("https://na1.api.riotgames.com/lol/league/v4/")


def test_oc1_uses_sea_for_matches_but_asia_for_account():
    urls = []
    client = make_client(url_seen(urls), platform="oc1")
    client.get_match_ids("abc", queue=420)
    assert urls[0].startswith("https://sea.api.riotgames.com/")

    def account_handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"puuid": "x"})

    make_client(account_handler, platform="oc1").get_account("Foo", "OCE")
    assert urls[1].startswith("https://asia.api.riotgames.com/riot/account/v1/")


def test_unknown_platform_raises_value_error():
    with pytest.raises(ValueError):
        RiotClient("RGAPI-test", platform="moon1")


def test_get_account_hits_europe_route_with_token_header():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Riot-Token")
        return httpx.Response(200, json={"puuid": "abc", "gameName": "PlayerOne", "tagLine": "EUW"})

    client = make_client(handler)
    account = client.get_account("PlayerOne", "EUW")
    assert account["puuid"] == "abc"
    assert seen["url"] == "https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/PlayerOne/EUW"
    assert seen["token"] == "RGAPI-test"


def test_get_account_encodes_unicode_names():
    seen = {}

    def handler(request):
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"puuid": "x"})

    make_client(handler).get_account("Ünï cödé", "EUW")
    assert "%C3%9Cn%C3%AF%20c%C3%B6d%C3%A9" in seen["path"]


def test_get_match_ids_passes_params():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=["EUW1_1", "EUW1_2"])

    ids = make_client(handler).get_match_ids("abc", queue=420, start=0, count=100, start_time=1700000000)
    assert ids == ["EUW1_1", "EUW1_2"]
    assert "matches/by-puuid/abc/ids" in seen["url"]
    assert "queue=420" in seen["url"]
    assert "count=100" in seen["url"]
    assert "startTime=1700000000" in seen["url"]


def test_get_match_routes_by_match_id_region_not_client_platform():
    # a euw1 client fetching a KR match must hit asia (the match-id's region),
    # not europe — otherwise cross-region matches (e.g. comparison players on
    # another server) 404. Regression: europe.api.../KR_... crawl failure.
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"metadata": {"matchId": "KR_1"}})

    client = make_client(handler, platform="euw1")  # client region = europe
    client.get_match("KR_8229729938")
    assert "https://asia.api.riotgames.com" in seen["url"]
    client.get_match_timeline("KR_8229729938")
    assert "https://asia.api.riotgames.com" in seen["url"]
    client.get_match("EUW1_5")  # same-region match still uses europe
    assert "https://europe.api.riotgames.com" in seen["url"]


def test_get_league_entries_uses_platform_host():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"}])

    entries = make_client(handler).get_league_entries("abc")
    assert entries[0]["tier"] == "GOLD"
    assert seen["url"].startswith("https://euw1.api.riotgames.com/lol/league/v4/entries/by-puuid/abc")


def test_403_raises_api_key_expired():
    def handler(request):
        return httpx.Response(403, json={"status": {"message": "Forbidden"}})

    with pytest.raises(ApiKeyExpiredError):
        make_client(handler).get_account("PlayerOne", "EUW")


def test_404_raises_not_found():
    def handler(request):
        return httpx.Response(404, json={"status": {"message": "not found"}})

    with pytest.raises(NotFoundError):
        make_client(handler).get_account("Nobody", "EUW")


def test_429_sleeps_retry_after_then_retries():
    calls = {"n": 0}
    clock = FakeClock()
    limiter = RateLimiter(limits=[(1000, 1.0)], clock=clock.time, sleep=clock.sleep)

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json={"puuid": "abc"})

    client = make_client(handler, limiter=limiter)
    account = client.get_account("PlayerOne", "EUW")
    assert account["puuid"] == "abc"
    assert calls["n"] == 2
    assert 3 in clock.slept


def test_5xx_retries_then_succeeds():
    calls = {"n": 0}
    clock = FakeClock()
    limiter = RateLimiter(limits=[(1000, 1.0)], clock=clock.time, sleep=clock.sleep)

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"puuid": "abc"})

    account = make_client(handler, limiter=limiter).get_account("PlayerOne", "EUW")
    assert account["puuid"] == "abc"
    assert calls["n"] == 3
