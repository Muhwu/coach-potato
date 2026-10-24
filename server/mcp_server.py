"""Model Context Protocol server: Coach Potato's data as AI tools.

This is what lets Claude (or any MCP client) read your stats, notes and
coaching history, and write notes back. It speaks MCP's **Streamable HTTP**
transport directly — like `server/obs.py` speaks obs-websocket directly —
rather than pulling in the MCP SDK, because the protocol surface a tool server
needs is small and the packaged PyInstaller build is what pays for every new
dependency (see the `--hidden-import websocket` note in CLAUDE.md). What a
client actually sends:

    -> POST /mcp  {"method": "initialize", ...}            <- capabilities
    -> POST /mcp  {"method": "notifications/initialized"}  <- 202, no body
    -> POST /mcp  {"method": "tools/list"}                 <- every TOOLS entry
    -> POST /mcp  {"method": "tools/call", params: {name, arguments}}

Each POST is self-contained: no session state, so nothing has to survive a
restart and `Mcp-Session-Id` is never issued. The spec lets a server answer a
POST with either `application/json` or a one-event SSE stream; app.py picks
based on the client's Accept header, so a client that insists on
`text/event-stream` works too.

**Why there is a token.** claude.ai in the browser cannot reach localhost —
connectors are called from Anthropic's servers over the public internet — so
using this from the web app means putting a tunnel in front of it, and the
moment that exists, `/mcp` is a public, writable door into the user's coaching
data. So: the feature is OFF by default, `/mcp` 404s while it is off, and every
request must carry the token (`Authorization: Bearer`, `X-Api-Key`, or
`/mcp/<token>` in the path — the path form is for claude.ai's "No sign-in"
auth, since its request-header option is still a limited beta). Tunnel only the
`/mcp` path, never `/`: every other endpoint here is unauthenticated and
`GET /api/settings` hands out the Riot API key.

Tools are deliberately coarse — a dozen that answer coaching questions, not
one per REST endpoint. A model picks well from a short, well-described list and
badly from a hundred near-duplicates.
"""
from __future__ import annotations

import json
import secrets
import time

from . import db, stats
from .config import PROJECT_ROOT
from .metrics import METRICS

# Versions we will echo back on initialize. The tool surface is identical
# across them, so whichever one a client asks for is the one we agree to;
# anything unknown gets our latest and the client decides whether to continue.
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL = PROTOCOL_VERSIONS[-1]
SERVER_NAME = "coach-potato"
TOKEN_SETTING = "mcp_token"
ENABLED_SETTING = "mcp_enabled"

# Period presets, the same vocabulary as the UI's range filters (app.py's
# RANGE_PRESETS) — spelled out again rather than imported, because app.py
# imports this module and a tool argument wants a plain enum anyway.
RANGE_DAYS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90, "180d": 180, "365d": 365}
RANGE_VALUES = ["all", *RANGE_DAYS]
ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
MAX_SEARCH_HITS = 60
SNIPPET_CHARS = 400


class McpError(Exception):
    """A tool refused its arguments. Reported to the model as a tool error
    (isError), not as a JSON-RPC error — so the model can correct itself."""


# ---------------------------------------------------------------- the token

def is_enabled(conn) -> bool:
    """Off unless explicitly switched on in Settings. Opt-in, because turning
    it on is the first half of exposing this data to the internet."""
    return db.get_settings(conn).get(ENABLED_SETTING) == "1"


def get_token(conn) -> str:
    return db.get_settings(conn).get(TOKEN_SETTING) or ""


def ensure_token(conn) -> str:
    """The stored token, generating one on first use. 32 url-safe bytes: it
    travels inside a URL for claude.ai's no-sign-in connectors, so it has to be
    both unguessable and safe to paste as a path segment."""
    token = get_token(conn)
    if not token:
        token = secrets.token_urlsafe(32)
        db.set_settings(conn, {TOKEN_SETTING: token})
    return token


def rotate_token(conn) -> str:
    """A new token, invalidating every connector configured with the old one."""
    token = secrets.token_urlsafe(32)
    db.set_settings(conn, {TOKEN_SETTING: token})
    return token


