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

from . import config, crypto, db, pdf_export, rune_data, stats
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
    """True while any background job is making Riot API calls — a full crawl
    or the block-timeline backfill. Both must not run at once or they'd each
    drive their own rate limiter and together exceed Riot's limits."""
    return CRAWL_STATE["running"] or TIMELINE_STATE["running"]


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
    }


@app.get("/api/version")
def api_version():
    return {"version": config.app_version(), "repo": config.GITHUB_REPO}


HIDEABLE_VIEWS = {"overview", "matchups", "progress", "trends", "blocks", "guide", "research"}
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
        session_id = db.add_session(conn, date_str,
                                    title=(body.get("title") or "").strip(),
                                    notes=body.get("notes") or "")
        return {"id": session_id}
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a session on {date_str} already exists")
    finally:
        conn.close()


@app.patch("/api/sessions/{session_id}")
def api_update_session(session_id: int, body: dict):
    title = body.get("title")
    notes = body.get("notes")
    if title is None and notes is None:
        raise HTTPException(400, "provide title and/or notes")
    conn = get_conn()
    try:
        if not db.update_session(conn, session_id, title=title, notes=notes):
            raise HTTPException(404, "no such session")
        return {"updated": True}
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
            """SELECT session_date, title, notes, start_ranks, created_at_ms
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
    finally:
        conn.close()

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
    for row in payload.get("research_screenshots") or []:
        if conn.execute("SELECT 1 FROM research_screenshots WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"research screenshot #{row['id']}")
    for row in payload.get("clips") or []:
        if conn.execute("SELECT 1 FROM clips WHERE id=?", (row["id"],)).fetchone():
            conflicts.append(f"clip #{row['id']}")
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
                       (session_date, title, notes, start_ranks, created_at_ms)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["session_date"], row.get("title", ""), row.get("notes", ""),
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
        return {"imported": _import_counts(payload)}
    finally:
        conn.close()


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: int):
    conn = get_conn()
    try:
        freed = db.delete_clips_for_owner(conn, "session", session_id)
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
            side=params.get("side") or None)
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
            side=params.get("side") or None)
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
            side=params.get("side") or None)
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


@app.get("/api/stats/trends")
def api_trends(request: Request, bucket: str = "month"):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        try:
            puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
            buckets = stats.trend_buckets(
                conn, puuids, bucket=bucket,
                champion=params.get("champion") or None, queues=queues,
                side=params.get("side") or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"buckets": buckets, "meta": METRICS}
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


@app.get("/api/champions/notes/{champion}")
def api_get_champion_note(champion: str):
    conn = get_conn()
    try:
        return {"notes": db.get_champion_note(conn, champion)}
    finally:
        conn.close()


@app.put("/api/champions/notes/{champion}")
def api_put_champion_note(champion: str, body: dict):
    body = body or {}
    if "notes" not in body:
        raise HTTPException(400, "provide notes")
    _validate_champion(champion)
    conn = get_conn()
    try:
        db.set_champion_note(conn, champion, str(body.get("notes") or ""))
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
    series_titles = {r["id"]: r["title"] for r in
                     conn.execute("SELECT id, title FROM block_series")}
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
        return {"blocks": _blocks_payload(conn), "block_size": db.get_block_size(conn),
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
                   AND opp.team_id != me.team_id AND opp.team_position = 'TOP'
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
    if body.get("notes") is None:
        raise HTTPException(400, "provide notes")
    conn = get_conn()
    try:
        if not db.update_block_game(conn, entry_id, body["notes"]):
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


app.mount("/", StaticFiles(directory=PROJECT_ROOT / "static", html=True), name="static")
