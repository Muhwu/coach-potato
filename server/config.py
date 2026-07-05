"""Project configuration: runtime settings live in the db (Settings view),
with a .env fallback for the dev workflow. `load_config` remains the
.env-only path used by the crawl.py CLI."""
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .riot_client import PLATFORM_ROUTING

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Where resolve_settings looks for a fallback .env (tests point this elsewhere;
# frozen bundles get the PyInstaller temp dir, which never contains one).
ENV_FALLBACK_ROOT = PROJECT_ROOT

APP_DIR_NAME = "CoachPotato"


def default_db_path() -> Path:
    env = os.environ.get("LOL_DB_PATH")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):  # packaged desktop build
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_DIR_NAME
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
        else:
            base = Path(os.environ.get("XDG_DATA_HOME",
                                       str(Path.home() / ".local" / "share"))) / "coach-potato"
        return base / "lol.sqlite"
    return PROJECT_ROOT / "data" / "lol.sqlite"


def resolve_settings(conn) -> dict:
    """Effective runtime settings: db values win, .env fills the gaps.
    Never persists anything — PUT /api/settings is the only writer."""
    from . import db as _db

    stored = _db.get_settings(conn)
    env = {}
    env_path = ENV_FALLBACK_ROOT / ".env"
    if env_path.exists():
        env = parse_env_file(env_path)
    api_key = stored.get("riot_api_key") or env.get("RIOT_API_KEY") or ""
    if stored.get("accounts"):
        accounts = json.loads(stored["accounts"])
    elif env.get("ACCOUNTS"):
        accounts = [f"{n}#{t}" for n, t in parse_accounts(env["ACCOUNTS"])]
    else:
        accounts = []
    platform = (stored.get("platform") or env.get("PLATFORM") or "euw1").lower()
    configured = bool(api_key and accounts)
    source = None
    if configured:
        source = "db" if stored.get("riot_api_key") else "env"
    return {"riot_api_key": api_key, "accounts": accounts, "platform": platform,
            "configured": configured, "source": source}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    riot_api_key: str
    db_path: Path
    accounts: list
    platform: str = "euw1"


def parse_env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def parse_accounts(raw: str) -> list:
    accounts = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "#" not in part:
            raise ConfigError(f"Account {part!r} must be in Name#TAG format")
        name, _, tag = part.partition("#")
        accounts.append((name.strip(), tag.strip()))
    return accounts


def load_config(root: Path | None = None) -> Config:
    root = Path(root) if root else PROJECT_ROOT
    env_path = root / ".env"
    if not env_path.exists():
        raise ConfigError(
            f"No .env file at {env_path}. Run setup.sh or create one with RIOT_API_KEY=..."
        )
    env = parse_env_file(env_path)
    api_key = env.get("RIOT_API_KEY", "")
    if not api_key:
        raise ConfigError(f"RIOT_API_KEY missing from {env_path}")
    if not env.get("ACCOUNTS"):
        raise ConfigError(
            f"ACCOUNTS missing from {env_path}. "
            'Set it to a comma-separated list of Riot IDs, e.g. ACCOUNTS=Name#EUW, Other#EUW'
        )
    accounts = parse_accounts(env["ACCOUNTS"])
    platform = (env.get("PLATFORM") or "euw1").lower()
    if platform not in PLATFORM_ROUTING:
        raise ConfigError(
            f"Unknown PLATFORM {platform!r} in {env_path}. "
            f"Valid: {', '.join(sorted(PLATFORM_ROUTING))}"
        )
    db_path = Path(env["DB_PATH"]) if env.get("DB_PATH") else root / "data" / "lol.sqlite"
    return Config(riot_api_key=api_key, db_path=db_path, accounts=accounts,
                  platform=platform)