def token_matches(conn, presented: str) -> bool:
    """Constant-time compare against the stored token. An unset token never
    matches — a server with no token is closed, not open."""
    stored = get_token(conn)
    if not stored or not presented:
        return False
    return secrets.compare_digest(stored, presented)


# ------------------------------------------------------------ tool registry

TOOLS: list[dict] = []


def tool(name, description, schema=None, writes=False):
    """Register an MCP tool. `schema` is the JSON Schema for `arguments`;
    `writes` marks the ones that change user content, which is what drives the
    read-only hint clients show before they call."""
    def register(fn):
        TOOLS.append({
            "name": name,
            "description": description,
            "inputSchema": schema or {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": not writes, "destructiveHint": False},
            "handler": fn,
        })
        return fn
    return register


def _schema(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


_RANGE_PROP = {"type": "string", "enum": RANGE_VALUES,
               "description": "Period to cover. Default 'all'."}
_CHAMPION_PROP = {"type": "string", "description": "DDragon champion id, e.g. "
                  "'Gwen' or 'MonkeyKing' (Wukong). Omit for all champions."}
_ROLE_PROP = {"type": "string", "enum": ROLES,
              "description": "Your own role. Omit for all roles."}


def tool_list():
    """The `tools/list` payload — the registry minus our python handlers."""
    return [{k: v for k, v in entry.items() if k != "handler"} for entry in TOOLS]


def _find(name):
    for entry in TOOLS:
        if entry["name"] == name:
            return entry
    return None


# -------------------------------------------------------------- arg helpers

def _str(args, key, default=""):
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise McpError(f"{key} must be a string")
    return str(value).strip()


def _int(args, key, default, low=None, high=None):
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise McpError(f"{key} must be a number")
    value = int(value)
    if low is not None and value < low:
        raise McpError(f"{key} must be >= {low}")
    if high is not None and value > high:
        raise McpError(f"{key} must be <= {high}")
    return value


def _required(args, key):
    value = _str(args, key)
    if not value:
        raise McpError(f"{key} is required")
    return value


def _required_int(args, key):
    if args.get(key) is None:
        raise McpError(f"{key} is required")
    return _int(args, key, None)


def _from_ms(args, now_ms=None):
    """Translate a `range` argument into a from-timestamp (None = all time)."""
    preset = _str(args, "range", "all") or "all"
    if preset == "all":
        return None
    if preset not in RANGE_DAYS:
        raise McpError(f"range must be one of {RANGE_VALUES}")
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return now_ms - RANGE_DAYS[preset] * 86_400_000


def _roles(args):
    role = _str(args, "role")
    if not role:
        return None
    role = role.upper()
    if role not in ROLES:
        raise McpError(f"role must be one of {ROLES}")
    return [role]


def _tracked(conn):
    puuids = [r["puuid"] for r in
              conn.execute("SELECT puuid FROM players WHERE is_tracked=1")]
    if not puuids:
        raise McpError("no tracked accounts yet — add one in Coach Potato's "
                       "Settings and run a crawl first")
    return puuids


def _champion_ids():
    """The static champion roster, for validating what a write names. A twin of
    app.py's `_champion_ids()`; kept local because app.py imports this module."""
    try:
        data = json.loads((PROJECT_ROOT / "static" / "champions.json").read_text())
        return {c["id"] for c in data["champions"]}
    except (OSError, KeyError, ValueError):
        return set()  # roster file missing/corrupt: skip validation, as app.py does


CHAMPION_IDS = _champion_ids()


def _champion(args, key):
    name = _required(args, key)
    if CHAMPION_IDS and name not in CHAMPION_IDS:
        raise McpError(f"{name!r} is not a champion id — use the DDragon id "
                       "(e.g. 'MonkeyKing' for Wukong)")
    return name


# --------------------------------------------------------------- read tools

@tool("get_overview",
      "Win rate, KDA and per-champion totals for the tracked accounts, plus "
      "the 20 most recent games. The place to start for 'how am I doing'.",
      _schema({"range": _RANGE_PROP, "champion": _CHAMPION_PROP, "role": _ROLE_PROP,
               "min_games": {"type": "integer", "minimum": 1,
                             "description": "Drop champions with fewer games than "
                                            "this. Default 1."}}))
def _get_overview(conn, args):
    return stats.summary(conn, _tracked(conn), from_ms=_from_ms(args),
                         champion=_str(args, "champion") or None,
                         min_games=_int(args, "min_games", 1, low=1),
                         roles=_roles(args))


@tool("get_matchups",
      "Per-opponent results in your own role (you vs the enemy in that same "
      "role): games, win rate, KDA, CS and coaching-metric averages. Use it to "
      "find which matchups are actually losing.",
      _schema({"range": _RANGE_PROP, "champion": _CHAMPION_PROP, "role": _ROLE_PROP,
               "min_games": {"type": "integer", "minimum": 1,
                             "description": "Minimum games per matchup. Default 1."}}))
def _get_matchups(conn, args):
    return stats.matchups(conn, _tracked(conn), from_ms=_from_ms(args),
                          champion=_str(args, "champion") or None,
                          min_games=_int(args, "min_games", 1, low=1),
                          roles=_roles(args))


@tool("get_recent_games",
      "Individual games, newest first: result, KDA, CS, lane advantage at the 7 "
      "and 14 minute marks, and the reflection you wrote (tags + note) if any. "
      "A game's match_id and my_puuid are what set_game_reflection takes.",
      _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100,
                         "description": "How many games. Default 20."},
               "range": _RANGE_PROP, "champion": _CHAMPION_PROP, "role": _ROLE_PROP,
               "opp_champion": {"type": "string",
                                "description": "Only games against this champion."}}))
