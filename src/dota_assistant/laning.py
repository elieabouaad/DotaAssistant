"""Laning intelligence.

For most cores, the single biggest lever below the highest brackets is *not
falling behind in lane*. This module gives you a live, honest CS scoreboard
against two bars:

1. **Your hero's benchmark pace** — the median last-hits-per-minute for the hero,
   from OpenDota's /benchmarks endpoint (cached). Role-adjusts automatically
   (a hard support's bar is far lower than a mid's).
2. **Your own history** — your average CS@10 on the hero from stats.db. To climb
   you target the *higher* of the two, so you're always reaching.

Live, at 5:00 / 8:00 / 10:00 it compares your last-hit count to the expected
pace and speaks a quiet nudge only when you're behind (plus one bit of positive
reinforcement if you're ahead at ten). The dashboard shows the same read every
tick as a CS-vs-target line.

Deferred (needs Stratz's lane-phase data): a true "you win / lose *this lane*"
verdict at the draft. Whole-game OpenDota matchups aren't lane-specific, so we
don't fake that number — we give a deterministic laning *threat* advisory
instead (see laning_threats), reusing the itemization hero-tag table.
"""

from __future__ import annotations

import json
import time

import requests

from . import config
from .draft import API

ACTIVE = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"

# When to check in during the laning stage (seconds of game clock).
MILESTONES = (300, 480, 600)
# Only nag if you're this far under the expected pace (fraction).
BEHIND_RATIO = 0.85
# Fallback pace (last hits / min) if we have neither benchmark nor history.
DEFAULT_PACE = 4.5
# OpenDota's last_hits_per_min is a *whole-game* average — late-game jungle
# farming inflates it well above true lane pace. Scale it down so the CS@10
# target is realistic (a ~9 lh/min whole-game core is ~6 lh/min in the lane).
LANE_FACTOR = 0.7


# --------------------------------------------------------------------------- #
# Pace targets (OpenDota benchmark + personal history)
# --------------------------------------------------------------------------- #

