"""User-editable runtime settings, persisted as a small JSON overlay.

Kept separate from timings.toml (which holds the patch defaults and lots of
comments we don't want to rewrite): this file only stores the *overrides* the
user makes from the dashboard Settings page, e.g. which announcements are on.

Shape:
    {"announce": {"<event key>": true/false, ...},
     "overlay":  {"offset_x": 24, "offset_y": 150, ...}}

Missing keys fall back to each event's own default, so deleting settings.json
simply restores defaults.
"""

from __future__ import annotations

import json

from . import config


def load() -> dict:
    try:
        return json.loads(config.settings_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    config.settings_path().write_text(json.dumps(data, indent=2))


def announce_overrides() -> dict:
    """Map of {event key -> enabled} the user has explicitly set."""
    ov = load().get("announce", {})
    return {k: bool(v) for k, v in ov.items()} if isinstance(ov, dict) else {}


def set_announce(key: str, enabled: bool) -> None:
    data = load()
    data.setdefault("announce", {})[key] = bool(enabled)
    save(data)


def overlay_overrides() -> dict:
    """Overlay settings the user has changed at runtime — currently just the
    position, written by `dota-assistant overlay --setup`. Overrides config.toml."""
    ov = load().get("overlay", {})
    return dict(ov) if isinstance(ov, dict) else {}


def set_overlay(values: dict) -> None:
    data = load()
    data.setdefault("overlay", {}).update(values)
    save(data)
