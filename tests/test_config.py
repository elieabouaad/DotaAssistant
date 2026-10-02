"""The config loader: defaults, config.toml merging, env overrides, and the
DEFAULTS <-> config.example.toml sync rule."""

import tomllib
from pathlib import Path

import pytest

from dota_assistant import config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point the data dir at an empty temp dir and reset the load cache."""
    monkeypatch.setenv("DOTA_ASSISTANT_HOME", str(tmp_path))
    config.load.cache_clear()
    yield tmp_path
    config.load.cache_clear()


def test_defaults_without_config_file(isolated_home):
    cfg = config.load()
    assert cfg["server"]["port"] == 53100
    assert cfg["player"]["account_id"] == 0
    assert cfg["player"]["bracket"] == ""
    assert cfg["coach"]["enabled"] is False
    assert cfg["logging"]["session_logs"] is True


def test_config_toml_merges_over_defaults(isolated_home):
    (isolated_home / "config.toml").write_text(
        '[player]\naccount_id = 42\n\n[server]\nport = 60000\n', encoding="utf-8")
    config.load.cache_clear()
    cfg = config.load()
    assert cfg["player"]["account_id"] == 42
    assert cfg["server"]["port"] == 60000
    # untouched sections keep their defaults
    assert cfg["overlay"]["anchor"] == "top-right"


def test_port_env_override(isolated_home, monkeypatch):
    monkeypatch.setenv("DOTA_ASSISTANT_PORT", "54321")
    config.load.cache_clear()
    assert config.load()["server"]["port"] == 54321


def test_writable_paths_live_in_data_dir(isolated_home):
    assert config.db_path() == isolated_home / "stats.db"
    assert config.cache_dir() == isolated_home / "cache"
    assert config.cache_dir().is_dir()  # created on demand
    assert config.log_dir() == isolated_home / "logs"


def test_resources_readable():
    assert "events" in config.read_resource_text("timings.toml")
    assert '"uri"' in config.read_resource_text("gamestate_integration_assistant.cfg")
    assert config.read_resource_json("map_coords.json", {})  # non-empty
    assert "<title>" in config.read_web("dashboard.html").lower()


def _keys(d: dict, prefix=()) -> set[tuple]:
    out = set()
    for k, v in d.items():
        out.add(prefix + (k,))
        if isinstance(v, dict):
            out |= _keys(v, prefix + (k,))
    return out


def test_example_file_keys_are_subset_of_defaults():
    """config.example.toml and config.DEFAULTS must stay in sync — every key a
    user can copy from the example must have a default (see CONTRIBUTING.md)."""
    example = Path(__file__).parent.parent / "config.example.toml"
    if not example.exists():  # installed-package test runs have no repo root
        pytest.skip("config.example.toml not present")
    parsed = tomllib.loads(example.read_text(encoding="utf-8"))
    assert _keys(parsed) <= _keys(config.DEFAULTS)