def _benchmarks(hero_id: int) -> dict:
    """OpenDota per-minute benchmark percentiles for a hero. Cached 7 days.
    Kept out of draft._cached_get because the query string ('?') isn't a legal
    Windows filename."""
    cache = config.cache_dir() / f"benchmarks_{hero_id}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        return json.loads(cache.read_text())
    resp = requests.get(f"{API}/benchmarks", params={"hero_id": hero_id}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    cache.write_text(json.dumps(data))
    return data


def _percentile_value(series: list[dict], p: float) -> float | None:
    """Value at (nearest) percentile p from a [{percentile, value}] series."""
    if not series:
        return None
    best = min(series, key=lambda e: abs(e.get("percentile", 0) - p))
    return best.get("value")


def _benchmark_pace(hero_id: int) -> float | None:
    try:
        data = _benchmarks(hero_id)
        lhpm = _percentile_value(data.get("result", {}).get("last_hits_per_min", []), 0.5)
        return lhpm * LANE_FACTOR if lhpm else None
    except Exception:
        return None


def _personal_pace(hero_id: int) -> float | None:
    """Your average CS@10 on this hero (falling back to overall), as lh/min."""
    try:
        from . import stats  # deferred: stats is heavier than this one query needs
        conn = stats.connect()
        try:
            row = conn.execute(
                "SELECT AVG(cs_at_10) v FROM matches WHERE hero_id=? AND cs_at_10 IS NOT NULL",
                (hero_id,),
            ).fetchone()
            avg = row["v"] if row else None
            if avg is None:
                row = conn.execute(
                    "SELECT AVG(cs_at_10) v FROM matches WHERE cs_at_10 IS NOT NULL"
                ).fetchone()
                avg = row["v"] if row else None
        finally:
            conn.close()
        return avg / 10.0 if avg else None
    except Exception:
        return None


def target_pace(hero_id: int) -> float:
    """Last-hits-per-minute to aim for: the higher of the hero benchmark and
    your own average, so the bar always pushes you forward."""
    bench = _benchmark_pace(hero_id)
    mine = _personal_pace(hero_id)
    pace = max(p for p in (bench, mine) if p) if (bench or mine) else None
    return round(pace, 2) if pace else DEFAULT_PACE


# --------------------------------------------------------------------------- #
# Live lane coach
# --------------------------------------------------------------------------- #

class LaneCoach:
    """Speaks a CS check at lane milestones when you're behind pace, and always
    exposes the current read (last_read) for the dashboard."""

    def __init__(self, get_pace=target_pace):
        self.get_pace = get_pace
        self.enabled = True
        self._match: str | None = None
        self._fired: set[int] = set()
        self._pace: float | None = None
        self.last_read: dict = {}

    def _reset(self, match_id: str | None) -> None:
        self._match = match_id
        self._fired = set()
        self._pace = None
        self.last_read = {}

    def update(self, state: dict) -> list[str]:
        map_ = state.get("map", {})
        if map_.get("game_state") != ACTIVE or map_.get("paused"):
            return []
        hero = state.get("hero", {})
        player = state.get("player", {})

        mid = map_.get("matchid")
        if mid != self._match:
            self._reset(mid)

        hero_id = hero.get("id")
        if hero_id and self._pace is None:
            self._pace = self.get_pace(hero_id)

        clock = map_.get("clock_time") or 0
        lh = player.get("last_hits")

        # Dashboard read (updated every tick, independent of the spoken toggle).
        if self._pace and clock > 0 and lh is not None:
            self.last_read = {
                "clock": clock,
                "last_hits": lh,
                "pace": self._pace,
                "expected": round(self._pace * clock / 60),
                "on_track": lh >= round(self._pace * clock / 60) * BEHIND_RATIO,
            }

        if not self.enabled or not self._pace or lh is None or not hero.get("alive"):
            return []

        msgs: list[str] = []
        for ms in MILESTONES:
            # Fire once, only near the milestone (so a mid-game server restart
            # doesn't retroactively nag about a checkpoint we missed).
            if ms in self._fired or clock < ms or clock >= ms + 30:
                continue
            self._fired.add(ms)
            expected = round(self._pace * ms / 60)
            mmss = f"{ms // 60}:{ms % 60:02d}"
            if lh < expected * BEHIND_RATIO:
                msgs.append(
                    f"CS check: {lh} at {mmss}. Aim for about {expected}. "
                    f"Freeze the lane and focus last hits."
                )
            elif ms == 600 and lh >= expected:
                msgs.append(f"Good farm: {lh} creeps at ten minutes.")
        return msgs


# --------------------------------------------------------------------------- #
# Draft-time laning threat advisory (deterministic, reuses itemization tags)
# --------------------------------------------------------------------------- #

def laning_threats(enemy_ids: list[int], role: str) -> list[str]:
    """Short, honest laning warnings from the enemy draft — not a fake lane
    winrate. Uses the curated hero-threat tags."""
    from . import itemization  # deferred: breaks the laning <-> itemization cycle
    tagged = itemization._enemy_tagset(enemy_ids)
    magic = [n for n, t in tagged if "magic_burst" in t]
    physical = [n for n, t in tagged if "physical_carry" in t]
    harass = [n for n, t in tagged if {"silence", "hard_disable"} & t]

    tips: list[str] = []
    if len(magic) >= 2:
        tips.append(
            f"Respect early magic burst ({', '.join(magic[:3])}) — buy a Magic Stick/Wand "
            f"and don't sit in nuke range at low HP."
        )
    if physical and role in ("carry", "offlane"):
        tips.append(
            f"Physical laners ({', '.join(physical[:3])}) — early armor (Ring/branches, "
            f"Bracer/Stout) keeps your last-hits safe."
        )
    if harass:
        tips.append(
            f"Lockdown in lane ({', '.join(harass[:3])}) — hold your TP and don't get "
            f"caught pushed up."
        )
    return tips
