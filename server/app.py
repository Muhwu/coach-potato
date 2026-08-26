"""FastAPI app: JSON API over the sqlite db + static frontend."""
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import (ascent_log, config, crypto, db, obs, pdf_export, recordings,
               rune_data, stats, youtube)
from .config import PROJECT_ROOT
from .metrics import METRICS
from .riot_client import PLATFORM_ROUTING

app = FastAPI(title="Coach Potato")

CRAWL_STATE = {"running": False, "message": "idle", "last_result": None, "error": None,
               "rate_limited": False}
# Background fetch of match timelines for block games (deeper lane-delta
# stats), separate from the full crawl but sharing the same Riot rate budget.
TIMELINE_STATE = {"running": False, "done": 0, "total": 0, "error": None}


def _riot_job_running():
    """True while any background job is making Riot API calls — a full crawl,
    the block-timeline backfill, or a comparison-player fetch. None may run at
    once or they'd each drive their own rate limiter and together exceed Riot's
    limits. (COMPARISON_CRAWL is defined lower down; guard for import order.)"""
    return (CRAWL_STATE["running"] or TIMELINE_STATE["running"]
            or COMPARISON_CRAWL["running"])


def _champion_ids():
    """Valid DDragon champion ids from the static roster file (patched by
    re-running the DDragon fetch; see CLAUDE.md)."""
    try:
        data = json.loads((PROJECT_ROOT / "static" / "champions.json").read_text())
        return {c["id"] for c in data["champions"]}
    except (OSError, KeyError, ValueError):
        return set()  # roster file missing/corrupt: skip validation rather than break


CHAMPION_IDS = _champion_ids()


RUNE_TREE_NAMES, RUNE_NAMES, RUNE_SHARD_NAMES = (
    rune_data.TREE_NAMES, rune_data.RUNE_NAMES, rune_data.SHARD_NAMES)
_RUNE_ALL_NAMES = set(RUNE_NAMES) | set(RUNE_TREE_NAMES) | set(RUNE_SHARD_NAMES)

RANGE_PRESETS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90, "180d": 180, "365d": 365}


def get_db_path() -> Path:
    return config.default_db_path()


def get_conn():
    return db.connect(get_db_path())


def get_clips_dir() -> Path:
    d = get_db_path().parent / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


MAX_CLIP_BYTES = 50 * 1024 * 1024  # 50 MB
ALLOWED_CLIP_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
CLIP_OWNER_TABLES = {"session": "coaching_sessions", "block_game": "block_games"}


def get_background_dir() -> Path:
    d = get_db_path().parent / "background"
    d.mkdir(parents=True, exist_ok=True)
    return d


MAX_BACKGROUND_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_BACKGROUND_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def get_research_screenshots_dir() -> Path:
    d = get_db_path().parent / "research-screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


MAX_SCREENSHOT_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _unlink_clip_files(file_names):
    for name in file_names:
        (get_clips_dir() / name).unlink(missing_ok=True)


def _unlink_screenshot_files(file_names):
    for name in file_names:
        (get_research_screenshots_dir() / name).unlink(missing_ok=True)


def parse_time_range(params: dict, now_ms: int | None = None):
    """Return (from_ms, to_ms) from either range=7d|14d|... or from/to ISO dates."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    preset = params.get("range")
    if preset and preset != "all":
        if preset not in RANGE_PRESETS:
            raise HTTPException(400, f"unknown range {preset!r}")
        return (now_ms - RANGE_PRESETS[preset] * 86_400_000, None)
    from_ms = to_ms = None
    if params.get("from"):
        dt = datetime.strptime(params["from"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        from_ms = int(dt.timestamp() * 1000)
    if params.get("to"):
        dt = datetime.strptime(params["to"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_ms = int(dt.timestamp() * 1000) + 86_400_000 - 1  # inclusive end of day
    return (from_ms, to_ms)


def stat_filters(request: Request, conn):
    """Common stat query params. `puuid` may repeat (multi-account) or be
    absent (= all tracked accounts)."""
    params = dict(request.query_params)
    from_ms, to_ms = parse_time_range(params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    return {
        "puuid": request.query_params.getlist("puuid") or _tracked_puuids(conn),
        "from_ms": from_ms,
        "to_ms": to_ms,
        "champion": params.get("champion") or None,
        "queues": queues,
        "rank_tier": params.get("rank_tier") or None,
        "min_games": int(params.get("min_games", 1)),
        "side": params.get("side") or None,  # "blue" | "red" | None (both)
        # role filter: repeatable ?role=TOP&role=JUNGLE (the client sends the
        # tracked player's role(s)); empty = all roles
        "roles": request.query_params.getlist("role") or None,
    }


@app.get("/api/version")
def api_version():
    return {"version": config.app_version(), "repo": config.GITHUB_REPO}


# must stay in step with the .view-toggle-cb checkboxes in index.html — a view
# offered there but missing here makes saving settings 400
HIDEABLE_VIEWS = {"overview", "matchups", "progress", "trends", "blocks", "series",
                  "pool", "guide", "research", "players", "tiers"}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hidden_views(conn):
    raw = db.get_settings(conn).get("hidden_views")
    return json.loads(raw) if raw else []


DEFAULT_AUTO_CRAWL_HOURS = 3


def _extra_settings(conn):
    stored = db.get_settings(conn)
    hours = stored.get("auto_crawl_hours")
    last = stored.get("last_crawl_ms")
    return {
        "hidden_views": _hidden_views(conn),
        "auto_crawl_hours": int(hours) if hours is not None else DEFAULT_AUTO_CRAWL_HOURS,
        "last_crawl_ms": int(last) if last else None,
        "hide_my_rank": stored.get("hide_my_rank") == "1",
        "block_size": db.get_block_size(conn),
        "block_gap_hours": db.get_block_gap_ms(conn) / 3_600_000,
        "block_gap_confirm": stored.get("block_gap_confirm") != "0",
        "block_series_enabled": stored.get("block_series_enabled") != "0",
        "ui_opacity": int(stored.get("ui_opacity") or 100),
        "background_image": bool(stored.get("background_image_file")),
        "accent_color": stored.get("accent_color") or None,
        "date_format": stored.get("date_format") or "iso",
        "runes_mode": stored.get("runes_mode") or "matchup",
        "enable_player_comparison": stored.get("enable_player_comparison") == "1",
        "main_role": stored.get("main_role") or "",         # team_position or ""
        "secondary_role": stored.get("secondary_role") or "",
        "ascent_db_path": stored.get("ascent_db_path") or "",
        "ascent_db_detected": str(recordings.default_ascent_db_path() or ""),
        "youtube_client_secrets": stored.get("youtube_client_secrets") or "",
        "youtube_privacy": stored.get("youtube_privacy") or youtube.DEFAULT_PRIVACY,
        "youtube_ready": youtube.has_credentials(
            stored.get("youtube_client_secrets"), get_db_path().parent),
        # the whole OBS session-recording feature can be switched off; the UI
        # then stops offering it (existing recordings stay listed and playable)
        "obs_enabled": stored.get("obs_enabled") != "0",
        "obs_host": stored.get("obs_host") or obs.DEFAULT_HOST,
        "obs_port": int(stored.get("obs_port") or obs.DEFAULT_PORT),
        "obs_password": stored.get("obs_password") or "",
        # websocket-client missing -> the UI explains instead of offering a
        # Record button that could only fail (same idea as youtube_ready)
        "obs_available": obs.libraries_available(),
    }


def _hide_my_rank(conn):
    return db.get_settings(conn).get("hide_my_rank") == "1"


# Own-rank data is nulled at the API boundary when "Hide my rank / LP" is on,
# so every view — including future ones — hides it without its own logic.
# Snapshots keep being recorded; turning the setting off restores everything.
_MY_RANK_KEYS = {"solo_tier", "solo_division", "solo_lp", "start_ranks", "end_ranks"}


def _scrub_my_ranks(value):
    if isinstance(value, dict):
        return {k: (None if k in _MY_RANK_KEYS else _scrub_my_ranks(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_my_ranks(v) for v in value]
    return value


_MY_RANK_KEY_BYTES = [f'"{k}"'.encode() for k in _MY_RANK_KEYS]


@app.middleware("http")
async def redact_my_rank(request: Request, call_next):
    response = await call_next(request)
    if (not request.url.path.startswith("/api")
            or "application/json" not in response.headers.get("content-type", "")):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    headers = {k: v for k, v in response.headers.items() if k != "content-length"}
    # cheap sniff: only payloads that mention a rank key need the settings
    # lookup (keeps e.g. the 2 s crawl-status poll off the db)
    if any(key in body for key in _MY_RANK_KEY_BYTES):
        conn = get_conn()
        try:
            hidden = _hide_my_rank(conn)
        finally:
            conn.close()
        if hidden:
            return JSONResponse(_scrub_my_ranks(json.loads(body)),
                                status_code=response.status_code, headers=headers)
    return Response(content=body, status_code=response.status_code, headers=headers)


@app.get("/api/settings")
def api_get_settings():
    conn = get_conn()
    try:
        settings = config.resolve_settings(conn)
        settings["platforms"] = sorted(PLATFORM_ROUTING)
        settings.update(_extra_settings(conn))
        return settings
    finally:
        conn.close()


@app.put("/api/settings")
def api_put_settings(body: dict):
    api_key = (body.get("riot_api_key") or "").strip()
    accounts = body.get("accounts") or []
    platform = (body.get("platform") or "euw1").strip().lower()
    if not api_key:
        raise HTTPException(400, "Riot API key is required")
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(400, "add at least one account")
    cleaned = []
    for account in accounts:
        account = str(account).strip()
        name, _, tag = account.partition("#")
        if not name or not tag:
            raise HTTPException(400, f"account {account!r} must be Name#TAG")
        cleaned.append(f"{name.strip()}#{tag.strip()}")
    if platform not in PLATFORM_ROUTING:
        raise HTTPException(400, f"unknown platform {platform!r}")
    hidden_views = body.get("hidden_views", [])
    if not isinstance(hidden_views, list) or not set(hidden_views) <= HIDEABLE_VIEWS:
        raise HTTPException(400, f"hidden_views must be a subset of {sorted(HIDEABLE_VIEWS)}")
    hours = body.get("auto_crawl_hours", DEFAULT_AUTO_CRAWL_HOURS)
    if not isinstance(hours, int) or isinstance(hours, bool) or hours < 0:
        raise HTTPException(400, "auto_crawl_hours must be a non-negative whole number")
    hide_my_rank = body.get("hide_my_rank", False)
    if not isinstance(hide_my_rank, bool):
        raise HTTPException(400, "hide_my_rank must be a boolean")
    block_size = body.get("block_size", db.BLOCK_SIZE)
    if not isinstance(block_size, int) or isinstance(block_size, bool) or block_size < 1:
        raise HTTPException(400, "block_size must be a whole number >= 1")
    gap_hours = body.get("block_gap_hours", db.BLOCK_GAP_HOURS)
    if (isinstance(gap_hours, bool) or not isinstance(gap_hours, (int, float))
            or not 0 <= gap_hours <= db.MAX_BLOCK_GAP_HOURS):
        raise HTTPException(400, f"block_gap_hours must be 0..{db.MAX_BLOCK_GAP_HOURS:g}")
    gap_confirm = body.get("block_gap_confirm", True)
    if not isinstance(gap_confirm, bool):
        raise HTTPException(400, "block_gap_confirm must be a boolean")
    series_enabled = body.get("block_series_enabled", True)
    if not isinstance(series_enabled, bool):
        raise HTTPException(400, "block_series_enabled must be a boolean")
    ui_opacity = body.get("ui_opacity", 100)
    if (not isinstance(ui_opacity, int) or isinstance(ui_opacity, bool)
            or not 20 <= ui_opacity <= 100):
        raise HTTPException(400, "ui_opacity must be a whole number 20..100")
    accent_color = body.get("accent_color")
    if accent_color is not None and (not isinstance(accent_color, str)
                                      or not HEX_COLOR_RE.match(accent_color)):
        raise HTTPException(400, "accent_color must be a #rrggbb hex string or null")
    date_format = body.get("date_format", "iso")
    if date_format not in ("iso", "us", "eu"):
        raise HTTPException(400, "date_format must be one of: iso, us, eu")
    runes_mode = body.get("runes_mode", "matchup")
    if runes_mode not in ("matchup", "general"):
        raise HTTPException(400, "runes_mode must be one of: matchup, general")
    enable_comparison = body.get("enable_player_comparison", False)
    if not isinstance(enable_comparison, bool):
        raise HTTPException(400, "enable_player_comparison must be a boolean")
    valid_roles = ("", "TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
    main_role = body.get("main_role", "") or ""
    secondary_role = body.get("secondary_role", "") or ""
    if main_role not in valid_roles or secondary_role not in valid_roles:
        raise HTTPException(400, "role must be TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY or empty")
    # Paths are stored as typed — they point at files this machine owns, and
    # both are optional (blank = auto-detect Ascent / no YouTube configured).
    ascent_db_path = (body.get("ascent_db_path") or "").strip()
    youtube_client_secrets = (body.get("youtube_client_secrets") or "").strip()
    for label, value in (("ascent_db_path", ascent_db_path),
                         ("youtube_client_secrets", youtube_client_secrets)):
        if value and not Path(value).exists():
            raise HTTPException(400, f"{label}: no file at {value}")
    youtube_privacy = body.get("youtube_privacy") or youtube.DEFAULT_PRIVACY
    if youtube_privacy not in youtube.PRIVACY_VALUES:
        raise HTTPException(
            400, f"youtube_privacy must be one of: {', '.join(youtube.PRIVACY_VALUES)}")
    # OBS connection: host/port/password of obs-websocket. Blank host/port fall
    # back to OBS's own defaults rather than erroring — the common case is that
    # the user never touched them.
    obs_host = (body.get("obs_host") or "").strip() or obs.DEFAULT_HOST
    obs_port = body.get("obs_port", obs.DEFAULT_PORT)
    if isinstance(obs_port, str):
        obs_port = int(obs_port) if obs_port.strip().isdigit() else obs_port
    if (not isinstance(obs_port, int) or isinstance(obs_port, bool)
            or not 1 <= obs_port <= 65535):
        raise HTTPException(400, "obs_port must be a whole number 1..65535")
    obs_password = body.get("obs_password") or ""
    if not isinstance(obs_password, str):
        raise HTTPException(400, "obs_password must be a string")
    obs_enabled = body.get("obs_enabled", True)
    if not isinstance(obs_enabled, bool):
        raise HTTPException(400, "obs_enabled must be a boolean")
    conn = get_conn()
    try:
        db.set_settings(conn, {
            "riot_api_key": api_key,
            "accounts": json.dumps(cleaned),
            "platform": platform,
            "hidden_views": json.dumps(hidden_views),
            "auto_crawl_hours": str(hours),
            "hide_my_rank": "1" if hide_my_rank else "0",
            "block_size": str(block_size),
            "block_gap_hours": str(gap_hours),
            "block_gap_confirm": "1" if gap_confirm else "0",
            "block_series_enabled": "1" if series_enabled else "0",
            "ui_opacity": str(ui_opacity),
            "accent_color": accent_color or "",
            "date_format": date_format,
            "runes_mode": runes_mode,
            "enable_player_comparison": "1" if enable_comparison else "0",
            "main_role": main_role,
            "secondary_role": secondary_role,
            "ascent_db_path": ascent_db_path,
            "youtube_client_secrets": youtube_client_secrets,
            "youtube_privacy": youtube_privacy,
            "obs_enabled": "1" if obs_enabled else "0",
            "obs_host": obs_host,
            "obs_port": str(obs_port),
            "obs_password": obs_password,
        })
        settings = config.resolve_settings(conn)
        settings["platforms"] = sorted(PLATFORM_ROUTING)
        settings.update(_extra_settings(conn))
        return settings
    finally:
        conn.close()


@app.delete("/api/accounts")
def api_delete_account(body: dict):
    """Delete a tracked account: remove it from the accounts setting AND purge
    its crawled data from the db (`db.delete_account_data`). Riot can always
    re-supply the crawled data, so this is the one crawled-data delete allowed;
    user-authored content is untouched. Case-insensitive match on Name#TAG."""
    account = (body or {}).get("account")
    account = str(account or "").strip()
    name, sep, tag = account.partition("#")
    if not sep or not name.strip() or not tag.strip():
        raise HTTPException(400, "account must be Name#TAG")
    conn = get_conn()
    try:
        stored = db.get_settings(conn)
        accounts = json.loads(stored.get("accounts") or "[]")
        accounts = [a for a in accounts if a.strip().lower() != account.lower()]
        db.set_settings(conn, {"accounts": json.dumps(accounts)})
        rows = conn.execute(
            "SELECT puuid FROM players "
            "WHERE lower(game_name)=lower(?) AND lower(tag_line)=lower(?)",
            (name.strip(), tag.strip())).fetchall()
        for r in rows:
            db.delete_account_data(conn, r["puuid"])
        return {"account": account, "players_deleted": len(rows), "accounts": accounts}
    finally:
        conn.close()


