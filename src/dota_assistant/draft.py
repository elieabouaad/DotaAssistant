"""Phase 3: draft assistant backed by OpenDota.

Counter-pick logic: for each candidate hero, average the enemy heroes'
head-to-head winrates against it (from /heroes/{id}/matchups). A candidate
scores well when the enemy picks all underperform against it. Score is
expressed as advantage percentage points vs. a 50/50 matchup.

If [player] account_id is set in config.toml, the candidate list is annotated
with your own games/winrate on each hero so you can filter to your pool.
OpenDota only knows your matches if "Expose Public Match Data" is enabled in
the Dota client settings.

All OpenDota responses are cached on disk (heroes 7 days, matchups 1 day,
player heroes 1 hour) to stay far below the free rate limit.
"""

import json
import time

import requests

from . import config

API = "https://api.opendota.com/api"

MIN_GAMES_PER_MATCHUP = 30  # ignore noisy low-sample matchups


def _cached_get(path: str, ttl_seconds: int):
    cache_file = config.cache_dir() / (path.strip("/").replace("/", "_") + ".json")
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl_seconds:
        return json.loads(cache_file.read_text())
    resp = requests.get(f"{API}{path}", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    cache_file.write_text(json.dumps(data))
    return data


def heroes() -> dict[int, dict]:
    """id -> {id, name, localized_name, ...}, cached 7 days."""
    return {h["id"]: h for h in _cached_get("/heroes", 7 * 86400)}


def matchups(hero_id: int) -> dict[int, dict]:
    """hero_id -> {games_played, wins} of `hero_id` VS each other hero, 1 day."""
    return {m["hero_id"]: m for m in _cached_get(f"/heroes/{hero_id}/matchups", 86400)}


def player_heroes(account_id: int) -> dict[int, dict]:
    """hero_id -> {games, win} for a player, cached 1 hour."""
    data = _cached_get(f"/players/{account_id}/heroes", 3600)
    return {int(h["hero_id"]): h for h in data}


def hero_stats() -> list[dict]:
    """Per-hero pick/win counts split by rank tier (1=Herald … 8=Immortal),
    cached 1 day. Returns [] if OpenDota's heroStats is unavailable."""
    try:
        return _cached_get("/heroStats", 86400)
    except Exception:
        return []


# Rank-tier groupings for bracket-aware advice (OpenDota tiers 1..8).
BRACKETS = {
    "herald-guardian": ([1, 2], "Herald–Guardian"),
    "crusader-archon": ([3, 4], "Crusader–Archon"),
    "legend-ancient": ([5, 6], "Legend–Ancient"),
    "divine-immortal": ([7, 8], "Divine–Immortal"),
    "all": ([1, 2, 3, 4, 5, 6, 7, 8], "all brackets"),
}
# How much a hero being generally strong in the bracket nudges its counter
# score. Kept small so a hard counter still outranks a merely-popular hero.
BRACKET_WEIGHT = 0.25


def bracket_winrates(tiers: list[int]) -> dict[int, float]:
    """hero_id -> win% across the given rank tiers, from heroStats. Empty if
    heroStats is down."""
    out: dict[int, float] = {}
    for h in hero_stats():
        picks = sum(h.get(f"{t}_pick", 0) or 0 for t in tiers)
        wins = sum(h.get(f"{t}_win", 0) or 0 for t in tiers)
        if picks:
            out[h["id"]] = round(100 * wins / picks, 1)
    return out


# OpenDota role tags that stand in for "can play a core / a support slot".
_ROLE_TAG = {"core": "Carry", "support": "Support"}


def _why(hero: str, edges: list[dict], bracket_wr: float | None,
         bracket_label: str, my_games: int, my_wr: float | None) -> str:
    """Human explanation of a suggestion, assembled from the signals we have."""
    parts = []
    best = sorted(edges, key=lambda e: e["advantage"], reverse=True)[:2]
    strong = [f"{e['enemy']} (+{e['advantage']:.0f}%)" for e in best if e["advantage"] > 0]
    if strong:
        parts.append("Strong vs " + ", ".join(strong))
    if bracket_wr is not None:
        verdict = "top-tier" if bracket_wr >= 52 else "solid" if bracket_wr >= 49 else "weak"
        parts.append(f"{bracket_wr:.0f}% WR at {bracket_label} ({verdict})")
    if my_games:
        parts.append(f"you: {my_wr:.0f}% over {my_games}g" if my_wr is not None
                     else f"you: {my_games}g")
    return "; ".join(parts) or "Counter by matchup data."


def counter_picks(enemy_ids: list[int], account_id: int | None = None,
                  role: str | None = None, bracket: str = "all",
                  prioritize_pool: bool = False, top: int = 25) -> list[dict]:
    """Rank heroes by how badly the enemy lineup performs into them, blended with
    how strong each hero is *in your bracket*, filtered by role, and (optionally)
    anchored to heroes you actually play. Every candidate carries a `why`.

    - `role`:  'core' | 'support' | None (any) — via OpenDota role tags.
    - `bracket`: a key of draft.BRACKETS; blends that bracket's win rate in.
    - `prioritize_pool`: keep only heroes you've played (needs account_id).
    """
    all_heroes = heroes()
    enemy_matchups = {eid: matchups(eid) for eid in enemy_ids}
    tiers, bracket_label = BRACKETS.get(bracket, BRACKETS["all"])
    bwr = bracket_winrates(tiers)
    role_tag = _ROLE_TAG.get(role or "")
    mine = {}
    if account_id:
        try:
            mine = player_heroes(account_id)
        except Exception:
            mine = {}

    results = []
    for cand_id, hero in all_heroes.items():
        if cand_id in enemy_ids:
            continue
        if role_tag and role_tag not in hero.get("roles", []):
            continue
        me = mine.get(cand_id)
        my_games = me["games"] if me else 0
        if prioritize_pool and not my_games:
            continue

        edges = []
        for eid, mus in enemy_matchups.items():
            mu = mus.get(cand_id)
            if not mu or mu["games_played"] < MIN_GAMES_PER_MATCHUP:
                continue
            enemy_wr = mu["wins"] / mu["games_played"]
            edges.append({
                "enemy_id": eid,
                "enemy": all_heroes[eid]["localized_name"],
                "advantage": round((0.5 - enemy_wr) * 100, 2),
                "games": mu["games_played"],
            })
        if len(edges) < len(enemy_ids):  # require data vs every enemy pick
            continue

        counter = sum(e["advantage"] for e in edges) / len(edges)
        cand_bwr = bwr.get(cand_id)
        my_wr = round(100 * me["win"] / me["games"], 1) if me and me["games"] else None
        # Counter advantage is primary; bracket strength nudges ties; your own
        # win rate on the hero gives a gentle personal boost when you have games.
        score = counter + (BRACKET_WEIGHT * (cand_bwr - 50) if cand_bwr is not None else 0)
        if my_wr is not None and my_games >= 3:
            score += 0.1 * (my_wr - 50)

        results.append({
            "hero_id": cand_id,
            "hero": hero["localized_name"],
            "score": round(score, 2),
            "counter": round(counter, 2),
            "bracket_wr": cand_bwr,
            "my_games": my_games,
            "my_winrate": my_wr,
            "vs": sorted(edges, key=lambda e: e["advantage"]),
            "why": _why(hero["localized_name"], edges, cand_bwr, bracket_label, my_games, my_wr),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top]