def _get_recent_games(conn, args):
    limit = _int(args, "limit", 20, low=1, high=100)
    games = stats.games_in_range(
        conn, _tracked(conn), from_ms=_from_ms(args),
        champion=_str(args, "champion") or None,
        opp_champion=_str(args, "opp_champion") or None, roles=_roles(args))
    rows = []
    for game in games[:limit]:
        game = dict(game)
        game["reflection"] = db.get_reflection(conn, game["match_id"], game["my_puuid"])
        rows.append(game)
    return rows


def _metric_meta():
    """Just enough of the registry for a model to read the numbers: what each
    key is called, and whether higher is better (direction 1/-1/0)."""
    return {m["key"]: {"label": m["label"], "group": m["group"],
                       "direction": m["direction"], "agg": m["agg"],
                       "suffix": m["suffix"]}
            for m in METRICS}


@tool("get_trends",
      "Coaching metrics bucketed over time (day/week/month) — the numbers "
      "behind the Trends view. `meta` describes each metric: its label, whether "
      "higher is better, and how it is aggregated.",
      _schema({"bucket": {"type": "string", "enum": ["day", "week", "month"],
                          "description": "Bucket size. Default 'month'."},
               "range": _RANGE_PROP, "champion": _CHAMPION_PROP, "role": _ROLE_PROP}))
def _get_trends(conn, args):
    bucket = _str(args, "bucket", "month") or "month"
    if bucket not in ("day", "week", "month"):
        raise McpError("bucket must be day, week or month")
    buckets = stats.trend_buckets(conn, _tracked(conn), bucket=bucket,
                                  from_ms=_from_ms(args),
                                  champion=_str(args, "champion") or None,
                                  roles=_roles(args))
    return {"buckets": buckets, "meta": _metric_meta()}


@tool("get_coaching_progress",
      "Did coaching work? One period per coaching session (plus a pre-coaching "
      "baseline), each carrying that period's stats and the session's own notes "
      "— so 'what we worked on' reads next to 'what changed after'.",
      _schema({"champion": _CHAMPION_PROP, "role": _ROLE_PROP}))
def _get_coaching_progress(conn, args):
    sessions = [dict(row) for row in db.list_sessions(conn)]
    if not sessions:
        return {"segments": [], "note": "no coaching sessions recorded yet"}
    return {"segments": stats.progress_segments(
        conn, _tracked(conn), sessions,
        champion=_str(args, "champion") or None, roles=_roles(args))}


@tool("list_coaching_sessions",
      "Every coaching session: date, title, coach, category and the full "
      "Markdown notes.")
