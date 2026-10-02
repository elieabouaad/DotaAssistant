"""Single source of configuration and path resolution.

Configuration is read from `config.toml` in the data directory, deep-merged
over the DEFAULTS below (which mirror config.example.toml — keep them in
sync; tests/test_config.py checks the example file's keys are a subset).

The data directory is where everything writable lives — config.toml,
settings.json, stats.db, cache/, logs/. It resolves to $DOTA_ASSISTANT_HOME
when set, else the current working directory, so you run `dota-assistant`
from a folder you own and your data accumulates there.

Read-only files shipped inside the package (timings.toml, talents.json,
map_coords.json, the GSI config template, dashboard.html) are accessed via
the resource helpers, so they work from an installed wheel as well as a
source checkout.
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from functools import lru_cache
from importlib import resources
from pathlib import Path

DEFAULT_MODEL = "claude-opus-4-8"

DEFAULTS: dict = {
    "player": {
        "account_id": 0,
        "bracket": "",   # a key of draft.BRACKETS, e.g. "crusader-archon"
        "roles": [],     # e.g. ["pos 1 carry", "pos 3 offlane"]
        "focus": [],     # e.g. ["laning", "itemization"]
    },
    "server": {
        "host": "127.0.0.1",
        "port": 53100,
    },
    "logging": {
        "session_logs": True,
        "max_mb": 64,
        "keep_sessions": 20,
    },
    "coach": {
        "enabled": False,
        "provider": "auto",
        "model": DEFAULT_MODEL,
        "min_interval": 120,
    },
    "overlay": {
        "anchor": "top-right",
        "offset_x": 24,
        "offset_y": 150,
        "width": 250,
        "font": "Consolas",
        "font_size": 12,
        "timers": 3,
        "opacity": 0.95,
        "hotkey": "f9",
        "lane_until": 900,
    },
    "discord": {
        "enabled": False,
        "bot_token": "",  # or the DISCORD_BOT_TOKEN env var (preferred)
        "voice_channel_id": 0,
        "voice": "en-US-GuyNeural",
    },
    "enrich": {
        "on_launch": True,
        "max_age_days": 90,
        "max_matches": 100,
        "batch_size": 10,
        "poll_timeout_s": 240,
        "poll_interval_s": 6,
        "request_delay_s": 1.5,
    },
}


# --------------------------------------------------------------------------- #
# writable paths (all under the data directory)
# --------------------------------------------------------------------------- #

def data_dir() -> Path:
    env = os.environ.get("DOTA_ASSISTANT_HOME")
    return Path(env) if env else Path.cwd()


def cache_dir() -> Path:
    p = data_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "stats.db"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def config_path() -> Path:
    return data_dir() / "config.toml"


# --------------------------------------------------------------------------- #
# config loading
# --------------------------------------------------------------------------- #

def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache(maxsize=1)
def load() -> dict:
    """Full config: DEFAULTS <- config.toml <- env overrides. Cached — tests
    that change DOTA_ASSISTANT_HOME or env vars call load.cache_clear()."""
    cfg = copy.deepcopy(DEFAULTS)
    path = config_path()
    if path.exists():
        cfg = _deep_merge(cfg, tomllib.loads(path.read_text(encoding="utf-8")))
    port = os.environ.get("DOTA_ASSISTANT_PORT")
    if port and port.isdigit():
        cfg["server"]["port"] = int(port)
    # DISCORD_BOT_TOKEN is honoured by DiscordAnnouncer itself, not here, so
    # the token never sits in the merged config dict.
    return cfg


def section(name: str) -> dict:
    return dict(load().get(name, {}))


# --------------------------------------------------------------------------- #
# packaged read-only resources
# --------------------------------------------------------------------------- #

def resource(name: str):
    """Traversable for a file in dota_assistant/data (has read_text/open)."""
    return resources.files("dota_assistant") / "data" / name


def read_resource_text(name: str) -> str:
    return resource(name).read_text(encoding="utf-8")


def read_resource_json(name: str, default):
    try:
        return json.loads(read_resource_text(name))
    except (OSError, json.JSONDecodeError):
        return default


def read_web(name: str) -> str:
    """A file from dota_assistant/web (the dashboard)."""
    return (resources.files("dota_assistant") / "web" / name).read_text(encoding="utf-8")