@app.post("/api/settings/background")
async def api_set_background(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_BACKGROUND_EXTENSIONS:
        raise HTTPException(
            400, f"unsupported file type {ext or '(none)'} — "
                 f"allowed: {', '.join(sorted(ALLOWED_BACKGROUND_EXTENSIONS))}")
    data = await file.read(MAX_BACKGROUND_BYTES + 1)
    if len(data) > MAX_BACKGROUND_BYTES:
        raise HTTPException(413, "image exceeds the 15 MB limit")
    conn = get_conn()
    try:
        old = db.get_settings(conn).get("background_image_file")
        stored_name = f"{uuid.uuid4().hex}{ext}"
        (get_background_dir() / stored_name).write_bytes(data)
        db.set_settings(conn, {"background_image_file": stored_name})
    finally:
        conn.close()
    if old:
        (get_background_dir() / old).unlink(missing_ok=True)
    return {"background_image": True}


@app.get("/api/settings/background/file")
def api_get_background_file():
    conn = get_conn()
    try:
        name = db.get_settings(conn).get("background_image_file")
    finally:
        conn.close()
    if not name:
        raise HTTPException(404, "no background image set")
    path = get_background_dir() / name
    if not path.exists():
        raise HTTPException(404, "background image missing on disk")
    return FileResponse(path)


@app.delete("/api/settings/background")
def api_delete_background():
    conn = get_conn()
    try:
        old = db.get_settings(conn).get("background_image_file")
        if old:
            with conn:
                conn.execute("DELETE FROM settings WHERE key='background_image_file'")
    finally:
        conn.close()
    if old:
        (get_background_dir() / old).unlink(missing_ok=True)
    return {"deleted": True}


@app.get("/api/players")
def players():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.puuid, p.game_name, p.tag_line, p.solo_tier, p.solo_division,
                      p.solo_lp, p.rank_fetched_at_ms,
                      (SELECT COUNT(*) FROM participants pa WHERE pa.puuid = p.puuid)
                          AS total_matches
               FROM players p WHERE p.is_tracked = 1 ORDER BY p.game_name"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/stats/matchups")
def api_matchups(request: Request):
    conn = get_conn()
    try:
        return stats.matchups(conn, **stat_filters(request, conn))
    finally:
        conn.close()


@app.get("/api/stats/matchups_by_rank")
def api_matchups_by_rank(request: Request):
    conn = get_conn()
    try:
        return stats.matchups_by_rank(conn, **stat_filters(request, conn))
    finally:
        conn.close()


@app.get("/api/stats/summary")
def api_summary(request: Request):
    conn = get_conn()
    try:
        return stats.summary(conn, **stat_filters(request, conn))
    finally:
        conn.close()


@app.get("/api/filters")
def api_filters(request: Request):
    conn = get_conn()
    try:
        puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
        return stats.filter_options(conn, puuids)
    finally:
        conn.close()


@app.get("/api/sessions")
def api_sessions():
    conn = get_conn()
    try:
        sessions = []
        for row in db.list_sessions(conn):
            record = dict(row)
            raw = record.pop("start_ranks", None)
            record["start_ranks"] = json.loads(raw) if raw else None
            sessions.append(record)
        return sessions
    finally:
        conn.close()


@app.post("/api/sessions")
def api_add_session(body: dict):
    date_str = (body or {}).get("date", "")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    conn = get_conn()
    try:
        coach = (body.get("coach") or "").strip()
        category = (body.get("category") or "").strip()
        session_id = db.add_session(conn, date_str,
                                    title=(body.get("title") or "").strip(),
                                    notes=body.get("notes") or "",
                                    coach=coach,
                                    category=category,
                                    link=_clean_session_link(body.get("link")))
        db.add_coach(conn, coach)  # remember it for the next session's suggestions
        db.add_session_category(conn, category)  # same deal for a new category
        return {"id": session_id}
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a session on {date_str} already exists")
    finally:
        conn.close()


@app.patch("/api/sessions/{session_id}")
def api_update_session(session_id: int, body: dict):
    """Partial update — `coach` is editable so older sessions can be backfilled
    with who ran them."""
    title = body.get("title")
    notes = body.get("notes")
    coach = body.get("coach")
    category = body.get("category")
    link = body.get("link")
    if title is None and notes is None and coach is None and link is None \
            and category is None:
        raise HTTPException(400, "provide title, notes, coach, category and/or link")
    if coach is not None:
        coach = str(coach).strip()
    if category is not None:
        category = str(category).strip()
    if link is not None:
        link = _clean_session_link(link)
    conn = get_conn()
    try:
        if not db.update_session(conn, session_id, title=title, notes=notes,
                                 coach=coach, link=link, category=category):
            raise HTTPException(404, "no such session")
        if coach:
            db.add_coach(conn, coach)
        if category:
            db.add_session_category(conn, category)
        return {"updated": True}
    finally:
        conn.close()


def _clean_session_link(value):
    """Where the coach posted the VOD (weteachleague, YouTube, a Drive link…).
    Only the scheme is constrained — an http(s) URL is the one thing that can
    be opened safely from a link; javascript:/data: are refused outright."""
    link = str(value or "").strip()
    if not link:
        return ""
    if not re.match(r"^https?://", link, re.I):
        raise HTTPException(400, "link must start with http:// or https://")
    return link


@app.get("/api/coaches")
def api_coaches():
    """Names suggested on the session Coach field."""
    conn = get_conn()
    try:
        return {"coaches": db.list_coaches(conn)}
    finally:
        conn.close()


@app.delete("/api/coaches/{name}")
def api_remove_coach(name: str):
    """Stop suggesting this coach. Sessions that name them keep it — this list
    is only an autocomplete, never the record of who coached what."""
    conn = get_conn()
    try:
        if not db.remove_coach(conn, name):
            raise HTTPException(404, "no such coach")
        return {"removed": True}
    finally:
        conn.close()


@app.get("/api/session-categories")
def api_session_categories():
    """The Category pick-list: the seeded defaults (Theory / VOD review /
    Live coaching) plus anything the user has typed into the field."""
    conn = get_conn()
    try:
        return {"categories": db.list_session_categories(conn)}
    finally:
        conn.close()


@app.delete("/api/session-categories/{name}")
def api_remove_session_category(name: str):
    """Stop suggesting this category. Sessions that recorded it keep it — the
    list is only a pick-list, never the record of what a session was."""
    conn = get_conn()
    try:
        if not db.remove_session_category(conn, name):
            raise HTTPException(404, "no such category")
        return {"removed": name}
    finally:
        conn.close()


@app.get("/api/sessions/export.md")
def api_export_sessions():
    conn = get_conn()
    try:
        rows = db.list_sessions(conn)
    finally:
        conn.close()
    parts = ["# Coaching sessions\n"]
    for row in reversed(rows):  # newest first
        title = row["title"] or "Session"
        parts.append(f"\n## {row['session_date']} — {title}\n")
        if row["coach"]:
            parts.append(f"\n*Coach: {row['coach']}*\n")
        if row["category"]:
            parts.append(f"\n*Category: {row['category']}*\n")
        if row["link"]:
            parts.append(f"\n[Session recording]({row['link']})\n")
        if row["notes"]:
            parts.append(f"\n{row['notes']}\n")
    return Response(
        content="".join(parts),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="coaching-sessions.md"'},
    )


@app.get("/api/export-all")
def api_export_all():
    """A full backup of everything you've authored — coaching sessions,
    block learnings/notes, matchup guides, champion notes, item builds,
    Research entries — plus every uploaded clip and screenshot file, as one
    .zip. Deliberately excludes settings (API key, accounts) and raw
    crawled match/rank data, which Riot's API can always re-supply."""
    conn = get_conn()
    try:
        sessions = [dict(r) for r in conn.execute(
            """SELECT session_date, title, coach, link, category, notes,
                      start_ranks, created_at_ms
               FROM coaching_sessions ORDER BY session_date""")]
        blocks_rows = [dict(r) for r in conn.execute(
            """SELECT id, title, learnings, pool_snapshot, start_ranks, end_ranks,
                      closed_at_ms, created_at_ms FROM blocks ORDER BY id""")]
        block_games_rows = [dict(r) for r in conn.execute(
            """SELECT id, block_id, match_id, puuid, notes, added_at_ms
               FROM block_games ORDER BY id""")]
        matchup_notes_rows = [dict(r) for r in conn.execute(
            """SELECT my_champion, opp_champion, notes, runes, patch_version,
                      skill_order, updated_at_ms
               FROM matchup_notes
               WHERE notes != '' OR runes != '' OR patch_version != '' OR skill_order != ''
               ORDER BY my_champion, opp_champion""")]
        champion_notes_rows = [dict(r) for r in conn.execute(
            "SELECT champion, notes, updated_at_ms FROM champion_notes ORDER BY champion")]
        item_build_rows = [dict(r) for r in conn.execute(
            """SELECT champion, core, situational, updated_at_ms
               FROM champion_item_builds ORDER BY champion""")]
        research_rows = [dict(r) for r in conn.execute(
            """SELECT id, player_name, champion, opp_champion, title, notes,
                      created_at_ms, updated_at_ms FROM research_entries ORDER BY id""")]
        screenshot_rows = [dict(r) for r in conn.execute(
            """SELECT id, entry_id, caption, file_name, created_at_ms
               FROM research_screenshots ORDER BY id""")]
        clip_rows = [dict(r) for r in conn.execute(
            """SELECT id, owner_type, owner_id, label, kind, file_name, url, created_at_ms
               FROM clips ORDER BY id""")]
        tier_list_rows = [dict(r) for r in conn.execute(
            "SELECT id, title, data, champion, created_at_ms, updated_at_ms FROM tier_lists ORDER BY id")]
        # OBS session recordings: the video files themselves are NOT bundled
        # (they live wherever OBS wrote them and can be gigabytes), but the
        # rows and their bookmarks are authored content worth keeping
        session_recording_rows = [dict(r) for r in conn.execute(
            """SELECT id, session_id, label, video_path, source, started_at_ms,
                      stopped_at_ms, created_at_ms
               FROM session_recordings ORDER BY id""")]
        session_mark_rows = [dict(r) for r in conn.execute(
            """SELECT id, recording_id, offset_ms, label, created_at_ms
               FROM session_marks ORDER BY id""")]
    finally:
        conn.close()

    for row in tier_list_rows:
        row["data"] = json.loads(row["data"]) if row["data"] else {"tiers": []}
    for row in matchup_notes_rows:
        row["runes"] = json.loads(row["runes"]) if row["runes"] else []
        row["skill_order"] = json.loads(row["skill_order"]) if row["skill_order"] else []
    for row in item_build_rows:
        row["core"] = json.loads(row["core"])
        row["situational"] = json.loads(row["situational"])
    for row in blocks_rows:
        for key in ("pool_snapshot", "start_ranks", "end_ranks"):
            row[key] = json.loads(row[key]) if row[key] else None
    for row in sessions:
        row["start_ranks"] = json.loads(row["start_ranks"]) if row["start_ranks"] else None

    payload = {
        "app": "coach-potato", "kind": "full-export", "version": 1,
        "exported_at_ms": int(time.time() * 1000),
        "sessions": sessions,
        "blocks": blocks_rows,
        "block_games": block_games_rows,
        "matchup_notes": matchup_notes_rows,
        "champion_notes": champion_notes_rows,
        "item_builds": item_build_rows,
        "research_entries": research_rows,
        "research_screenshots": screenshot_rows,
        "clips": clip_rows,
        "tier_lists": tier_list_rows,
        "session_recordings": session_recording_rows,
        "session_marks": session_mark_rows,
    }

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", json.dumps(payload, indent=2))
        clips_dir = get_clips_dir()
        for row in clip_rows:
            if row["kind"] == "upload" and row["file_name"]:
                path = clips_dir / row["file_name"]
                if path.exists():
                    zf.write(path, f"clips/{row['file_name']}")
        screenshots_dir = get_research_screenshots_dir()
        for row in screenshot_rows:
            path = screenshots_dir / row["file_name"]
            if path.exists():
                zf.write(path, f"screenshots/{row['file_name']}")

    filename = f"coach-potato-export-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.zip"
    return FileResponse(
        tmp.name, media_type="application/zip", filename=filename,
        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))


FULL_EXPORT_KIND = "full-export"
MAX_IMPORT_ZIP_BYTES = 500 * 1024 * 1024  # 500 MB — a backup can hold many clips