def _list_coaching_sessions(conn, args):
    keep = ("id", "session_date", "title", "coach", "category", "link", "notes")
    return [{k: row[k] for k in keep if k in row.keys()}
            for row in db.list_sessions(conn)]


@tool("get_learnings",
      "The whole arc of every practice-block series: its goals, each block's "
      "learnings, and the closing retrospective. This is where the written "
      "coaching history lives.",
      _schema({"series_id": {"type": "integer",
                             "description": "Only this series. Omit for all."}}))
def _get_learnings(conn, args):
    wanted = args.get("series_id")
    blocks_by_series = {}
    for block in db.list_blocks(conn):
        blocks_by_series.setdefault(block["series_id"], []).append(
            {"id": block["id"], "title": block["title"],
             "learnings": block["learnings"], "closed_at_ms": block["closed_at_ms"]})
    out = []
    for row in db.list_block_series(conn):
        if wanted is not None and row["id"] != wanted:
            continue
        out.append({"id": row["id"], "title": row["title"], "goals": row["goals"],
                    "closing_notes": row["closing_notes"],
                    "start_date": row["start_date"], "end_date": row["end_date"],
                    "blocks": blocks_by_series.get(row["id"], [])})
    return out


@tool("get_playbook",
      "Your written plan for a champion: general notes, the item build, and "
      "per-matchup notes with rune pages, patch and skill order. Pass "
      "`opp_champion` for one matchup, omit it for every matchup you have "
      "written up.",
      _schema({"champion": dict(_CHAMPION_PROP, description="Your champion (required)."),
               "opp_champion": {"type": "string",
                                "description": "Narrow to this one opponent."}},
              required=["champion"]))
def _get_playbook(conn, args):
    champion = _champion(args, "champion")
    matchups = db.get_matchup_notes(conn, champion)
    opponent = _str(args, "opp_champion")
    if opponent:
        matchups = {opponent: matchups.get(
            opponent, {"notes": "", "runes": [], "patch_version": "", "skill_order": []})}
    return {"champion": champion,
            "general_notes": db.get_champion_note(conn, champion),
            "item_build": db.get_item_build(conn, champion),
            "matchups": matchups}


# (table, the column holding the text, the columns that identify a hit)
_SEARCHABLE = (
    ("coaching_sessions", "notes", ("id", "session_date", "title")),
    ("blocks", "learnings", ("id", "title")),
    ("block_games", "notes", ("id", "block_id", "match_id")),
    ("block_series", "goals", ("id", "title")),
    ("block_series", "closing_notes", ("id", "title")),
    ("matchup_notes", "notes", ("my_champion", "opp_champion")),
    ("champion_notes", "notes", ("champion",)),
    ("game_reflections", "note", ("match_id", "puuid")),
    ("research_entries", "notes", ("id", "player_name", "champion", "opp_champion")),
)


def _snippet(text, query):
    """The match in context, so a hit reads without a second lookup."""
    if len(text) <= SNIPPET_CHARS:
        return text
    found = text.lower().find(query.lower())
    start = max(0, found - SNIPPET_CHARS // 3)
    return ("…" if start else "") + text[start:start + SNIPPET_CHARS] + "…"


@tool("search_notes",
      "Free-text search across everything you have written: session notes, "
      "block learnings, per-game notes and reflections, series goals and "
      "retrospectives, matchup and champion notes, research entries. Returns "
      "the matching snippet plus where it came from.",
      _schema({"query": {"type": "string",
                         "description": "Text to look for (case-insensitive)."}},
              required=["query"]))
def _search_notes(conn, args):
    query = _required(args, "query")
    # LIKE's own wildcards are escaped: a model searching for "90%" or
    # "lane_adv" means those characters literally.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    hits = []
    for table, column, identity in _SEARCHABLE:
        sql = (f"SELECT {', '.join(identity)}, {column} AS _text FROM {table} "
               f"WHERE {column} LIKE ? ESCAPE '\\'")
        for row in conn.execute(sql, (like,)):
            hits.append({"source": f"{table}.{column}",
                         "where": {k: row[k] for k in identity},
                         "snippet": _snippet(row["_text"], query)})
            if len(hits) >= MAX_SEARCH_HITS:
                return {"query": query, "hits": hits, "truncated": True}
    return {"query": query, "hits": hits, "truncated": False}


@tool("get_review_queue",
      "Block games you played but never wrote anything about — the backlog "
      "worth reviewing next.",
      _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50,
                         "description": "How many games. Default 8."}}))
def _get_review_queue(conn, args):
    return stats.review_queue(conn, _tracked(conn),
                              limit=_int(args, "limit", 8, low=1, high=50))


# -------------------------------------------------------------- write tools

def _is_iso_date(value):
    parts = value.split("-")
    return (len(parts) == 3 and all(p.isdigit() for p in parts)
            and len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2)


def _is_patch(value):
    parts = value.split(".")
    return 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts)


@tool("add_coaching_session",
      "Record a coaching session. Its date anchors a period in the coaching-"
      "progress table, so the stats from then on are measured against it.",
      _schema({"session_date": {"type": "string", "description": "ISO date, "
                                "YYYY-MM-DD. One session per date."},
               "title": {"type": "string", "description": "What it was about."},
               "coach": {"type": "string"},
               "notes": {"type": "string", "description": "Markdown."}},
              required=["session_date"]),
      writes=True)
def _add_coaching_session(conn, args):
    date = _required(args, "session_date")
    if not _is_iso_date(date):
        raise McpError("session_date must be YYYY-MM-DD")
    if any(row["session_date"] == date for row in db.list_sessions(conn)):
        raise McpError(f"a session already exists for {date} — "
                       "use update_coaching_session to edit it")
    session_id = db.add_session(conn, date, title=_str(args, "title"),
                                notes=_str(args, "notes"), coach=_str(args, "coach"))
    return {"id": session_id, "session_date": date}


@tool("update_coaching_session",
      "Edit an existing coaching session. Only the fields you pass change, so "
      "writing notes never wipes the title.",
      _schema({"session_id": {"type": "integer"},
               "title": {"type": "string"},
               "coach": {"type": "string"},
               "notes": {"type": "string", "description": "Markdown. REPLACES the "
                         "stored notes — read them first if you mean to append."}},
              required=["session_id"]),
      writes=True)
def _update_coaching_session(conn, args):
    session_id = _required_int(args, "session_id")
    fields = {k: _str(args, k) for k in ("title", "coach", "notes") if k in args}
    if not fields:
        raise McpError("pass at least one of title, coach, notes")
    if not db.update_session(conn, session_id, **fields):
        raise McpError(f"no session with id {session_id}")
    return {"id": session_id, "updated": sorted(fields)}


@tool("update_matchup_notes",
      "Write the playbook entry for one matchup (your champion vs theirs). Only "
      "the fields you pass are written, so notes and the saved rune/skill build "
      "cannot clobber each other. `notes` REPLACES the stored text.",
      _schema({"champion": dict(_CHAMPION_PROP, description="Your champion (required)."),
               "opp_champion": {"type": "string",
                                "description": "The opponent (required)."},
               "notes": {"type": "string", "description": "Markdown. Replaces what is "
                         "stored — read it with get_playbook first to append."},
               "patch_version": {"type": "string",
                                 "description": "e.g. '16.14'. Blank clears it."}},
              required=["champion", "opp_champion"]),
      writes=True)
def _update_matchup_notes(conn, args):
    champion = _champion(args, "champion")
    opponent = _champion(args, "opp_champion")
    fields = {}
    if "notes" in args:
        fields["notes"] = _str(args, "notes")
    if "patch_version" in args:
        patch = _str(args, "patch_version")
        if patch and not _is_patch(patch):
            raise McpError("patch_version must look like 16.14 or 16.14.1")
        fields["patch_version"] = patch
    if not fields:
        raise McpError("pass notes and/or patch_version")
    db.set_matchup_note(conn, champion, opponent, **fields)
    return {"champion": champion, "opp_champion": opponent, "written": sorted(fields)}