def _read_import_zip(data: bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        payload = json.loads(zf.read("data.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        raise HTTPException(400, "not a valid export .zip file")
    if payload.get("kind") != FULL_EXPORT_KIND:
        raise HTTPException(400, "not a coach-potato full-export file")
    return zf, payload


def _import_conflicts(conn, payload):
    """Scoped to restoring onto a fresh/empty setup: any row that would
    collide with something already present blocks the whole import (nothing
    is written) rather than silently overwriting or merging."""
    conflicts = []
    for row in payload.get("sessions") or []:
        if conn.execute("SELECT 1 FROM coaching_sessions WHERE session_date=?",
                         (row["session_date"],)).fetchone():
            conflicts.append(f"session on {row['session_date']}")
    for row in payload.get("blocks") or []:
        if conn.execute("SELECT 1 FROM blocks WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"block #{row['id']}")
    for row in payload.get("block_games") or []:
        if conn.execute("SELECT 1 FROM block_games WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"block game #{row['id']}")
    for row in payload.get("matchup_notes") or []:
        if conn.execute(
                "SELECT 1 FROM matchup_notes WHERE my_champion=? AND opp_champion=?",
                (row["my_champion"], row["opp_champion"])).fetchone():
            conflicts.append(f"matchup guide {row['my_champion']} vs {row['opp_champion']}")
    for row in payload.get("champion_notes") or []:
        if conn.execute("SELECT 1 FROM champion_notes WHERE champion=?",
                         (row["champion"],)).fetchone():
            conflicts.append(f"champion notes for {row['champion']}")
    for row in payload.get("item_builds") or []:
        if conn.execute("SELECT 1 FROM champion_item_builds WHERE champion=?",
                         (row["champion"],)).fetchone():
            conflicts.append(f"item build for {row['champion']}")
    for row in payload.get("research_entries") or []:
        if conn.execute("SELECT 1 FROM research_entries WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"research entry #{row['id']}")
    for row in payload.get("session_recordings") or []:
        if conn.execute("SELECT 1 FROM session_recordings WHERE id=?",
                         (row["id"],)).fetchone():
            conflicts.append(f"session recording #{row['id']}")
    for row in payload.get("research_screenshots") or []:
        if conn.execute("SELECT 1 FROM research_screenshots WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"research screenshot #{row['id']}")
    for row in payload.get("clips") or []:
        if conn.execute("SELECT 1 FROM clips WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"clip #{row['id']}")
    for row in payload.get("tier_lists") or []:
        if conn.execute("SELECT 1 FROM tier_lists WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"tier list #{row['id']}")
    return conflicts


def _import_counts(payload):
    return {
        "sessions": len(payload.get("sessions") or []),
        "blocks": len(payload.get("blocks") or []),
        "matchup_notes": len(payload.get("matchup_notes") or []),
        "champion_notes": len(payload.get("champion_notes") or []),
        "item_builds": len(payload.get("item_builds") or []),
        "research_entries": len(payload.get("research_entries") or []),
        "clips": len(payload.get("clips") or []),
        "tier_lists": len(payload.get("tier_lists") or []),
        "session_recordings": len(payload.get("session_recordings") or []),
    }


@app.post("/api/import-all/preview")
async def api_import_all_preview(file: UploadFile = File(...)):
    data = await file.read(MAX_IMPORT_ZIP_BYTES + 1)
    if len(data) > MAX_IMPORT_ZIP_BYTES:
        raise HTTPException(413, "export file exceeds the 500 MB limit")
    _, payload = _read_import_zip(data)
    conn = get_conn()
    try:
        conflicts = _import_conflicts(conn, payload)
    finally:
        conn.close()
    return {"counts": _import_counts(payload), "conflicts": conflicts}


@app.post("/api/import-all")
async def api_import_all(file: UploadFile = File(...)):
    data = await file.read(MAX_IMPORT_ZIP_BYTES + 1)
    if len(data) > MAX_IMPORT_ZIP_BYTES:
        raise HTTPException(413, "export file exceeds the 500 MB limit")
    zf, payload = _read_import_zip(data)
    conn = get_conn()
    try:
        conflicts = _import_conflicts(conn, payload)
        if conflicts:
            shown = ", ".join(conflicts[:10]) + ("…" if len(conflicts) > 10 else "")
            raise HTTPException(409, f"would overwrite existing data: {shown}")
        with conn:
            for row in payload.get("sessions") or []:
                conn.execute(
                    """INSERT INTO coaching_sessions
                       (session_date, title, coach, link, category, notes,
                        start_ranks, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["session_date"], row.get("title", ""), row.get("coach", ""),
                     row.get("link", ""), row.get("category", ""), row.get("notes", ""),
                     json.dumps(row["start_ranks"]) if row.get("start_ranks") else None,
                     row.get("created_at_ms")))
            for row in payload.get("blocks") or []:
                conn.execute(
                    """INSERT INTO blocks
                       (id, title, learnings, pool_snapshot, start_ranks, end_ranks,
                        closed_at_ms, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["id"], row.get("title", ""), row.get("learnings", ""),
                     json.dumps(row["pool_snapshot"]) if row.get("pool_snapshot") else None,
                     json.dumps(row["start_ranks"]) if row.get("start_ranks") else None,
                     json.dumps(row["end_ranks"]) if row.get("end_ranks") else None,
                     row.get("closed_at_ms"), row.get("created_at_ms")))
            for row in payload.get("block_games") or []:
                conn.execute(
                    """INSERT INTO block_games (id, block_id, match_id, puuid, notes, added_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (row["id"], row["block_id"], row["match_id"], row["puuid"],
                     row.get("notes", ""), row.get("added_at_ms")))
            for row in payload.get("matchup_notes") or []:
                skill_order = row.get("skill_order") or []
                conn.execute(
                    """INSERT INTO matchup_notes
                       (my_champion, opp_champion, notes, runes, patch_version,
                        skill_order, updated_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (row["my_champion"], row["opp_champion"], row.get("notes", ""),
                     json.dumps(row.get("runes") or []), row.get("patch_version", ""),
                     json.dumps(skill_order) if any(skill_order) else "",
                     row.get("updated_at_ms")))
            for row in payload.get("champion_notes") or []:
                conn.execute(
                    "INSERT INTO champion_notes (champion, notes, updated_at_ms) VALUES (?, ?, ?)",
                    (row["champion"], row.get("notes", ""), row.get("updated_at_ms")))
            for row in payload.get("item_builds") or []:
                conn.execute(
                    """INSERT INTO champion_item_builds (champion, core, situational, updated_at_ms)
                       VALUES (?, ?, ?, ?)""",
                    (row["champion"], json.dumps(row.get("core") or []),
                     json.dumps(row.get("situational") or []), row.get("updated_at_ms")))
            for row in payload.get("research_entries") or []:
                conn.execute(
                    """INSERT INTO research_entries
                       (id, player_name, champion, opp_champion, title, notes,
                        created_at_ms, updated_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["id"], row.get("player_name", ""), row.get("champion", ""),
                     row.get("opp_champion", ""), row.get("title", ""), row.get("notes", ""),
                     row.get("created_at_ms"), row.get("updated_at_ms")))
            for row in payload.get("research_screenshots") or []:
                conn.execute(
                    """INSERT INTO research_screenshots
                       (id, entry_id, caption, file_name, created_at_ms)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["id"], row["entry_id"], row.get("caption", ""), row["file_name"],
                     row.get("created_at_ms")))
                member = f"screenshots/{row['file_name']}"
                if member in zf.namelist():
                    (get_research_screenshots_dir() / row["file_name"]).write_bytes(zf.read(member))
            for row in payload.get("session_recordings") or []:
                conn.execute(
                    """INSERT INTO session_recordings
                       (id, session_id, label, video_path, source, started_at_ms,
                        stopped_at_ms, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["id"], row["session_id"], row.get("label", ""),
                     row.get("video_path", ""), row.get("source", "obs"),
                     row.get("started_at_ms"), row.get("stopped_at_ms"),
                     row.get("created_at_ms")))
            for row in payload.get("session_marks") or []:
                conn.execute(
                    """INSERT INTO session_marks
                       (id, recording_id, offset_ms, label, created_at_ms)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["id"], row["recording_id"], row.get("offset_ms", 0),
                     row.get("label", ""), row.get("created_at_ms")))
            for row in payload.get("clips") or []:
                conn.execute(
                    """INSERT INTO clips
                       (id, owner_type, owner_id, label, kind, file_name, url, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["id"], row["owner_type"], row["owner_id"], row.get("label", ""),
                     row["kind"], row.get("file_name"), row.get("url"), row.get("created_at_ms")))
                if row["kind"] == "upload" and row.get("file_name"):
                    member = f"clips/{row['file_name']}"
                    if member in zf.namelist():
                        (get_clips_dir() / row["file_name"]).write_bytes(zf.read(member))
            for row in payload.get("tier_lists") or []:
                data = row.get("data")
                conn.execute(
                    """INSERT INTO tier_lists (id, title, data, champion, created_at_ms, updated_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (row["id"], row.get("title", ""),
                     json.dumps(data) if isinstance(data, (dict, list)) else (data or "{}"),
                     row.get("champion", ""),
                     row.get("created_at_ms"), row.get("updated_at_ms")))
        return {"imported": _import_counts(payload)}
    finally:
        conn.close()


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: int):
    conn = get_conn()
    try:
        freed = db.delete_clips_for_owner(conn, "session", session_id)
        # recordings are forgotten, never deleted: the row goes, the video file
        # on disk stays exactly where OBS wrote it
        db.delete_session_recordings_for_session(conn, session_id)
        if not db.delete_session(conn, session_id):
            raise HTTPException(404, "no such session")
        _unlink_clip_files(freed)
        return {"deleted": True}
    finally:
        conn.close()


@app.get("/api/stats/progress")
def api_progress(request: Request):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
        sessions = [dict(r) for r in db.list_sessions(conn)]  # sessions are global
        return stats.progress_segments(
            conn, puuids, sessions,
            champion=params.get("champion") or None, queues=queues,
            side=params.get("side") or None,
            roles=request.query_params.getlist("role") or None)
    finally:
        conn.close()


@app.get("/api/stats/games")
def api_games(request: Request, from_ms: int | None = None, to_ms: int | None = None):
    params = dict(request.query_params)
    if from_ms is None and to_ms is None:
        from_ms, to_ms = parse_time_range(params)  # range=30d / from= / to= also work
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        players = conn.execute(
            "SELECT puuid, game_name FROM players WHERE is_tracked=1").fetchall()
        names = {r["puuid"]: r["game_name"] for r in players}
        puuids = request.query_params.getlist("puuid") or list(names)
        games = stats.games_in_range(
            conn, puuids, from_ms=from_ms, to_ms=to_ms,
            champion=params.get("champion") or None, queues=queues,
            opp_champion=params.get("opp_champion") or None,
            rank_tier=params.get("rank_tier") or None,
            side=params.get("side") or None,
            roles=request.query_params.getlist("role") or None)
        for game in games:
            game["account"] = names.get(game["my_puuid"], "?")
        return games
    finally:
        conn.close()


def _tracked_puuids(conn):
    return [r["puuid"] for r in
            conn.execute("SELECT puuid FROM players WHERE is_tracked=1")]


@app.get("/api/metrics/meta")
def api_metrics_meta():
    """The metric registry (labels/groups/decimals/default_hidden/…) on its
    own, so the frontend can build per-view metric column pickers before any
    stats panel has loaded."""
    return {"meta": METRICS}


@app.get("/api/stats/metrics")
def api_metrics(request: Request, from_ms: int | None = None, to_ms: int | None = None):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
        result = stats.segment_metrics(
            conn, puuids, from_ms=from_ms, to_ms=to_ms,
            champion=params.get("champion") or None, queues=queues,
            side=params.get("side") or None,
            roles=request.query_params.getlist("role") or None)
        result["meta"] = METRICS
        return result
    finally:
        conn.close()


@app.get("/api/stats/games/metrics")
def api_single_game_metrics(match_id: str, puuid: str):
    conn = get_conn()
    try:
        metrics = stats.single_game_metrics(conn, match_id, puuid)
        if metrics is None:
            raise HTTPException(404, "no metrics recorded for that game")
        return {"metrics": metrics, "meta": METRICS}
    finally:
        conn.close()


@app.get("/api/stats/rune-analysis")
def api_rune_analysis(champion: str, opp_champion: str = ""):
    """Win rate by keystone / secondary tree from the runes you actually
    played on `champion` (optionally vs `opp_champion`), across tracked
    accounts. Powers the Matchup guide's rune analysis."""
    _validate_champion(champion)
    if opp_champion:
        _validate_champion(opp_champion)
    conn = get_conn()
    try:
        puuids = _tracked_puuids(conn)
        if not puuids:
            return {"keystones": [], "secondaries": []}
        return stats.rune_analysis(conn, puuids, champion, opp_champion or None)
    finally:
        conn.close()


@app.get("/api/stats/game-curve")
def api_game_curve(match_id: str, puuid: str, opp_puuid: str | None = None):
    """Full-game per-minute gold/CS/XP/level series for one game (+ the lane
    opponent's, when opp_puuid is given) — the game-curve chart. 404 when
    nothing was recorded (crawled before the feature existed, or the
    timeline was unavailable)."""
    conn = get_conn()
    try:
        curve = stats.game_curve(conn, match_id, puuid, opp_puuid)
        if curve is None:
            raise HTTPException(404, "no frame series recorded for that game")
        return curve
    finally:
        conn.close()


@app.get("/api/stats/trends")
def api_trends(request: Request, bucket: str = "month"):
    params = dict(request.query_params)
    from_ms, to_ms = parse_time_range(params)  # range=30d / from= / to= also work
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        try:
            puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
            buckets = stats.trend_buckets(
                conn, puuids, bucket=bucket, from_ms=from_ms, to_ms=to_ms,
                champion=params.get("champion") or None, queues=queues,
                side=params.get("side") or None,
            roles=request.query_params.getlist("role") or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"buckets": buckets, "meta": METRICS}
    finally:
        conn.close()


@app.get("/api/stats/map-events")
def api_map_events(request: Request, from_ms: int | None = None, to_ms: int | None = None):
    """Death-location map events for the death heatmap (Trends view).
    Filtered like the other Trends-style queries: champion/role/period.
    Deaths-only — see stats.map_events / CLAUDE.md for why there's no ward
    counterpart yet."""
    params = dict(request.query_params)
    if from_ms is None and to_ms is None:
        from_ms, to_ms = parse_time_range(params)  # range=30d / from= / to= also work
    conn = get_conn()
    try:
        puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
        events = stats.map_events(
            conn, puuids, from_ms=from_ms, to_ms=to_ms,
            champion=params.get("champion") or None,
            roles=request.query_params.getlist("role") or None)
        return {"events": events}
    finally:
        conn.close()


def _validate_champion(champion: str):
    # match-v5 names differ in case from DDragon ids (FiddleSticks vs
    # Fiddlesticks) — validate case-insensitively, store the name as given
    # because reads key by the match-v5 spelling
    if CHAMPION_IDS and champion.lower() not in {c.lower() for c in CHAMPION_IDS}:
        raise HTTPException(400, f"not a champion: {champion}")


@app.get("/api/matchups/notes")
def api_matchup_notes(my_champion: str):
    if not my_champion:
        raise HTTPException(400, "provide my_champion")
    conn = get_conn()
    try:
        return db.get_matchup_notes(conn, my_champion)
    finally:
        conn.close()


PATCH_VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}(\.\d{1,3})?$")


def _validate_patch(patch_version: str):
    if patch_version and not PATCH_VERSION_RE.match(patch_version):
        raise HTTPException(400, "patch_version must look like 16.14 (or 16.14.1), or be empty")


R_POINT_LEVELS = (6, 11, 16)


def _validate_skill_order(cells):
    """skill_order: up to 18 entries of ''/Q/W/E/R, index = level-1. Enforces
    the in-game rules: one point per level (list shape), basics max 5 points
    with point k needing level 2k-1, R max 3 points at levels 6/11/16."""
    if not isinstance(cells, list) or len(cells) > 18:
        raise HTTPException(400, "skill_order must be a list of up to 18 levels")
    points = {"Q": [], "W": [], "E": [], "R": []}
    for i, cell in enumerate(cells):
        if cell in ("", None):
            continue
        if cell not in points:
            raise HTTPException(400, f"skill_order entries must be Q/W/E/R or blank: {cell!r}")
        points[cell].append(i + 1)
    for key, levels in points.items():
        max_points = 3 if key == "R" else 5
        if len(levels) > max_points:
            raise HTTPException(400, f"{key} can have at most {max_points} points")
        for i, level in enumerate(levels):
            needed = R_POINT_LEVELS[i] if key == "R" else 2 * (i + 1) - 1
            if level < needed:
                raise HTTPException(400, f"{key} point {i + 1} requires level {needed}")


def _validate_rune_page(page):
    if not isinstance(page, dict):
        raise HTTPException(400, "each rune page must be an object")
    for key in ("primary_tree", "secondary_tree"):
        value = page.get(key)
        if value and RUNE_TREE_NAMES and value not in RUNE_TREE_NAMES:
            raise HTTPException(400, f"not a rune tree: {value}")
    keystone = page.get("keystone")
    if keystone and RUNE_NAMES and keystone not in RUNE_NAMES:
        raise HTTPException(400, f"not a rune: {keystone}")
    # empty strings are unfilled slots (the picker sends positional arrays,
    # e.g. primary_runes ["Triumph", "", ""]) — a partial page is saveable
    for key in ("primary_runes", "secondary_runes"):
        for value in page.get(key) or []:
            if value and RUNE_NAMES and value not in RUNE_NAMES:
                raise HTTPException(400, f"not a rune: {value}")
    for value in page.get("shards") or []:
        if value and RUNE_SHARD_NAMES and value not in RUNE_SHARD_NAMES:
            raise HTTPException(400, f"not a stat shard: {value}")


@app.put("/api/matchups/notes/{my_champion}/{opp_champion}")
def api_put_matchup_note(my_champion: str, opp_champion: str, body: dict):
    """Partial update: only the fields present in the body are written —
    the cooldown popup saves skill_order without touching notes/runes and
    the guide editor saves notes/runes/patch without touching skill_order."""
    body = body or {}
    known = ("notes", "runes", "patch_version", "skill_order")
    if not any(k in body for k in known):
        raise HTTPException(400, f"provide at least one of: {', '.join(known)}")
    _validate_champion(my_champion)
    _validate_champion(opp_champion)
    fields = {}
    if "notes" in body:
        fields["notes"] = str(body.get("notes") or "")
    if "runes" in body:
        runes = body.get("runes") or []
        if not isinstance(runes, list):
            raise HTTPException(400, "runes must be a list of rune pages")
        for page in runes:
            _validate_rune_page(page)
        fields["runes"] = runes
    if "patch_version" in body:
        patch_version = str(body.get("patch_version") or "").strip()
        _validate_patch(patch_version)
        fields["patch_version"] = patch_version
    if "skill_order" in body:
        skill_order = body.get("skill_order") or []
        _validate_skill_order(skill_order)
        fields["skill_order"] = skill_order
    conn = get_conn()
    try:
        db.set_matchup_note(conn, my_champion, opp_champion, **fields)
        return {"saved": True}
    finally:
        conn.close()


MAX_REFLECTION_TAGS = 20
MAX_REFLECTION_TAG_LEN = 40


@app.get("/api/reflections")
def api_get_reflection(match_id: str, puuid: str):
    conn = get_conn()
    try:
        return db.get_reflection(conn, match_id, puuid)
    finally:
        conn.close()


def _validate_reflection_tags(tags):
    if not isinstance(tags, list) or len(tags) > MAX_REFLECTION_TAGS:
        raise HTTPException(400, f"tags must be a list of up to {MAX_REFLECTION_TAGS} strings")
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip() or len(tag) > MAX_REFLECTION_TAG_LEN:
            raise HTTPException(400, f"each tag must be a non-empty string up to "
                                      f"{MAX_REFLECTION_TAG_LEN} chars")


@app.put("/api/reflections/{match_id}/{puuid}")
def api_put_reflection(match_id: str, puuid: str, body: dict):
    """Partial update: only the fields present in the body are written — a
    tags-only edit (toggling a chip) never clobbers the note and vice versa,
    mirroring /api/matchups/notes/{my}/{opp}."""
    body = body or {}
    known = ("tags", "note")
    if not any(k in body for k in known):
        raise HTTPException(400, f"provide at least one of: {', '.join(known)}")
    fields = {}
    if "tags" in body:
        tags = body.get("tags") or []
        _validate_reflection_tags(tags)
        fields["tags"] = tags
    if "note" in body:
        fields["note"] = str(body.get("note") or "")
    conn = get_conn()
    try:
        db.set_reflection(conn, match_id, puuid, **fields)
        return {"saved": True}
    finally:
        conn.close()


@app.get("/api/champions/notes/{champion}")
def api_get_champion_note(champion: str):
    conn = get_conn()
    try:
        raw = db.get_champion_runes(conn, champion)
        return {"notes": db.get_champion_note(conn, champion),
                "runes": json.loads(raw) if raw else []}
    finally:
        conn.close()


@app.put("/api/champions/notes/{champion}")
def api_put_champion_note(champion: str, body: dict):
    """Partial update: writes `notes` and/or general `runes` (runes_mode=
    'general' stores one champion-level rune set here, shown by the item
    build). Passing only one leaves the other untouched."""
    body = body or {}
    if "notes" not in body and "runes" not in body:
        raise HTTPException(400, "provide notes and/or runes")
    _validate_champion(champion)
    conn = get_conn()
    try:
        if "notes" in body:
            db.set_champion_note(conn, champion, str(body.get("notes") or ""))
        if "runes" in body:
            runes = body.get("runes") or []
            if not isinstance(runes, list):
                raise HTTPException(400, "runes must be a list of rune pages")
            for page in runes:
                _validate_rune_page(page)
            db.set_champion_runes(conn, champion, json.dumps(runes) if runes else "")
        return {"saved": True}
    finally:
        conn.close()


MAX_CORE_ITEMS = 6
MAX_SITUATIONAL_SECTIONS = 12
MAX_ITEMS_PER_SECTION = 5


@app.get("/api/champions/item-build/{champion}")
def api_get_item_build(champion: str):
    conn = get_conn()
    try:
        return db.get_item_build(conn, champion)
    finally:
        conn.close()


def _validate_item_build(core, situational):
    if (not isinstance(core, list) or len(core) > MAX_CORE_ITEMS
            or not all(isinstance(i, str) and i.strip() for i in core)):
        raise HTTPException(400, f"core must be a list of up to {MAX_CORE_ITEMS} item names")
    if not isinstance(situational, list) or len(situational) > MAX_SITUATIONAL_SECTIONS:
        raise HTTPException(400, f"situational must be a list of up to {MAX_SITUATIONAL_SECTIONS} sections")
    cleaned_situational = []
    for section in situational:
        if not isinstance(section, dict):
            raise HTTPException(400, "each situational section must be an object")
        label = str(section.get("label") or "").strip()
        items = section.get("items") or []
        if not label:
            raise HTTPException(400, "each situational section needs a label")
        if (not isinstance(items, list) or len(items) > MAX_ITEMS_PER_SECTION
                or not all(isinstance(i, str) and i.strip() for i in items)):
            raise HTTPException(400, f"each situational section holds up to {MAX_ITEMS_PER_SECTION} items")
        cleaned_situational.append({"label": label, "items": [i.strip() for i in items]})
    return [i.strip() for i in core], cleaned_situational


@app.put("/api/champions/item-build/{champion}")
def api_put_item_build(champion: str, body: dict):
    body = body or {}
    _validate_champion(champion)
    core, situational = _validate_item_build(body.get("core") or [], body.get("situational") or [])
    conn = get_conn()
    try:
        db.set_item_build(conn, champion, core, situational)
        return {"saved": True}
    finally:
        conn.close()


EXPORT_KIND = "champ-guide-export"
EXPORT_VERSION = 1


@app.post("/api/matchups/notes/export")
def api_export_champ_guide(body: dict):
    body = body or {}
    my_champion = body.get("my_champion")
    if not my_champion:
        raise HTTPException(400, "provide my_champion")
    _validate_champion(my_champion)
    password = body.get("password") or None
    conn = get_conn()
    try:
        payload = {
            "general_notes": db.get_champion_note(conn, my_champion),
            "item_build": db.get_item_build(conn, my_champion),
            "guide": db.get_matchup_notes(conn, my_champion),
        }
    finally:
        conn.close()
    envelope = {
        "app": "coach-potato", "kind": EXPORT_KIND, "version": EXPORT_VERSION,
        "my_champion": my_champion, "exported_at_ms": int(time.time() * 1000),
    }
    if password:
        envelope["encrypted"] = True
        envelope.update(crypto.encrypt_payload(payload, password))
    else:
        envelope["encrypted"] = False
        envelope.update(payload)
    filename = f"champ-guide-{my_champion.lower()}.json"
    return Response(
        content=json.dumps(envelope, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/matchups/notes/export.pdf")
def api_export_champ_guide_pdf(my_champion: str):
    if not my_champion:
        raise HTTPException(400, "provide my_champion")
    _validate_champion(my_champion)
    conn = get_conn()
    try:
        general_notes = db.get_champion_note(conn, my_champion)
        item_build = db.get_item_build(conn, my_champion)
        guide = db.get_matchup_notes(conn, my_champion)
    finally:
        conn.close()
    pdf_bytes = pdf_export.build_champion_guide_pdf(my_champion, general_notes, item_build, guide)
    filename = f"champ-guide-{my_champion.lower()}.pdf"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _decode_champ_guide_export(body):
    data = body.get("data")
    if not isinstance(data, dict) or data.get("kind") != EXPORT_KIND:
        raise HTTPException(400, "not a champ-guide export file")
    my_champion = data.get("my_champion")
    if not my_champion:
        raise HTTPException(400, "export file missing my_champion")
    if data.get("encrypted"):
        password = body.get("password")
        if not password:
            raise HTTPException(401, "password required")
        try:
            payload = crypto.decrypt_payload(
                data.get("salt"), data.get("iterations"), data.get("ciphertext"), password)
        except ValueError:
            raise HTTPException(401, "wrong password or corrupt file")
    else:
        payload = {
            "general_notes": data.get("general_notes", ""),
            "item_build": data.get("item_build") or {"core": [], "situational": []},
            "guide": data.get("guide") or {},
        }
    # Validate the payload shape here so both preview and import reject a
    # malformed/hand-edited file with a 400 instead of a 500, and so import
    # applies the same rune validation as the PUT endpoint.
    guide = payload.get("guide") or {}
    if not isinstance(guide, dict):
        raise HTTPException(400, "guide must be an object of {opponent: entry}")
    for opp_champion, entry in guide.items():
        if entry is not None and not isinstance(entry, dict):
            raise HTTPException(400, f"invalid guide entry for {opp_champion}")
        runes = (entry or {}).get("runes") or []
        if not isinstance(runes, list):
            raise HTTPException(400, "runes must be a list of rune pages")
        for page in runes:
            _validate_rune_page(page)
        _validate_patch(str((entry or {}).get("patch_version") or "").strip())
        _validate_skill_order((entry or {}).get("skill_order") or [])
    return my_champion, payload


@app.post("/api/matchups/notes/import/preview")
def api_import_champ_guide_preview(body: dict):
    my_champion, payload = _decode_champ_guide_export(body or {})
    conn = get_conn()
    try:
        existing = set(db.get_matchup_notes(conn, my_champion).keys())
    finally:
        conn.close()
    opponents = list((payload.get("guide") or {}).keys())
    item_build = payload.get("item_build") or {}
    return {
        "my_champion": my_champion,
        "opponents": opponents,
        "will_overwrite": sorted(existing & set(opponents)),
        "has_general_notes": bool(payload.get("general_notes")),
        "has_item_build": bool(item_build.get("core") or item_build.get("situational")),
    }


@app.post("/api/matchups/notes/import")
def api_import_champ_guide(body: dict):
    my_champion, payload = _decode_champ_guide_export(body or {})
    _validate_champion(my_champion)
    conn = get_conn()
    try:
        if payload.get("general_notes"):
            db.set_champion_note(conn, my_champion, payload["general_notes"])
        item_build = payload.get("item_build") or {}
        if item_build.get("core") or item_build.get("situational"):
            core, situational = _validate_item_build(
                item_build.get("core") or [], item_build.get("situational") or [])
            db.set_item_build(conn, my_champion, core, situational)
        guide = payload.get("guide") or {}
        for opp_champion, entry in guide.items():
            _validate_champion(opp_champion)
            db.set_matchup_note(
                conn, my_champion, opp_champion,
                notes=str((entry or {}).get("notes") or ""),
                runes=(entry or {}).get("runes") or [],
                patch_version=str((entry or {}).get("patch_version") or ""),
                skill_order=(entry or {}).get("skill_order") or [])
        return {"imported": len(guide)}
    finally:
        conn.close()


# Matchup notes written before the champ-guide update (v1.14.0) migrated to
# my_champion='' — preserved, but unreachable from the per-champion guide UI.
# These endpoints back a Settings section (shown only while such rows exist)
# offering to migrate them under one of your champions, or delete them.


@app.get("/api/matchups/legacy-notes")
def api_legacy_notes():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT opp_champion, notes, patch_version FROM matchup_notes
               WHERE my_champion='' AND notes != '' ORDER BY opp_champion""").fetchall()
    finally:
        conn.close()
    return {
        "count": len(rows),
        "notes": {r["opp_champion"]: {
            "notes": r["notes"], "patch_version": r["patch_version"]} for r in rows},
    }


@app.post("/api/matchups/legacy-notes/migrate")
def api_legacy_notes_migrate(body: dict):
    my_champion = (body or {}).get("my_champion")
    if not my_champion:
        raise HTTPException(400, "provide my_champion")
    _validate_champion(my_champion)
    conn = get_conn()
    try:
        with conn:
            # never overwrite a guide already written for the target champion —
            # those legacy rows stay put and are reported back as skipped
            cursor = conn.execute(
                """UPDATE matchup_notes SET my_champion=? WHERE my_champion=''
                   AND opp_champion NOT IN
                     (SELECT opp_champion FROM matchup_notes WHERE my_champion=?)""",
                (my_champion, my_champion))
            migrated = cursor.rowcount
        skipped = [r["opp_champion"] for r in conn.execute(
            "SELECT opp_champion FROM matchup_notes WHERE my_champion='' ORDER BY opp_champion")]
    finally:
        conn.close()
    return {"migrated": migrated, "skipped": skipped}


@app.delete("/api/matchups/legacy-notes")
def api_legacy_notes_delete():
    conn = get_conn()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM matchup_notes WHERE my_champion=''")
        return {"deleted": cursor.rowcount}
    finally:
        conn.close()


@app.get("/api/stats/rank-history")
def api_rank_history():
    conn = get_conn()
    try:
        players = conn.execute(
            """SELECT puuid, game_name, tag_line FROM players
               WHERE is_tracked=1 ORDER BY game_name""").fetchall()
        history = ({} if _hide_my_rank(conn)
                   else stats.rank_history(conn, [p["puuid"] for p in players]))
        return {
            "series": [{"puuid": p["puuid"],
                        "account": f"{p['game_name']}#{p['tag_line']}",
                        "points": history.get(p["puuid"], [])} for p in players],
            "sessions": [{"date": s["session_date"], "title": s["title"]}
                         for s in db.list_sessions(conn)],
        }
    finally:
        conn.close()


@app.get("/api/pool")
def api_get_pool():
    conn = get_conn()
    try:
        return db.get_pool(conn)
    finally:
        conn.close()


@app.put("/api/pool")
def api_put_pool(body: dict):
    core = body.get("core") or []
    counter = body.get("counter") or []
    if not isinstance(core, list) or not isinstance(counter, list):
        raise HTTPException(400, "core and counter must be lists of champion names")
    main_blind = (body.get("main_blind") or "").strip() or None
    core = [str(c).strip() for c in core if str(c).strip()]
    counter = [str(c).strip() for c in counter if str(c).strip()]
    if CHAMPION_IDS:
        unknown = [c for c in [main_blind, *core, *counter]
                   if c and c not in CHAMPION_IDS]
        if unknown:
            raise HTTPException(400, f"not a champion: {', '.join(unknown)}")
    conn = get_conn()
    try:
        db.set_pool(conn, main_blind, core, counter)
        # a block completed before any pool was saved gets this pool stamped
        current = conn.execute(
            """SELECT b.id FROM blocks b WHERE b.pool_snapshot IS NULL
               AND b.id = (SELECT MAX(id) FROM blocks)
               AND (SELECT COUNT(*) FROM block_games WHERE block_id = b.id) >= ?""",
            (db.get_block_size(conn),)).fetchone()
        if current:
            db.snapshot_pool_to_block(conn, current["id"])
        return db.get_pool(conn)
    finally:
        conn.close()


def _blocks_payload(conn):
    names = {r["puuid"]: r["game_name"] for r in
             conn.execute("SELECT puuid, game_name FROM players WHERE is_tracked=1")}
    games_by_block = {}
    for game in stats.block_games_detailed(conn):
        game["account"] = names.get(game["puuid"], "?")
        games_by_block.setdefault(game["block_id"], []).append(game)
    series_titles = {r["id"]: r["title"] for r in db.list_block_series(conn)}
    # positional, gapless indices (by creation order) — deleting a block and
    # making a new one never skips a number. global_index numbers across all
    # blocks (series-off display); series_index restarts per series (series-on).
    rows = db.list_blocks(conn)  # newest first
    ordered = sorted(rows, key=lambda r: r["id"])
    global_index = {r["id"]: i + 1 for i, r in enumerate(ordered)}
    series_index, per_series = {}, {}
    for r in ordered:
        per_series[r["series_id"]] = per_series.get(r["series_id"], 0) + 1
        series_index[r["id"]] = per_series[r["series_id"]]
    blocks = []
    size = db.get_block_size(conn)
    for row in rows:
        games = games_by_block.get(row["id"], [])
        closed = row["closed_at_ms"] is not None
        # pool_snapshot marks a block finalized under an earlier size setting
        finalized = closed or row["pool_snapshot"] is not None
        record = {**dict(row), "games": games, "closed": closed,
                  "complete": finalized or len(games) >= size,
                  "series_title": series_titles.get(row["series_id"], ""),
                  "series_index": series_index[row["id"]],
                  "global_index": global_index[row["id"]]}
        snapshot = record.pop("pool_snapshot", None)
        record["pool"] = json.loads(snapshot) if snapshot else None
        for key in ("start_ranks", "end_ranks"):
            raw = record.pop(key, None)
            record[key] = json.loads(raw) if raw else None
        blocks.append(record)
    return blocks


@app.get("/api/blocks")
def api_blocks():
    conn = get_conn()
    try:
        # `series` is returned independently of the blocks so a series that has
        # no games yet is still visible (and editable) the moment it's started
        series = [dict(r) for r in db.list_block_series(conn)]
        return {"blocks": _blocks_payload(conn), "block_size": db.get_block_size(conn),
                "series": series,
                "current_series_id": series[0]["id"] if series else None,
                "series_enabled": db.get_settings(conn).get("block_series_enabled") != "0"}
    finally:
        conn.close()


@app.post("/api/blocks/series")
def api_start_block_series(body: dict):
    """Start a new block series so subsequent blocks number from #1 under it.
    Optional `title`; blank → a generated "Since M/D/YYYY"."""
    title = str((body or {}).get("title") or "").strip()
    conn = get_conn()
    try:
        series_id = db.start_new_series(conn, title or None)
        return {"series_id": series_id}
    finally:
        conn.close()


@app.patch("/api/blocks/series/{series_id}")
def api_update_block_series(series_id: int, body: dict):
    """Rename a series and/or set its Markdown `goals` (what a two-week
    challenge is actually for) and `closing_notes` (the retrospective: how it
    went, were the goals met, what was actually learnt). Partial: only the keys
    present are written."""
    body = body or {}
    title = body.get("title")
    goals = body.get("goals")
    closing_notes = body.get("closing_notes")
    if title is None and goals is None and closing_notes is None:
        raise HTTPException(400, "provide title, goals and/or closing_notes")
    conn = get_conn()
    try:
        updated = db.update_block_series(
            conn, series_id,
            title=None if title is None else str(title).strip(),
            goals=None if goals is None else str(goals),
            closing_notes=None if closing_notes is None else str(closing_notes))
        if not updated:
            raise HTTPException(404, "no such series")
        row = next((r for r in db.list_block_series(conn) if r["id"] == series_id), None)
        return dict(row) if row else {"updated": True}
    finally:
        conn.close()


@app.get("/api/live-game")
def api_live_game():
    """Find a tracked account currently in a game (spectator-v5) and return
    the champions in it. Champions come back as numeric championIds (the
    frontend maps them via DDragon); role/lane isn't in the spectator payload,
    so the caller picks the lane opponent. {found: false} when nobody's live."""
    from .riot_client import NotFoundError, RateLimiter, RiotClient
    conn = get_conn()
    try:
        settings = config.resolve_settings(conn)
        if not settings["configured"]:
            raise HTTPException(400, "not configured")
        tracked = [r["puuid"] for r in
                   conn.execute("SELECT puuid FROM players WHERE is_tracked=1")]
    finally:
        conn.close()
    client = RiotClient(settings["riot_api_key"], platform=settings["platform"],
                        limiter=RateLimiter())
    for puuid in tracked:
        try:
            game = client.get_active_game(puuid)
        except NotFoundError:
            continue  # this account isn't in a game
        parts = game.get("participants") or []
        me = next((p for p in parts if p.get("puuid") == puuid), None)
        if not me:
            continue
        team = me.get("teamId")
        return {
            "found": True,
            "puuid": puuid,
            "queue_id": game.get("gameQueueConfigId"),
            "my_champion_id": me.get("championId"),
            "enemy_champion_ids": [p["championId"] for p in parts
                                   if p.get("teamId") != team and "championId" in p],
            "ally_champion_ids": [p["championId"] for p in parts
                                  if p.get("teamId") == team and p.get("puuid") != puuid
                                  and "championId" in p],
        }
    return {"found": False}


# ---------- comparison ("research") players: up to N others you compare
# yourself against in the Matchup guide. Games are pulled DEEP and in the
# BACKGROUND (Riot's dev key is slow), by count with no time window, so the UI
# isn't blocked; the guide comparison then shows whatever is stored, no date
# restriction. "Fetch more" walks further back (has_participant skip). ----------

COMPARISON_FETCH_TARGET = 300  # games pulled per add / "fetch more"

# one comparison fetch at a time; the UI polls this while it runs
COMPARISON_CRAWL = {"running": False, "puuid": None, "message": "idle",
                    "new_matches": 0, "error": None}


def _comparison_games(conn, puuid):
    return conn.execute("SELECT COUNT(*) c FROM participants WHERE puuid=?",
                        (puuid,)).fetchone()["c"]


def _fetch_comparison_player(player, api_key, prefix=""):
    """Pull up to COMPARISON_FETCH_TARGET of ONE comparison player's games (by
    count, no time window) into the db. has_participant skip means repeated
    runs walk further back, deepening history. `prefix` labels the status
    messages when this is one player of several. Returns the new-match count;
    raises on failure, which the worker below turns into a status."""
    from .crawler import Crawler
    from .riot_client import RateLimiter, RiotClient
    puuid = player["puuid"]
    client = RiotClient(api_key, platform=player["platform"], limiter=RateLimiter())
    conn = db.connect(get_db_path())
    try:
        crawler = Crawler(client, conn,
                          status_cb=lambda m: COMPARISON_CRAWL.__setitem__("message", prefix + m))
        res = crawler.crawl_player(player["game_name"], player["tag_line"],
                                   limit=COMPARISON_FETCH_TARGET,
                                   is_tracked=False,  # since_s omitted -> by count
                                   # fetch timelines so the comparison shows each
                                   # player's lane Δ @14m (costs ~2x the API calls;
                                   # deliberately re-enabled — lane Δ is wanted here)
                                   fetch_timeline=True)
        # fill lane Δ on any of this/other comparison players' older games
        # that were stored before timelines were fetched (has_timeline=0).
        crawler.backfill_lane_deltas()
        # this client is on the player's region, so it can fill loadout
        # (spells + items) on their already-stored matches that crawl_player
        # skipped (has_participant) — the global backfill can't cross region.
        crawler.backfill_items_for_player(puuid)
        crawler.backfill_timeline_items_for_player(puuid)  # start buy + build order (timeline)
        COMPARISON_CRAWL["message"] = f"{prefix}done — {_comparison_games(conn, puuid)} games stored"
        return res["new_matches"]
    finally:
        conn.close()


def _run_comparison_crawl(players, api_key):
    """Background worker: fetch each of `players` IN TURN. Sequential on
    purpose — every player gets their own region-scoped client with its own
    rate limiter, so running two at once would drive Riot's shared per-key
    limit twice as hard as either limiter knows about. One player failing
    (expired key aside, usually a bad region) doesn't abandon the rest; the
    failures are collected and reported together at the end."""
    failures = []
    total = len(players)
    try:
        for i, player in enumerate(players, 1):
            label = f"{player['game_name']}#{player['tag_line']}"
            prefix = f"({i}/{total}) {label} — " if total > 1 else ""
            COMPARISON_CRAWL.update({"puuid": player["puuid"],
                                     "message": f"{prefix}fetching games…"})
            try:
                COMPARISON_CRAWL["new_matches"] += _fetch_comparison_player(
                    player, api_key, prefix)
            except Exception as exc:  # surfaced via the status field
                failures.append(f"{label}: {exc}")
        COMPARISON_CRAWL["error"] = "; ".join(failures) or None
        if len(failures) == total:
            COMPARISON_CRAWL["message"] = "failed"
        elif total > 1:
            done = total - len(failures)
            COMPARISON_CRAWL["message"] = (
                f"done — refreshed {done} of {total} players, "
                f"{COMPARISON_CRAWL['new_matches']} new games")
    finally:
        COMPARISON_CRAWL["running"] = False


def _start_comparison_crawl(players, api_key):
    """Kick off the worker for one or more players (each a row from
    db.list_comparison_players, with `platform` already resolved)."""
    COMPARISON_CRAWL.update({"running": True, "puuid": players[0]["puuid"],
                             "message": "fetching games…", "new_matches": 0, "error": None})
    threading.Thread(target=_run_comparison_crawl, args=(players, api_key),
                     daemon=True).start()


def _comparison_platform(row, settings):
    """A comparison player's own server, falling back to yours if it's unset
    (rows predating the platform column) or no longer a known platform."""
    platform = (row.get("platform") or settings["platform"]).strip().lower()
    return platform if platform in PLATFORM_ROUTING else settings["platform"]


@app.get("/api/comparison-players")
def api_get_comparison_players():
    conn = get_conn()
    try:
        players = db.list_comparison_players(conn)
        for p in players:
            p["enabled"] = bool(p["enabled"])
            p["games"] = _comparison_games(conn, p["puuid"])
        return {"players": players, "max": None,  # no cap on comparison players
                "fetching": dict(COMPARISON_CRAWL)}
    finally:
        conn.close()


@app.post("/api/comparison-players")
def api_add_comparison_player(body: dict):
    from .riot_client import NotFoundError, RateLimiter, RiotClient
    if _riot_job_running():
        raise HTTPException(409, "a data fetch is already running — wait for it to finish")
    riot_id = (body.get("riot_id") or "").strip()
    name, _, tag = riot_id.partition("#")
    if not name.strip() or not tag.strip():
        raise HTTPException(400, "player must be Name#TAG")
    conn = get_conn()
    try:
        settings = config.resolve_settings(conn)
        if not settings["configured"]:
            raise HTTPException(400, "not configured — set your API key in Settings")
    finally:
        conn.close()
    # a comparison player can be on a different server than your own accounts;
    # default to yours when the body doesn't specify one
    platform = (body.get("platform") or settings["platform"]).strip().lower()
    if platform not in PLATFORM_ROUTING:
        raise HTTPException(400, f"unknown server/platform {platform!r}")
    client = RiotClient(settings["riot_api_key"], platform=platform, limiter=RateLimiter())
    try:
        account = client.get_account(name.strip(), tag.strip())
    except NotFoundError:
        raise HTTPException(404, f"no Riot account {riot_id!r}")
    puuid = account["puuid"]
    game_name = account.get("gameName", name.strip())
    tag_line = account.get("tagLine", tag.strip())
    # Register as a comparison player FIRST: the crawler only stores per-match
    # metrics/runes for puuids in comparison_players (or tracked).
    conn = get_conn()
    try:
        db.add_comparison_player(conn, puuid, game_name, tag_line, platform=platform)
    finally:
        conn.close()
    _start_comparison_crawl([{"puuid": puuid, "game_name": game_name, "tag_line": tag_line,
                              "platform": platform}], settings["riot_api_key"])
    return {"puuid": puuid, "game_name": game_name, "tag_line": tag_line, "started": True}


@app.post("/api/comparison-players/refresh-all")
def api_comparison_refresh_all():
    """Fetch new games for EVERY research player, one after another in one
    background job — the alternative is clicking "Fetch more" down the list and
    waiting for each to finish, since only one Riot job may run at a time."""
    if _riot_job_running():
        raise HTTPException(409, "a data fetch is already running — wait for it to finish")
    conn = get_conn()
    try:
        settings = config.resolve_settings(conn)
        players = db.list_comparison_players(conn)
    finally:
        conn.close()
    if not settings["configured"]:
        raise HTTPException(400, "not configured — set your API key in Settings")
    if not players:
        raise HTTPException(400, "no research players to refresh")
    _start_comparison_crawl(
        [{**p, "platform": _comparison_platform(p, settings)} for p in players],
        settings["riot_api_key"])
    return {"started": True, "players": len(players)}


@app.post("/api/comparison-players/{puuid}/fetch-more")
def api_comparison_fetch_more(puuid: str):
    if _riot_job_running():
        raise HTTPException(409, "a data fetch is already running — wait for it to finish")
    conn = get_conn()
    try:
        settings = config.resolve_settings(conn)
        row = next((p for p in db.list_comparison_players(conn) if p["puuid"] == puuid), None)
        if row is None:
            raise HTTPException(404, "not a comparison player")
        if not settings["configured"]:
            raise HTTPException(400, "not configured — set your API key in Settings")
    finally:
        conn.close()
    _start_comparison_crawl([{**row, "platform": _comparison_platform(row, settings)}],
                            settings["riot_api_key"])
    return {"puuid": puuid, "started": True}


MAX_COMPARISON_NOTE = 200  # a label beside the name, not a write-up


@app.patch("/api/comparison-players/{puuid}")
def api_patch_comparison_player(puuid: str, body: dict):
    """Partial update: `enabled` and `note` are written independently, so
    toggling the checkbox never clobbers a note being edited, or vice versa."""
    enabled, note = body.get("enabled"), body.get("note")
    if "enabled" in body and not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be a boolean")
    if "note" in body:
        if not isinstance(note, str):
            raise HTTPException(400, "note must be a string")
        note = note.strip()
        if len(note) > MAX_COMPARISON_NOTE:
            raise HTTPException(400, f"note must be at most {MAX_COMPARISON_NOTE} characters")
    if "enabled" not in body and "note" not in body:
        raise HTTPException(400, "nothing to update — pass enabled and/or note")
    written = {"puuid": puuid}
    conn = get_conn()
    try:
        if "enabled" in body:
            db.set_comparison_enabled(conn, puuid, enabled)
            written["enabled"] = enabled
        if "note" in body:
            db.set_comparison_note(conn, puuid, note)
            written["note"] = note
    finally:
        conn.close()
    return written


@app.delete("/api/comparison-players/{puuid}")
def api_delete_comparison_player(puuid: str):
    conn = get_conn()
    try:
        db.remove_comparison_player(conn, puuid)
    finally:
        conn.close()
    return {"ok": True}


COMPARISON_SCOPES = ("matchup", "champion", "overall")


@app.get("/api/comparison")
def api_comparison(scope: str = "matchup", my_champion: str = "", opp_champion: str = ""):
    """You vs the enabled comparison players, measured the same way on both
    sides. `scope` picks what's being compared:
      - matchup:  my_champion vs opp_champion (the Playbook / Matchups rows)
      - champion: my_champion against everyone (the My champions rows)
      - overall:  every tracked game, no champion filter (Coaching progress —
                  deliberately TOTALS, not the per-session segments)
    `you` aggregates ALL tracked accounts, matching how coaching progress
    treats you as a player rather than an account. Returns you + [] players
    when comparison is off, so callers still render your own column."""
    if scope not in COMPARISON_SCOPES:
        raise HTTPException(400, f"scope must be one of {', '.join(COMPARISON_SCOPES)}")
    if scope != "overall" and my_champion:
        _validate_champion(my_champion)
    if scope == "matchup" and opp_champion:
        _validate_champion(opp_champion)
    champion = my_champion or None if scope != "overall" else None
    opponent = opp_champion or None if scope == "matchup" else None
    conn = get_conn()
    try:
        tracked = [r["puuid"] for r in
                   conn.execute("SELECT puuid FROM players WHERE is_tracked=1")]
        you = (stats.comparison_entry(conn, tracked, champion, opponent) if tracked
               else {"scoped": None, "overall": None, "recent": []})
        out = []
        if db.get_settings(conn).get("enable_player_comparison") == "1":
            for p in db.list_comparison_players(conn):
                if not p["enabled"]:
                    continue
                data = stats.comparison_entry(conn, p["puuid"], champion, opponent)
                out.append({"puuid": p["puuid"], "game_name": p["game_name"],
                            "tag_line": p["tag_line"], **data})
        return {"scope": scope, "my_champion": champion or "",
                "opp_champion": opponent or "", "you": you, "players": out}
    finally:
        conn.close()


def _game_date(game):
    return datetime.fromtimestamp(game["game_creation_ms"] / 1000,
                                  tz=timezone.utc).strftime("%Y-%m-%d")


def _blocks_for_export(conn, block_id):
    blocks = _blocks_payload(conn)
    if block_id is None:
        return blocks
    selected = [b for b in blocks if b["id"] == block_id]
    if not selected:
        raise HTTPException(404, "no such block")
    return selected


@app.get("/api/blocks/export.md")
def api_blocks_export_md(block_id: int | None = None):
    conn = get_conn()
    try:
        blocks = _blocks_for_export(conn, block_id)
    finally:
        conn.close()
    parts = ["# Block Learnings\n"]
    for block in blocks:
        wins = sum(g["win"] for g in block["games"])
        title = f" — {block['title']}" if block["title"] else ""
        parts.append(f"\n## Block #{block['id']}{title} "
                     f"({wins}–{len(block['games']) - wins})\n")
        pool = block["pool"]
        if pool:
            parts.append(f"\nPool: {pool['main_blind'] or '–'}"
                         f" · Core: {', '.join(pool['core']) or '–'}"
                         f" · Counters: {', '.join(pool['counter']) or '–'}\n")
        parts.append("\n")
        for g in block["games"]:
            opp = f" vs {g['opp_champion']}" if g["opp_champion"] else ""
            line = (f"- {_game_date(g)} · {g['account']} · {g['my_champion']}{opp}"
                    f" · {'W' if g['win'] else 'L'}"
                    f" · {g['kills']}/{g['deaths']}/{g['assists']}")
            note_lines = g["notes"].splitlines() if g["notes"] else []
            if len(note_lines) == 1 and not note_lines[0].startswith("- "):
                line += f" — {note_lines[0]}"
            else:
                # multi-line / list-style notes nest under the game bullet
                for note in note_lines:
                    bullet = note if note.startswith("- ") else f"- {note}"
                    line += f"\n  {bullet}"
            parts.append(line + "\n")
        if block["learnings"]:
            parts.append(f"\n### Learnings\n\n{block['learnings']}\n")
    return Response(
        content="".join(parts),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="block-learnings.md"'},
    )


@app.get("/api/blocks/export.csv")
def api_blocks_export_csv(block_id: int | None = None):
    import csv
    import io

    conn = get_conn()
    try:
        blocks = _blocks_for_export(conn, block_id)
    finally:
        conn.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["block", "title", "date", "account", "champion", "opponent",
                     "result", "kills", "deaths", "assists", "notes", "learnings"])
    for block in blocks:
        for g in block["games"]:
            writer.writerow([
                block["id"], block["title"], _game_date(g), g["account"],
                g["my_champion"], g["opp_champion"] or "",
                "W" if g["win"] else "L", g["kills"], g["deaths"], g["assists"],
                g["notes"], block["learnings"],
            ])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="block-learnings.csv"'},
    )


@app.get("/api/blocks/noted-champions")
def api_block_noted_champions():
    """Opponent champions that have at least one block-game note — drives the
    block-notes indicator in the matchups table."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT DISTINCT opp.champion_name AS champ
               FROM block_games bg
               JOIN participants me ON me.match_id = bg.match_id AND me.puuid = bg.puuid
               JOIN participants opp ON opp.match_id = bg.match_id
                   AND opp.team_id != me.team_id AND opp.team_position = me.team_position
                   AND me.team_position != ''
               WHERE TRIM(bg.notes) != ''""").fetchall()
        return sorted(r["champ"] for r in rows if r["champ"])
    finally:
        conn.close()


@app.get("/api/blocks/game-notes")
def api_block_game_notes(opp_champion: str):
    """Read-only: block-game notes from games against the given champion,
    newest first (my champion is filtered client-side)."""
    if not opp_champion:
        raise HTTPException(400, "opp_champion query param required")
    conn = get_conn()
    try:
        blocks_by_id = {b["id"]: b for b in db.list_blocks(conn)}
        names = {r["puuid"]: r["game_name"] for r in
                 conn.execute("SELECT puuid, game_name FROM players WHERE is_tracked=1")}

        def block_field(block_id, field):
            block = blocks_by_id.get(block_id)
            return block[field] if block else ""

        notes = [{
            "block_id": g["block_id"],
            "block_title": block_field(g["block_id"], "title"),
            "block_learnings": block_field(g["block_id"], "learnings"),
            "match_id": g["match_id"],
            "puuid": g["puuid"],
            "account": names.get(g["puuid"], "?"),
            "game_creation_ms": g["game_creation_ms"],
            "my_champion": g["my_champion"],
            "opp_champion": g["opp_champion"],
            "win": g["win"],
            "notes": g["notes"],
        } for g in stats.block_games_detailed(conn)
            if g["opp_champion"] == opp_champion and g["notes"].strip()]
        notes.reverse()  # block_games_detailed is oldest first
        return notes
    finally:
        conn.close()


@app.post("/api/blocks/games")
def api_add_block_game(body: dict):
    match_id = (body or {}).get("match_id")
    puuid = (body or {}).get("puuid")
    if not match_id or not puuid:
        raise HTTPException(400, "match_id and puuid required")
    conn = get_conn()
    try:
        known = conn.execute(
            "SELECT 1 FROM participants WHERE match_id=? AND puuid=?",
            (match_id, puuid)).fetchone()
        if not known:
            raise HTTPException(404, "no such game for that account")
        holder = db.find_block_for_game(conn, match_id, puuid)
        if holder is not None:  # duplicate check before any gap side-effects
            raise HTTPException(409, f"game is already in Block #{holder}")
        gap = db.block_gap_exceeded(conn, match_id)
        if gap is not None:
            gap_block, gap_ms = gap
            confirm_on = db.get_settings(conn).get("block_gap_confirm") != "0"
            if confirm_on and not body.get("confirm_gap"):
                # 412: the client confirms, then retries with confirm_gap
                raise HTTPException(412, {
                    "reason": "gap", "block_id": gap_block,
                    "gap_hours": round(gap_ms / 3_600_000, 1),
                })
            db.close_block(conn, gap_block)  # auto-close, new block below
        try:
            block_id = db.add_game_to_block(conn, match_id, puuid)
        except sqlite3.IntegrityError:
            holder = db.find_block_for_game(conn, match_id, puuid)
            raise HTTPException(409, f"game is already in Block #{holder}")
        return {"block_id": block_id}
    finally:
        conn.close()


@app.post("/api/blocks/{block_id}/close")
def api_close_block(block_id: int):
    conn = get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM blocks WHERE id=?", (block_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "no such block")
        if not db.close_block(conn, block_id):
            raise HTTPException(409, "block is already closed or complete")
        return {"closed": True}
    finally:
        conn.close()


@app.patch("/api/blocks/{block_id}")
def api_update_block(block_id: int, body: dict):
    title = body.get("title")
    learnings = body.get("learnings")
    if title is None and learnings is None:
        raise HTTPException(400, "provide title and/or learnings")
    conn = get_conn()
    try:
        if not db.update_block(conn, block_id, title=title, learnings=learnings):
            raise HTTPException(404, "no such block")
        return {"updated": True}
    finally:
        conn.close()


@app.patch("/api/blocks/games/{entry_id}")
def api_update_block_game(entry_id: int, body: dict):
    """Partial update of a block game: `notes` (Markdown), `weakside`
    (true/false/null — the manual side flag), and/or `lane_result_7`/
    `lane_result_14` (each one of db.LANE_RESULT_VALUES, or null — the manual
    lane-verdict override, graded independently per mark)."""
    body = body or {}
    has_notes = body.get("notes") is not None
    has_weakside = "weakside" in body
    lane_result_marks = [m for m in (7, 14) if f"lane_result_{m}" in body]
    if not has_notes and not has_weakside and not lane_result_marks:
        raise HTTPException(400, "provide notes, weakside, and/or lane_result_7/lane_result_14")
    for m in lane_result_marks:
        value = body[f"lane_result_{m}"]
        if value is not None and value not in db.LANE_RESULT_VALUES:
            raise HTTPException(400, f"invalid lane_result_{m}")
    conn = get_conn()
    try:
        ok = True
        if has_notes:
            ok = db.update_block_game(conn, entry_id, body["notes"])
        if has_weakside:
            ok = db.set_block_game_weakside(conn, entry_id, body["weakside"]) and ok
        for m in lane_result_marks:
            ok = db.set_block_game_lane_result(conn, entry_id, m, body[f"lane_result_{m}"]) and ok
        if not ok:
            raise HTTPException(404, "no such block game")
        return {"updated": True}
    finally:
        conn.close()


@app.delete("/api/blocks/games/{entry_id}")
def api_delete_block_game(entry_id: int):
    conn = get_conn()
    try:
        freed = db.delete_clips_for_owner(conn, "block_game", entry_id)
        if not db.delete_block_game(conn, entry_id):
            raise HTTPException(404, "no such block game")
        _unlink_clip_files(freed)
        return {"deleted": True}
    finally:
        conn.close()


@app.delete("/api/blocks/{block_id}")
def api_delete_block(block_id: int):
    conn = get_conn()
    try:
        freed = db.delete_clips_for_block(conn, block_id)
        if not db.delete_block(conn, block_id):
            raise HTTPException(404, "no such block")
        _unlink_clip_files(freed)
        return {"deleted": True}
    finally:
        conn.close()


def _research_entry_dict(conn, row):
    d = dict(row)
    d["screenshots"] = [
        {**dict(s), "file_url": f"/api/research/screenshots/{s['id']}/file"}
        for s in db.list_research_screenshots(conn, d["id"])]
    return d


def _validate_champion_if_given(champion):
    if champion:
        _validate_champion(champion)


@app.get("/api/research")
def api_list_research():
    conn = get_conn()
    try:
        return [dict(r) for r in db.list_research_entries(conn)]
    finally:
        conn.close()


@app.get("/api/research/{entry_id}")
def api_get_research_entry(entry_id: int):
    conn = get_conn()
    try:
        row = db.get_research_entry(conn, entry_id)
        if not row:
            raise HTTPException(404, "no such research entry")
        return _research_entry_dict(conn, row)
    finally:
        conn.close()


@app.post("/api/research")
def api_create_research_entry(body: dict):
    body = body or {}
    player_name = str(body.get("player_name") or "").strip()
    champion = str(body.get("champion") or "").strip()
    opp_champion = str(body.get("opp_champion") or "").strip()
    if not player_name:
        raise HTTPException(400, "player_name is required")
    _validate_champion_if_given(champion)
    _validate_champion_if_given(opp_champion)
    conn = get_conn()
    try:
        entry_id = db.create_research_entry(
            conn, player_name, champion, opp_champion,
            str(body.get("title") or ""), str(body.get("notes") or ""))
        return _research_entry_dict(conn, db.get_research_entry(conn, entry_id))
    finally:
        conn.close()


@app.patch("/api/research/{entry_id}")
def api_update_research_entry(entry_id: int, body: dict):
    body = body or {}
    conn = get_conn()
    try:
        existing = db.get_research_entry(conn, entry_id)
        if not existing:
            raise HTTPException(404, "no such research entry")
        player_name = str(body.get("player_name", existing["player_name"]) or "").strip()
        champion = str(body.get("champion", existing["champion"]) or "").strip()
        opp_champion = str(body.get("opp_champion", existing["opp_champion"]) or "").strip()
        if not player_name:
            raise HTTPException(400, "player_name is required")
        _validate_champion_if_given(champion)
        _validate_champion_if_given(opp_champion)
        db.update_research_entry(
            conn, entry_id, player_name, champion, opp_champion,
            str(body.get("title", existing["title"]) or ""),
            str(body.get("notes", existing["notes"]) or ""))
        return _research_entry_dict(conn, db.get_research_entry(conn, entry_id))
    finally:
        conn.close()


@app.delete("/api/research/{entry_id}")
def api_delete_research_entry(entry_id: int):
    conn = get_conn()
    try:
        screenshots = db.list_research_screenshots(conn, entry_id)
        if not db.delete_research_entry(conn, entry_id):
            raise HTTPException(404, "no such research entry")
        _unlink_screenshot_files([s["file_name"] for s in screenshots])
        return {"deleted": True}
    finally:
        conn.close()


@app.post("/api/research/{entry_id}/screenshots")
async def api_add_research_screenshot(entry_id: int, caption: str = Form(""),
                                      file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_SCREENSHOT_EXTENSIONS:
        raise HTTPException(
            400, f"unsupported file type {ext or '(none)'} — "
                 f"allowed: {', '.join(sorted(ALLOWED_SCREENSHOT_EXTENSIONS))}")
    data = await file.read(MAX_SCREENSHOT_BYTES + 1)
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(413, "screenshot exceeds the 15 MB limit")
    conn = get_conn()
    try:
        if not db.get_research_entry(conn, entry_id):
            raise HTTPException(404, "no such research entry")
        stored_name = f"{uuid.uuid4().hex}{ext}"
        (get_research_screenshots_dir() / stored_name).write_bytes(data)
        db.add_research_screenshot(conn, entry_id, caption, stored_name)
        return [{**dict(s), "file_url": f"/api/research/screenshots/{s['id']}/file"}
                for s in db.list_research_screenshots(conn, entry_id)]
    finally:
        conn.close()


@app.get("/api/research/screenshots/{screenshot_id}/file")
def api_research_screenshot_file(screenshot_id: int):
    conn = get_conn()
    try:
        screenshot = db.get_research_screenshot(conn, screenshot_id)
    finally:
        conn.close()
    if not screenshot:
        raise HTTPException(404, "screenshot not found")
    path = get_research_screenshots_dir() / screenshot["file_name"]
    if not path.exists():
        raise HTTPException(404, "screenshot file missing on disk")
    return FileResponse(path)


@app.delete("/api/research/screenshots/{screenshot_id}")
def api_delete_research_screenshot(screenshot_id: int):
    conn = get_conn()
    try:
        screenshot = db.get_research_screenshot(conn, screenshot_id)
        if not screenshot:
            raise HTTPException(404, "screenshot not found")
        db.delete_research_screenshot(conn, screenshot_id)
    finally:
        conn.close()
    _unlink_screenshot_files([screenshot["file_name"]])
    return {"deleted": True}


# ---------- tier lists ----------

_MAX_TIERS = 12
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_tier_data(data):
    """Clean a tier-list payload: {"tiers": [{label, color, champions:[ids]}]}.
    Unknown champions are dropped (roster changes over patches); returns the
    normalised dict."""
    if not isinstance(data, dict):
        raise HTTPException(400, "tier list data must be an object")
    tiers = data.get("tiers")
    if not isinstance(tiers, list):
        raise HTTPException(400, "data.tiers must be a list")
    if len(tiers) > _MAX_TIERS:
        raise HTTPException(400, f"at most {_MAX_TIERS} tiers")
    known = {c.lower(): c for c in CHAMPION_IDS}
    seen, out = set(), []
    for t in tiers:
        if not isinstance(t, dict):
            raise HTTPException(400, "each tier must be an object")
        color = str(t.get("color") or "").strip()
        if color and not _HEX_COLOR.match(color):
            raise HTTPException(400, f"bad tier color {color!r}")
        champs = []
        for c in (t.get("champions") or []):
            key = str(c).lower()
            if key in known and known[key] not in seen:  # valid + no dupes across tiers
                champs.append(known[key])
                seen.add(known[key])
        # optional row icon: a champion (default) or a rune name
        image = str(t.get("image") or "")
        kind = "rune" if t.get("image_kind") == "rune" else "champion"
        if image and kind == "rune":
            if _RUNE_ALL_NAMES and image not in _RUNE_ALL_NAMES:
                image = ""
        elif image:
            image = known.get(image.lower(), "")
        if not image:
            kind = "champion"
        out.append({"label": str(t.get("label") or "")[:24], "color": color,
                    "image": image, "image_kind": kind, "champions": champs})
    # champions marked with a "?" (uncertain) — validated ids, deduped
    flagged = []
    for c in (data.get("flagged") or []):
        real = known.get(str(c).lower())
        if real and real not in flagged:
            flagged.append(real)
    result = {"tiers": out}
    if flagged:
        result["flagged"] = flagged
    return result


def _tier_list_dict(row):
    d = dict(row)
    try:
        d["data"] = json.loads(d["data"]) if d["data"] else {"tiers": []}
    except (ValueError, TypeError):
        d["data"] = {"tiers": []}
    return d


@app.get("/api/champion-roles")
def api_champion_roles():
    """Each champion's lane(s) inferred from stored games, for filtering the
    tier-list pool by role. See stats.champion_roles."""
    conn = get_conn()
    try:
        return stats.champion_roles(conn)
    finally:
        conn.close()


@app.get("/api/tier-lists")
def api_list_tier_lists(scope: str = "standalone"):
    """The editable tier lists of the Tier list tab. Copies saved into a
    champion's Matchup guide live in the same table with `champion` set and are
    read back via /api/champions/{champion}/tier-lists — `scope=all` returns
    both (what the compare window offers to pick from)."""
    conn = get_conn()
    try:
        champion = None if scope == "all" else ""
        return [_tier_list_dict(r) for r in db.list_tier_lists(conn, champion=champion)]
    finally:
        conn.close()


@app.get("/api/champions/{champion}/tier-lists")
def api_champion_tier_lists(champion: str):
    """Tier lists saved into this champion's Matchup guide, oldest first. These
    are SNAPSHOTS taken in the Tier list tab (see api_save_champion_tier_list) —
    the guide only displays and deletes them, it never edits them."""
    _validate_champion(champion)
    conn = get_conn()
    try:
        return [_tier_list_dict(r) for r in db.list_tier_lists(conn, champion=champion)]
    finally:
        conn.close()


@app.post("/api/champions/{champion}/tier-lists")
def api_save_champion_tier_list(champion: str, body: dict):
    """Copy a tier list into a champion's Matchup guide. A champion can hold
    several; re-saving one with the SAME title overwrites that copy (reported
    back as `replaced`) instead of piling up duplicates."""
    _validate_champion(champion)
    body = body or {}
    title = str(body.get("title") or "").strip() or "Tier list"
    data = _validate_tier_data(body.get("data") or {"tiers": []})
    conn = get_conn()
    try:
        existing = next((r for r in db.list_tier_lists(conn, champion=champion)
                         if (r["title"] or "").strip().lower() == title.lower()), None)
        if existing:
            db.update_tier_list(conn, existing["id"], title=title, data=data)
            tid, replaced = existing["id"], True
        else:
            tid = db.create_tier_list(conn, title, data, champion=champion)
            replaced = False
        out = _tier_list_dict(db.get_tier_list(conn, tid))
        out["replaced"] = replaced
        return out
    finally:
        conn.close()


@app.post("/api/tier-lists")
def api_create_tier_list(body: dict):
    body = body or {}
    title = str(body.get("title") or "").strip() or "Tier list"
    data = _validate_tier_data(body.get("data") or {"tiers": []})
    conn = get_conn()
    try:
        tid = db.create_tier_list(conn, title, data)
        return _tier_list_dict(db.get_tier_list(conn, tid))
    finally:
        conn.close()


@app.put("/api/tier-lists/{tier_list_id}")
def api_update_tier_list(tier_list_id: int, body: dict):
    body = body or {}
    conn = get_conn()
    try:
        existing = db.get_tier_list(conn, tier_list_id)
        if not existing:
            raise HTTPException(404, "no such tier list")
        title = str(body.get("title", existing["title"]) or "").strip() or "Tier list"
        data = _validate_tier_data(body["data"]) if "data" in body else None
        db.update_tier_list(conn, tier_list_id, title=title, data=data)
        return _tier_list_dict(db.get_tier_list(conn, tier_list_id))
    finally:
        conn.close()


@app.delete("/api/tier-lists/{tier_list_id}")
def api_delete_tier_list(tier_list_id: int):
    conn = get_conn()
    try:
        if not db.delete_tier_list(conn, tier_list_id):
            raise HTTPException(404, "no such tier list")
        return {"deleted": True}
    finally:
        conn.close()


def _clip_dict(row):
    d = dict(row)
    if d["kind"] == "upload":
        d["play_url"] = f"/api/clips/{d['id']}/file"
    else:
        d["play_url"] = d["url"]
    return d


@app.get("/api/clips")
def api_list_clips(owner_type: str, owner_id: int):
    if owner_type not in CLIP_OWNER_TABLES:
        raise HTTPException(400, f"owner_type must be one of {sorted(CLIP_OWNER_TABLES)}")
    conn = get_conn()
    try:
        return [_clip_dict(r) for r in db.list_clips(conn, owner_type, owner_id)]
    finally:
        conn.close()


@app.post("/api/clips")
async def api_add_clip(owner_type: str = Form(...), owner_id: int = Form(...),
                        label: str = Form(""), url: str | None = Form(None),
                        file: UploadFile | None = File(None)):
    if owner_type not in CLIP_OWNER_TABLES:
        raise HTTPException(400, f"owner_type must be one of {sorted(CLIP_OWNER_TABLES)}")
    if bool(file) == bool(url):
        raise HTTPException(400, "provide exactly one of: file, url")
    conn = get_conn()
    try:
        owner_exists = conn.execute(
            f"SELECT 1 FROM {CLIP_OWNER_TABLES[owner_type]} WHERE id=?", (owner_id,)
        ).fetchone()
        if not owner_exists:
            raise HTTPException(404, f"no such {owner_type}")
        if file:
            ext = Path(file.filename or "").suffix.lower()
            if ext not in ALLOWED_CLIP_EXTENSIONS:
                raise HTTPException(
                    400, f"unsupported file type {ext or '(none)'} — "
                         f"allowed: {', '.join(sorted(ALLOWED_CLIP_EXTENSIONS))}")
            data = await file.read(MAX_CLIP_BYTES + 1)
            if len(data) > MAX_CLIP_BYTES:
                raise HTTPException(413, "clip exceeds the 50 MB limit")
            stored_name = f"{uuid.uuid4().hex}{ext}"
            (get_clips_dir() / stored_name).write_bytes(data)
            clip_id = db.add_clip(conn, owner_type, owner_id, label, "upload",
                                  file_name=stored_name)
        else:
            if not url.startswith(("http://", "https://")):
                raise HTTPException(400, "url must start with http:// or https://")
            clip_id = db.add_clip(conn, owner_type, owner_id, label, "link", url=url)
        return _clip_dict(db.get_clip(conn, clip_id))
    finally:
        conn.close()


@app.get("/api/clips/{clip_id}/file")
def api_clip_file(clip_id: int):
    conn = get_conn()
    try:
        clip = db.get_clip(conn, clip_id)
    finally:
        conn.close()
    if not clip or clip["kind"] != "upload":
        raise HTTPException(404, "clip not found")
    path = get_clips_dir() / clip["file_name"]
    if not path.exists():
        raise HTTPException(404, "clip file missing on disk")
    return FileResponse(path)


@app.delete("/api/clips/{clip_id}")
def api_delete_clip(clip_id: int):
    conn = get_conn()
    try:
        clip = db.get_clip(conn, clip_id)
        if not clip:
            raise HTTPException(404, "clip not found")
        db.delete_clip(conn, clip_id)
    finally:
        conn.close()
    if clip["kind"] == "upload" and clip["file_name"]:
        _unlink_clip_files([clip["file_name"]])
    return {"deleted": True}


# ---------- recordings (local Ascent VODs) ----------

# background YouTube upload state, same shape as CRAWL_STATE
UPLOAD_STATE = {"running": False, "uuid": None, "progress": 0.0,
                "error": None, "video_id": None}


def _sync_ascent_log_events(conn):
    """Best-effort: no Ascent logs (or an unreadable one) must never fail the
    recording sync, which is the part that matters."""
    log_dir = ascent_log.default_log_dir()
    if not log_dir:
        return {"skipped": "no Ascent log directory"}
    try:
        accounts = json.loads(db.get_settings(conn).get("accounts") or "[]")
        return ascent_log.sync(conn, log_dir, accounts)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _ascent_db_path(conn):
    configured = db.get_settings(conn).get("ascent_db_path")
    return configured or recordings.default_ascent_db_path()


def _recording_payload(row, conn=None, puuid=None):
    out = {
        "uuid": row["uuid"], "match_id": row["match_id"],
        "video_path": row["video_path"], "started_at_ms": row["started_at_ms"],
        "duration_s": row["duration_s"], "offset_ms": row["offset_ms"],
        "file_exists": os.path.exists(row["video_path"]),
        "youtube_video_id": row["youtube_video_id"],
        "youtube_url": (youtube.watch_url(row["youtube_video_id"])
                        if row["youtube_video_id"] else None),
        "youtube_uploaded_at_ms": row["youtube_uploaded_at_ms"],
    }
    if conn is not None and puuid:
        out["deaths"] = recordings.death_markers(
            conn, row["match_id"], puuid, row["offset_ms"])
        # the map draws only what has coordinates, read independently of the
        # chapter list's source so it never goes blank when the log wins
        out["events"] = recordings.positioned_markers(
            conn, row["match_id"], puuid, row["offset_ms"])
        # every event, positioned or not — the seek buttons under the video
        out["marks"] = recordings.timeline_markers(
            conn, row["match_id"], puuid, row["offset_ms"])
    return out


@app.get("/api/recordings")
def api_recordings(match_id: str, puuid: str = ""):
    """Recordings for one game, with each death as a video position."""
    conn = get_conn()
    try:
        return {"recordings": [_recording_payload(r, conn, puuid)
                               for r in db.recordings_for_match(conn, match_id)]}
    finally:
        conn.close()


@app.get("/api/recordings/matches")
def api_recorded_matches():
    """Every match id we have a recording for — lets the games tables show a
    marker without one request per row."""
    conn = get_conn()
    try:
        return {"match_ids": sorted(db.recorded_match_ids(conn))}
    finally:
        conn.close()


@app.post("/api/recordings/sync")
def api_sync_recordings():
    conn = get_conn()
    try:
        path = _ascent_db_path(conn)
        if not path:
            raise HTTPException(
                400, "No Ascent database found. Set its path in Settings.")
        try:
            result = recordings.sync(conn, path)
            # second, offline source: Ascent's logs carry League's Live Client
            # event feed (kills/towers/objectives) for recent games, so VOD
            # chapters fill in without a Riot API key
            result["events"] = _sync_ascent_log_events(conn)
            return result
        except FileNotFoundError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:  # unexpected schema
            raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@app.patch("/api/recordings/{rec_uuid}")
def api_update_recording(rec_uuid: str, body: dict):
    """Nudge a recording's sync offset (ms; negative = video runs ahead)."""
    offset = (body or {}).get("offset_ms")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise HTTPException(400, "offset_ms must be a whole number of milliseconds")
    conn = get_conn()
    try:
        if not db.set_recording_offset(conn, rec_uuid, offset):
            raise HTTPException(404, "no such recording")
        return {"updated": True}
    finally:
        conn.close()


@app.delete("/api/recordings/{rec_uuid}")
def api_delete_recording(rec_uuid: str):
    """Forget the link. The video file itself is never touched."""
    conn = get_conn()
    try:
        if not db.delete_recording(conn, rec_uuid):
            raise HTTPException(404, "no such recording")
        return {"deleted": True}
    finally:
        conn.close()


@app.get("/api/recordings/upload-status")
def api_upload_status():
    return UPLOAD_STATE


@app.get("/api/recordings/{rec_uuid}/description")
def api_recording_description(rec_uuid: str, puuid: str):
    """A ready-to-paste YouTube description with a chapter per death."""
    conn = get_conn()
    try:
        row = db.get_recording(conn, rec_uuid)
        if not row:
            raise HTTPException(404, "no such recording")
        return {"description": recordings.build_description(conn, row, puuid)}
    finally:
        conn.close()


@app.post("/api/recordings/{rec_uuid}/reveal")
def api_reveal_recording(rec_uuid: str):
    """Show the video in the OS file manager, for the manual upload path.

    Nothing leaves the machine — this only opens Explorer/Finder with the file
    selected so it can be dragged into youtube.com/upload. Returns the path so
    the caller can also offer it as copyable text (pasting the path into
    YouTube's file picker is quicker than dragging between windows).
    """
    conn = get_conn()
    try:
        row = db.get_recording(conn, rec_uuid)
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "no such recording")
    try:
        recordings.reveal_in_file_manager(row["video_path"])
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"revealed": True, "video_path": row["video_path"]}


@app.get("/api/recordings/{rec_uuid}/file")
def api_recording_file(rec_uuid: str):
    """Stream the local video so a <video> element can play and seek it.

    Only paths already in our recordings table are servable — the id is looked
    up, never taken from the request — so this can't be pointed at an arbitrary
    file. FileResponse handles Range requests, which is what makes seeking to a
    death timestamp work without downloading the whole file.
    """
    conn = get_conn()
    try:
        row = db.get_recording(conn, rec_uuid)
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "no such recording")
    path = Path(row["video_path"])
    if not path.exists():
        raise HTTPException(404, f"video file is gone: {path}")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


def _run_upload(rec_uuid, video_path, title, description, privacy,
                client_secrets, db_dir):
    try:
        video_id = youtube.upload(
            video_path, title, description, privacy,
            client_secrets_path=client_secrets, db_dir=db_dir,
            on_progress=lambda p: UPLOAD_STATE.update(progress=p))
        conn = db.connect(get_db_path())
        try:
            db.set_recording_youtube(conn, rec_uuid, video_id, privacy)
        finally:
            conn.close()
        UPLOAD_STATE.update(video_id=video_id, progress=1.0)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        UPLOAD_STATE.update(error=str(exc))
    finally:
        UPLOAD_STATE.update(running=False)


@app.post("/api/recordings/{rec_uuid}/youtube")
def api_upload_recording(rec_uuid: str, body: dict = None):
    """Start a YouTube upload for one recording, in a background thread.

    Deliberately explicit: the caller passes the title/description it showed
    the user, and privacy defaults to whatever Settings says (private unless
    changed). Poll /api/recordings/upload-status for progress.
    """
    body = body or {}
    if UPLOAD_STATE["running"]:
        raise HTTPException(409, "an upload is already running")
    conn = get_conn()
    try:
        row = db.get_recording(conn, rec_uuid)
        if not row:
            raise HTTPException(404, "no such recording")
        if not os.path.exists(row["video_path"]):
            raise HTTPException(400, f"video file is gone: {row['video_path']}")
        stored = db.get_settings(conn)
        privacy = body.get("privacy") or stored.get("youtube_privacy") \
            or youtube.DEFAULT_PRIVACY
        if privacy not in youtube.PRIVACY_VALUES:
            raise HTTPException(400, "invalid privacy")
        client_secrets = stored.get("youtube_client_secrets")
        if not youtube.has_credentials(client_secrets, get_db_path().parent):
            raise HTTPException(
                400, "YouTube isn't set up yet — add your OAuth client secrets "
                     "file in Settings first.")
        title = (body.get("title") or Path(row["video_path"]).stem)[:youtube.MAX_TITLE]
        # default to the generated description (matchup + a chapter per death)
        # so a one-click upload lands with timestamps already on it
        description = body.get("description")
        if description is None and body.get("puuid"):
            description = recordings.build_description(conn, row, body["puuid"])
        description = (description or "")[:youtube.MAX_DESCRIPTION]
    finally:
        conn.close()
    UPLOAD_STATE.update(running=True, uuid=rec_uuid, progress=0.0,
                        error=None, video_id=None)
    threading.Thread(
        target=_run_upload,
        args=(rec_uuid, row["video_path"], title, description, privacy,
              client_secrets, str(get_db_path().parent)),
        daemon=True).start()
    return {"started": True, "privacy": privacy}


# ---------- OBS-recorded coaching sessions (see server/obs.py) ----------

# One cached obs-websocket connection, reused across requests: the status poll
# runs every couple of seconds while recording, and reconnecting each time would
# spam OBS's own log with connect/disconnect lines. FastAPI runs sync endpoints
# in a threadpool, hence the lock.
OBS_CONN = {"client": None, "key": None}
_OBS_LOCK = threading.RLock()
# OBS's encoder takes a moment to spin up after StartRecord: GetRecordStatus
# can still say "not recording" for the first instant, and the UI polls status
# immediately after starting. Without this grace a brand-new row was closed 16
# ms after it was created (observed in the wild) while OBS recorded on. A stop
# EVENT overrides the grace — an outputPath in hand is definitive.
OBS_START_GRACE_MS = 10_000
# how far back a stop event's path may backfill an orphaned row (one that got
# closed without a path) — beyond this it is likelier to be the wrong video
OBS_BACKFILL_WINDOW_MS = 60 * 60 * 1000


def _now_ms():
    return int(time.time() * 1000)


def _obs_settings(conn):
    stored = db.get_settings(conn)
    port = stored.get("obs_port")
    return {"host": stored.get("obs_host") or obs.DEFAULT_HOST,
            "port": int(port) if port else obs.DEFAULT_PORT,
            "password": stored.get("obs_password") or ""}


def _obs_drop():
    client, OBS_CONN["client"], OBS_CONN["key"] = OBS_CONN["client"], None, None
    if client is not None:
        client.close()


def _obs_call(settings, action):
    """Run `action(client)` on the shared connection, reconnecting once if the
    cached one has gone away (OBS restarted, machine slept). Only transport
    failures are retried — an ObsError that is OBS answering "no" (already
    recording, unknown request) would just fail again."""
    key = (settings["host"], settings["port"], settings["password"])
    with _OBS_LOCK:
        if OBS_CONN["client"] is not None and OBS_CONN["key"] != key:
            _obs_drop()  # settings changed under us
        if OBS_CONN["client"] is not None:
            try:
                return action(OBS_CONN["client"])
            except obs.ObsConnectionError:
                _obs_drop()
        client = obs.connect(**settings)
        OBS_CONN.update(client=client, key=key)
        return action(client)


def _obs_request(conn, action, settings=None):
    """_obs_call, with OBS's own errors turned into a 502 the UI can print."""
    try:
        return _obs_call(settings or _obs_settings(conn), action)
    except obs.ObsError as exc:
        raise HTTPException(502, str(exc)) from exc


def _session_recording_payload(row, conn):
    """`video_path` is what OBS reported; `play_path` is what we would actually
    serve — the same file unless a playable remux sits beside an .mkv."""
    stored_path = row["video_path"] or ""
    play_path = obs.playable_path(stored_path) if stored_path else ""
    exists = bool(play_path) and os.path.exists(play_path)
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "label": row["label"],
        "source": row["source"],
        "video_path": stored_path,
        "play_path": play_path,
        "started_at_ms": row["started_at_ms"],
        "stopped_at_ms": row["stopped_at_ms"],
        "recording": row["stopped_at_ms"] is None,
        "file_exists": exists,
        # an .mkv (OBS's default format) exists but no browser will play it
        "playable": exists and obs.is_playable(play_path),
        "play_url": f"/api/session-recordings/{row['id']}/file" if exists else None,
        "marks": [dict(m) for m in db.list_session_marks(conn, row["id"])],
    }


def _finish_recording(conn, row, video_path=""):
    db.update_session_recording(conn, row["id"], video_path=video_path or None,
                                stopped_at_ms=_now_ms())
    return db.get_session_recording(conn, row["id"])


def _require_session(conn, session_id):
    if not conn.execute("SELECT 1 FROM coaching_sessions WHERE id=?",
                        (session_id,)).fetchone():
        raise HTTPException(404, "no such coaching session")


@app.get("/api/obs/status")
def api_obs_status():
    """Whether OBS is reachable and what it is doing, plus the recording row
    still marked as rolling (if any). Polled by the session card."""
    conn = get_conn()
    try:
        active = db.active_session_recording(conn)
        out = {"available": obs.libraries_available(), "connected": False,
               "recording": False, "paused": False, "duration_ms": 0,
               "error": None, "active": None, "finished": None}
        try:
            # one round trip serves both: reading the status also drains any
            # queued RecordStateChanged event, whose outputPath is the file of
            # a recording that was stopped in OBS rather than here
            status, stopped_path = _obs_call(
                _obs_settings(conn),
                lambda c: (c.record_status(), c.take_last_output_path()))
        except obs.ObsError as exc:
            out["error"] = str(exc)
            # OBS being unreachable tells us nothing about the row — leave it open
            if active:
                out["active"] = _session_recording_payload(active, conn)
            return out
        out.update(connected=True, **status)
        if active and not status["recording"]:
            age_ms = _now_ms() - (active["started_at_ms"] or 0)
            if stopped_path or age_ms > OBS_START_GRACE_MS:
                # recording ended outside the app (stopped in OBS, or the app
                # was closed mid-session). If OBS's stop event went by on this
                # connection we have the file; otherwise the row closes without
                # a path and the UI offers to attach it. A row younger than the
                # grace is NOT closed on status alone — right after StartRecord
                # OBS can briefly report "not recording" while the encoder
                # spins up, and closing on that instant orphans a live
                # recording. The stop event overrides the grace: a path in
                # hand is definitive.
                finished = _finish_recording(conn, active, stopped_path)
                out["finished"] = _session_recording_payload(finished, conn)
                active = None
        elif stopped_path and not active:
            # a stop event with no open row: its file belongs to a recording
            # whose row was already closed without a path (the app missed the
            # stop, or an early reconciliation got it). Heal the newest such
            # row rather than dropping a path we know is right.
            orphan = conn.execute(
                """SELECT * FROM session_recordings
                   WHERE source='obs' AND video_path='' AND stopped_at_ms IS NOT NULL
                     AND stopped_at_ms > ?
                   ORDER BY id DESC LIMIT 1""",
                (_now_ms() - OBS_BACKFILL_WINDOW_MS,)).fetchone()
            if orphan:
                db.update_session_recording(conn, orphan["id"], video_path=stopped_path)
                out["finished"] = _session_recording_payload(
                    db.get_session_recording(conn, orphan["id"]), conn)
        if active:
            out["active"] = _session_recording_payload(active, conn)
        return out
    finally:
        conn.close()


@app.get("/api/obs/record-format")
def api_obs_record_format():
    """Preflight for the Record button: what container OBS would record into,
    and whether the app could play it back. The UI warns BEFORE starting when
    the answer is a format no browser plays (OBS's default .mkv), so the user
    can switch OBS to mp4 first instead of finding out after the session."""
    conn = get_conn()
    try:
        record_format = _obs_request(conn, lambda c: c.record_format())
        return {"format": record_format,
                "playable": obs.format_playable(record_format)}
    finally:
        conn.close()


@app.post("/api/obs/test")
def api_obs_test(body: dict = None):
    """Settings' "Test connection" button. Takes the values currently typed into
    the form, so the connection can be checked before saving; anything not
    passed falls back to what is stored."""
    body = body or {}
    conn = get_conn()
    try:
        settings = _obs_settings(conn)
        if body.get("host"):
            settings["host"] = str(body["host"]).strip()
        if body.get("port"):
            try:
                settings["port"] = int(body["port"])
            except (TypeError, ValueError):
                raise HTTPException(400, "port must be a number")
        if "password" in body:
            settings["password"] = body.get("password") or ""

        def probe(client):
            info = dict(client.version())
            info.update(client.record_status())
            try:  # nice-to-have: where OBS writes its files
                info["record_directory"] = (
                    client.request("GetRecordDirectory") or {}).get("recordDirectory", "")
            except obs.ObsError:
                info["record_directory"] = ""
            info["record_format"] = client.record_format()
            info["format_playable"] = obs.format_playable(info["record_format"])
            return info
        return {"connected": True, **_obs_request(conn, probe, settings)}
    finally:
        conn.close()


@app.get("/api/sessions/{session_id}/recordings")
def api_session_recordings(session_id: int):
    conn = get_conn()
    try:
        return {"recordings": [_session_recording_payload(r, conn)
                               for r in db.list_session_recordings(conn, session_id)]}
    finally:
        conn.close()


@app.post("/api/sessions/{session_id}/recordings/start")
def api_start_session_recording(session_id: int, body: dict = None):
    """Tell OBS to start recording, and open a row for this session so live
    bookmarks have something to attach to before the file exists."""
    conn = get_conn()
    try:
        _require_session(conn, session_id)
        if db.active_session_recording(conn):
            raise HTTPException(409, "a recording is already in progress")
        _obs_request(conn, lambda c: c.start_record())
        recording_id = db.add_session_recording(
            conn, session_id, label=((body or {}).get("label") or "").strip(),
            source="obs", started_at_ms=_now_ms())
        return _session_recording_payload(
            db.get_session_recording(conn, recording_id), conn)
    finally:
        conn.close()


@app.post("/api/sessions/{session_id}/recordings/stop")
def api_stop_session_recording(session_id: int):
    """Stop OBS and store the path it reports. If OBS turns out not to be
    recording, the row is still closed — with no path — so the UI can offer to
    attach the file by hand instead of leaving a row rolling forever."""
    conn = get_conn()
    try:
        active = db.active_session_recording(conn)
        if not active or active["session_id"] != session_id:
            raise HTTPException(404, "this session is not recording")
        path = _obs_request(conn, lambda c: c.stop_record())
        return _session_recording_payload(_finish_recording(conn, active, path), conn)
    finally:
        conn.close()


@app.post("/api/sessions/{session_id}/recordings/attach")
def api_attach_session_recording(session_id: int, body: dict):
    """Point a session at a video file that already exists — one recorded
    before this was set up, or one whose stop we missed. The file is only ever
    read from where it is."""
    path = (body.get("path") or "").strip().strip('"')
    if not path:
        raise HTTPException(400, "path is required")
    if not os.path.isfile(path):
        raise HTTPException(400, f"no file at {path}")
    conn = get_conn()
    try:
        _require_session(conn, session_id)
        now = _now_ms()
        recording_id = db.add_session_recording(
            conn, session_id, label=(body.get("label") or "").strip(),
            source="manual", started_at_ms=now, video_path=path, stopped_at_ms=now)
        return _session_recording_payload(
            db.get_session_recording(conn, recording_id), conn)
    finally:
        conn.close()


@app.patch("/api/session-recordings/{recording_id}")
def api_update_session_recording(recording_id: int, body: dict):
    label = body.get("label")
    if label is None:
        raise HTTPException(400, "nothing to update — pass label")
    conn = get_conn()
    try:
        if not db.update_session_recording(conn, recording_id, label=str(label)):
            raise HTTPException(404, "no such recording")
        return _session_recording_payload(
            db.get_session_recording(conn, recording_id), conn)
    finally:
        conn.close()


@app.delete("/api/session-recordings/{recording_id}")
def api_delete_session_recording(recording_id: int, delete_file: bool = False):
    """Forget the recording and its bookmarks. By default the video file is
    left alone (the Ascent-VOD rule); `?delete_file=true` — sent only after
    the user explicitly confirmed it in the UI — also removes the file from
    disk, plus a remuxed sibling if one exists, since to the user both ARE
    this recording. This is the one place a video file may ever be deleted."""
    conn = get_conn()
    try:
        row = db.get_session_recording(conn, recording_id)
        if not row:
            raise HTTPException(404, "no such recording")
        db.delete_session_recording(conn, recording_id)
    finally:
        conn.close()
    removed = []
    if delete_file and row["video_path"]:
        for candidate in {row["video_path"], obs.playable_path(row["video_path"])}:
            target = Path(candidate)
            if target.is_file():
                target.unlink(missing_ok=True)
                removed.append(str(target))
    return {"deleted": True, "files_removed": removed}


@app.get("/api/session-recordings/{recording_id}/file")
def api_session_recording_file(recording_id: int):
    """Stream the local video so <video> can play and seek it. Only paths
    already in the table are servable — the id is looked up, never taken from
    the request. FileResponse handles Range requests, which is what makes
    seeking to a bookmark work without downloading the whole file."""
    conn = get_conn()
    try:
        row = db.get_session_recording(conn, recording_id)
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "no such recording")
    path = Path(obs.playable_path(row["video_path"] or ""))
    if not row["video_path"] or not path.exists():
        raise HTTPException(404, f"video file is gone: {row['video_path']}")
    return FileResponse(path, media_type=obs.media_type(path), filename=path.name)


def _live_offset_ms(conn, row):
    try:
        status = _obs_call(_obs_settings(conn), lambda c: c.record_status())
        if status["recording"] and status["duration_ms"]:
            return status["duration_ms"]
    except obs.ObsError:
        pass  # OBS unreachable: wall-clock is close enough to keep marking
    return max(0, _now_ms() - (row["started_at_ms"] or _now_ms()))


@app.post("/api/session-recordings/{recording_id}/marks")
def api_add_session_mark(recording_id: int, body: dict = None):
    """Bookmark a moment. While OBS is rolling the offset comes from OBS's own
    outputDuration (right even if OBS took a moment to start, and it does not
    advance while paused); wall-clock since the start is the fallback, and an
    explicit offset_ms wins over both — that is how a finished video is marked
    from the player."""
    body = body or {}
    conn = get_conn()
    try:
        row = db.get_session_recording(conn, recording_id)
        if not row:
            raise HTTPException(404, "no such recording")
        offset_ms = body.get("offset_ms")
        if offset_ms is None:
            if row["stopped_at_ms"] is not None:
                raise HTTPException(
                    400, "offset_ms is required once a recording has stopped")
            offset_ms = _live_offset_ms(conn, row)
        mark_id = db.add_session_mark(conn, recording_id, int(offset_ms),
                                      (body.get("label") or "").strip())
        return dict(db.get_session_mark(conn, mark_id))
    finally:
        conn.close()


@app.patch("/api/session-marks/{mark_id}")
def api_update_session_mark(mark_id: int, body: dict):
    label, offset_ms = body.get("label"), body.get("offset_ms")
    if label is None and offset_ms is None:
        raise HTTPException(400, "nothing to update — pass label and/or offset_ms")
    conn = get_conn()
    try:
        if not db.update_session_mark(conn, mark_id, label=label, offset_ms=offset_ms):
            raise HTTPException(404, "no such bookmark")
        return dict(db.get_session_mark(conn, mark_id))
    finally:
        conn.close()


@app.delete("/api/session-marks/{mark_id}")
def api_delete_session_mark(mark_id: int):
    conn = get_conn()
    try:
        if not db.delete_session_mark(conn, mark_id):
            raise HTTPException(404, "no such bookmark")
        return {"deleted": True}
    finally:
        conn.close()


def _run_crawl():
    try:
        from .crawler import Crawler
        from .riot_client import RateLimiter, RiotClient

        conn = db.connect(get_db_path())
        settings = config.resolve_settings(conn)
        if not settings["configured"]:
            raise RuntimeError("not configured — set your API key and accounts in Settings")

        def on_wait(seconds):
            if seconds >= 2:  # ignore sub-second burst throttling
                CRAWL_STATE["rate_limited"] = True

        client = RiotClient(settings["riot_api_key"], platform=settings["platform"],
                            limiter=RateLimiter(on_wait=on_wait))

        def status_cb(msg):
            CRAWL_STATE["message"] = msg
            CRAWL_STATE["rate_limited"] = False  # progress resumed

        crawler = Crawler(client, conn, status_cb=status_cb)
        results = []
        for account in settings["accounts"]:
            game_name, _, tag_line = account.partition("#")
            CRAWL_STATE["message"] = f"crawling {account}"
            results.append(crawler.crawl_player(game_name, tag_line))
        CRAWL_STATE["message"] = "fetching opponent ranks"
        crawler.enrich_ranks()
        crawler.backfill_metrics()
        crawler.backfill_lane_deltas(block_games_only=True)  # deepen block-game stats
        crawler.refresh_tracked_ranks()
        # link any local Ascent VODs to the games we just crawled. Best-effort:
        # Ascent not being installed (or its schema having moved) must never
        # fail the crawl, which is the part that actually matters.
        ascent_path = _ascent_db_path(conn)
        if ascent_path:
            try:
                CRAWL_STATE["message"] = "linking recordings"
                summary = recordings.sync(conn, ascent_path)
                summary["events"] = _sync_ascent_log_events(conn)
                CRAWL_STATE["recordings"] = summary
            except Exception as exc:  # noqa: BLE001
                CRAWL_STATE["recordings"] = {"error": str(exc)}
        db.set_settings(conn, {"last_crawl_ms": str(int(time.time() * 1000))})
        conn.close()
        CRAWL_STATE["last_result"] = results
        CRAWL_STATE["message"] = "done"
        CRAWL_STATE["error"] = None
    except Exception as exc:  # surfaced via /api/crawl/status
        CRAWL_STATE["error"] = str(exc)
        CRAWL_STATE["message"] = "failed"
    finally:
        CRAWL_STATE["running"] = False


@app.post("/api/crawl")
def api_crawl():
    if _riot_job_running():
        return JSONResponse({"detail": "a crawl or timeline fetch is already running"},
                            status_code=409)
    CRAWL_STATE.update({"running": True, "message": "starting", "error": None,
                        "rate_limited": False})
    threading.Thread(target=_run_crawl, daemon=True).start()
    return {"started": True}


def _run_timeline_backfill():
    try:
        from .crawler import Crawler
        from .riot_client import RateLimiter, RiotClient

        conn = db.connect(get_db_path())
        settings = config.resolve_settings(conn)
        if not settings["configured"]:
            raise RuntimeError("not configured")
        client = RiotClient(settings["riot_api_key"], platform=settings["platform"],
                            limiter=RateLimiter())

        def status_cb(msg):  # "lane-delta backfill: 3/8 matches"
            head, _, tail = msg.partition(": ")
            done, _, total = tail.replace(" matches", "").partition("/")
            TIMELINE_STATE["done"] = int(done or 0)
            TIMELINE_STATE["total"] = int(total or 0)

        crawler = Crawler(client, conn, status_cb=status_cb)
        crawler.backfill_lane_deltas(block_games_only=True)
        conn.close()
        TIMELINE_STATE["error"] = None
    except Exception as exc:  # surfaced via /api/blocks/timeline-status
        TIMELINE_STATE["error"] = str(exc)
    finally:
        TIMELINE_STATE["running"] = False


@app.post("/api/blocks/backfill-timelines")
def api_backfill_block_timelines():
    """Kick off a background fetch of match timelines for block games missing
    lane deltas (has_timeline=0). No-op (not an error) if nothing is pending
    or a Riot job is already running."""
    conn = get_conn()
    try:
        pending = conn.execute(
            """SELECT COUNT(*) c FROM participant_metrics pm
               WHERE pm.has_timeline = 0 AND EXISTS (
                 SELECT 1 FROM block_games bg
                 WHERE bg.match_id = pm.match_id AND bg.puuid = pm.puuid)""").fetchone()["c"]
    finally:
        conn.close()
    if _riot_job_running() or not pending:
        return {"started": False, "pending": pending}
    TIMELINE_STATE.update({"running": True, "done": 0, "total": pending, "error": None})
    threading.Thread(target=_run_timeline_backfill, daemon=True).start()
    return {"started": True, "pending": pending}


@app.get("/api/blocks/timeline-status")
def api_block_timeline_status():
    return TIMELINE_STATE


@app.get("/api/crawl/status")
def api_crawl_status():
    return CRAWL_STATE


class _NoStaleStatic(StaticFiles):
    """StaticFiles that forces revalidation on every request. There is no
    build step and no version-stamped URLs, so after an upgrade a browser was
    free to keep serving last week's app.js/style.css against today's API —
    mixed old-UI/new-backend states that look like bizarre layout bugs.
    `no-cache` does NOT mean "don't cache": the browser keeps its copy and
    asks "still current?" each time, getting a cheap 304 (this app is
    localhost — the round trip is microseconds) or the new file the moment
    one exists. Same-mtime edits within one second are the ETag's blind spot,
    which only ever matters mid-development, never for upgrades."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", _NoStaleStatic(directory=PROJECT_ROOT / "static", html=True), name="static")