@tool("set_game_reflection",
      "Tag and annotate one game from your own perspective — the quick post-"
      "match habit ('bad TP', 'int death', 'good vision'). match_id and puuid "
      "come from get_recent_games. Only the fields you pass change.",
      _schema({"match_id": {"type": "string"},
               "puuid": {"type": "string", "description": "The tracked player's "
                         "puuid (a game's `my_puuid`)."},
               "tags": {"type": "array", "items": {"type": "string"},
                        "description": "Freeform tags. Replaces the stored list."},
               "note": {"type": "string",
                        "description": "Markdown. Replaces the stored note."}},
              required=["match_id", "puuid"]),
      writes=True)
def _set_game_reflection(conn, args):
    match_id = _required(args, "match_id")
    puuid = _required(args, "puuid")
    if not conn.execute("SELECT 1 FROM participants WHERE match_id=? AND puuid=?",
                        (match_id, puuid)).fetchone():
        raise McpError(f"no stored game {match_id} for that puuid")
    fields = {}
    if "tags" in args:
        tags = args.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise McpError("tags must be a list of strings")
        fields["tags"] = [t.strip() for t in tags if t.strip()]
    if "note" in args:
        fields["note"] = _str(args, "note")
    if not fields:
        raise McpError("pass tags and/or note")
    db.set_reflection(conn, match_id, puuid, **fields)
    return {"match_id": match_id, "puuid": puuid,
            "reflection": db.get_reflection(conn, match_id, puuid)}


@tool("set_block_learnings",
      "Write a practice block's learnings — what that run of games taught you. "
      "REPLACES the stored text; read it with get_learnings first if you mean "
      "to add to it.",
      _schema({"block_id": {"type": "integer"},
               "learnings": {"type": "string", "description": "Markdown."}},
              required=["block_id", "learnings"]),
      writes=True)
def _set_block_learnings(conn, args):
    block_id = _required_int(args, "block_id")
    if not any(block["id"] == block_id for block in db.list_blocks(conn)):
        raise McpError(f"no block with id {block_id}")
    db.update_block(conn, block_id, learnings=_str(args, "learnings"))
    return {"block_id": block_id}


# ------------------------------------------------------------- the protocol

INSTRUCTIONS = (
    "Coach Potato is this player's League of Legends coaching database: crawled "
    "match history plus everything they have written down (coaching sessions, "
    "practice-block learnings, per-matchup playbooks, per-game reflections). "
    "Read before you write — the note-writing tools REPLACE stored text, so "
    "fetch the current value first when you mean to add to it.")


def call_tool(conn, name, arguments):
    """Run one tool. Raises McpError for anything the model should see and fix."""
    entry = _find(name)
    if not entry:
        raise McpError(f"unknown tool {name!r}")
    if not isinstance(arguments, dict):
        raise McpError("arguments must be an object")
    return entry["handler"](conn, arguments)


def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def tool_text(value):
    """Tool output as the single text block clients render and models read.
    JSON because every payload here is structured, and unindented so the
    model's context goes on data rather than whitespace."""
    return {"content": [{"type": "text",
                         "text": json.dumps(value, default=str, ensure_ascii=False)}],
            "isError": False}


def handle(conn, payload, version=None, scrub=None):
    """One JSON-RPC message -> the response dict, or None for a notification
    (which gets an empty 202 rather than a body).

    `scrub` optionally post-processes a tool's value — app.py passes the
    rank-redaction it already applies to every JSON response, so "hide my
    rank / LP" holds here too instead of leaking through a second door."""
    if not isinstance(payload, dict):
        return _error(None, -32600, "request must be a JSON object")
    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion") or ""
        return _result(request_id, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else LATEST_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": version or "0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": tool_list()})
    if method == "tools/call":
        try:
            value = call_tool(conn, params.get("name"), params.get("arguments") or {})
        except McpError as exc:
            return _result(request_id, {"content": [{"type": "text", "text": str(exc)}],
                                        "isError": True})
        return _result(request_id, tool_text(scrub(value) if scrub else value))
    if isinstance(method, str) and method.startswith("notifications/"):
        return None  # nothing to acknowledge; the caller answers 202
    if request_id is None:
        return None  # an unknown notification is still just a notification
    return _error(request_id, -32601, f"method {method!r} is not supported")
