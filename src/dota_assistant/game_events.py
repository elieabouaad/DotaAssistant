"""Both-team event intelligence.

GSI's `events` array carries a live feed for *both* teams — not just your own
hero — which the rest of the app treats as own-hero-only. This engine turns the
three highest-value signals in that feed into spoken tactical alerts:

  1. Enemy smoke      (CHAT_MESSAGE_SMOKE_ACTIVATED) -> gank warning
  2. Roshan contested (CHAT_MESSAGE_ROSHAN_ROAR)     -> someone is hitting Rosh
  3. Teamfight swing  (CHAT_MESSAGE_HERO_KILL)       -> man-advantage window

Player-id convention: ids 0-4 are Radiant, 5-9 are Dire, so every event with a
playerid can be attributed to a team relative to your own (`player.team_name`).

Fed the full GSI state each tick like the other engines; `update()` returns the
announcements that are newly due. GSI repeats each event across many payloads,
so we dedupe by content, and reset that state (plus the kill window) whenever
the match changes or the clock rewinds.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field

# A pickup counts toward a fight swing only if the kills land close together.
FIGHT_WINDOW = 15          # seconds
# Don't re-announce the same swing / roar / smoke more often than this.
FIGHT_COOLDOWN = 30
ROAR_COOLDOWN = 25
SMOKE_COOLDOWN = 8
ACTIVITY_COOLDOWN = 12     # glyph / scan / buyback callouts
# How recently (seconds) an enemy action still counts as "current" for the
# coach context passed to the LLM.
RECENT = 40

# Dota's internal team ids, used by CHAT_MESSAGE_SCAN_USED's `value` field.
TEAM_NUM = {"radiant": 2, "dire": 3}


def team_of(pid) -> str | None:
    """Radiant = player ids 0-4, Dire = 5-9. None for 'no player' (-1)."""
    if pid is None or pid < 0 or pid > 9:
        return None
    return "radiant" if pid <= 4 else "dire"


def own_player_id(state: dict) -> int | None:
    """Your global player id (0-9). Derived from team + team_slot, and matches
    the id used in the event feed (verified against items[].purchaser)."""
    p = state.get("player", {})
    ts, tn = p.get("team_slot"), p.get("team_name")
    if ts is None or tn not in ("radiant", "dire"):
        return None
    return ts + (5 if tn == "dire" else 0)


def hero_short(state: dict) -> str:
    name = state.get("hero", {}).get("name", "") or ""
    return name.removeprefix("npc_dota_hero_").replace("_", " ") if name else ""


def slot_label(pid, my_id, my_hero: str = "") -> str:
    """Human label for a player id: your hero's name for you, else team+slot
    (GSI never exposes the other nine heroes' names in a normal pub)."""
    if pid == my_id and my_hero:
        return my_hero
    if pid is None or pid < 0 or pid > 9:
        return "?"
    return f"{'Radiant' if pid <= 4 else 'Dire'} {pid % 5 + 1}"


@dataclass
class GameEventEngine:
    smoke_enabled: bool = True
    roshan_contest_enabled: bool = True
    teamfight_enabled: bool = True
    enemy_activity_enabled: bool = True  # glyph / scan / buyback

    _seen: set = field(default_factory=set)
    _match_id: str | None = None
    _last_clock: int | None = None
    # Per-player [kills, deaths] rebuilt from the kill feed, for the scoreboard.
    _score: dict = field(default_factory=dict)
    # Stable per-kill ids (killer, victim, kill_time) so a kill counts once even
    # though GSI rebroadcasts it across many payloads.
    _kill_ids: set = field(default_factory=set)
    # (game_time, victim_team) for recent hero kills, for the swing window.
    _kills: deque = field(default_factory=lambda: deque())
    _last_fight: int | None = None
    _last_roar: int | None = None
    _last_smoke: int | None = None
    _last_activity: int | None = None
    # game_time of the last enemy action of each kind, for the coach context.
    _seen_smoke_t: int | None = None
    _seen_scan_t: int | None = None
    _seen_buyback_t: int | None = None

    def update(self, state: dict) -> list[str]:
        map_ = state.get("map", {})
        my_team = state.get("player", {}).get("team_name")
        if not my_team:
            return []
        enemy = "dire" if my_team == "radiant" else "radiant"

        self._maybe_reset(map_)

        out: list[str] = []
        for e in state.get("events", []):
            key = (e.get("game_time"), e.get("event_type"), e.get("data", ""))
            if key in self._seen:
                continue
            self._seen.add(key)
            msg = self._classify(e, my_team, enemy)
            if msg:
                out.append(msg)

        if len(self._seen) > 20000:  # same hygiene bound as the server feed
            self._seen.clear()
        return out

    # --- reset on new match / clock rewind ---------------------------------

    def _maybe_reset(self, map_: dict) -> None:
        match_id = map_.get("matchid")
        clock = map_.get("clock_time")
        rewound = (
            self._last_clock is not None
            and clock is not None
            and clock < self._last_clock - 5
        )
        if (match_id and match_id != self._match_id) or rewound:
            self._seen.clear()
            self._kills.clear()
            self._score.clear()
            self._kill_ids.clear()
            self._last_fight = self._last_roar = self._last_smoke = None
            self._last_activity = None
            self._seen_smoke_t = self._seen_scan_t = self._seen_buyback_t = None
        self._match_id = match_id or self._match_id
        if clock is not None:
            self._last_clock = clock

    # --- per-event classification ------------------------------------------

    def _classify(self, e: dict, my_team: str, enemy: str) -> str | None:
        if e.get("event_type") != "generic_event":
            return None
        try:
            data = json.loads(e.get("data", "{}"))
        except (json.JSONDecodeError, TypeError):
            return None

        mtype = data.get("type", "")
        t = e.get("game_time", 0)

        if mtype == "CHAT_MESSAGE_SMOKE_ACTIVATED":
            return self._smoke(data, my_team, enemy, t)
        if mtype == "CHAT_MESSAGE_ROSHAN_ROAR":
            return self._roar(t)
        if mtype == "CHAT_MESSAGE_HERO_KILL":
            return self._hero_kill(data, my_team, enemy, t)
        if mtype == "CHAT_MESSAGE_GLYPH_USED":
            return self._glyph(data, enemy, t)
        if mtype == "CHAT_MESSAGE_SCAN_USED":
            return self._scan(data, enemy, t)
        if mtype == "CHAT_MESSAGE_BUYBACK":
            return self._buyback(data, enemy, t)
        return None

    def _smoke(self, data: dict, my_team: str, enemy: str, t: int) -> str | None:
        # Only the enemy smoking matters — you already know when you smoke.
        if team_of(data.get("playerid1")) != enemy:
            return None
        self._seen_smoke_t = t  # recorded for the coach context even if muted
        if not self.smoke_enabled:
            return None
        if self._last_smoke is not None and t - self._last_smoke < SMOKE_COOLDOWN:
            return None
        self._last_smoke = t
        return "Enemy smoke — someone is moving, watch the map"

    def _roar(self, t: int) -> str | None:
        if not self.roshan_contest_enabled:
            return None
        if self._last_roar is not None and t - self._last_roar < ROAR_COOLDOWN:
            return None
        self._last_roar = t
        return "Roshan is being hit — contest it or get vision"

    def _hero_kill(self, data: dict, my_team: str, enemy: str, t: int) -> str | None:
        killer, victim = data.get("playerid1"), data.get("playerid2")
        # A kill is rebroadcast across many payloads; count it once via its
        # stable (killer, victim, kill_time) id.
        kid = (killer, victim, round(data.get("time", 0), 2))
        if kid in self._kill_ids:
            return None
        self._kill_ids.add(kid)
        if 0 <= (killer if killer is not None else -1) <= 9:
            self._score.setdefault(killer, [0, 0])[0] += 1
        if 0 <= (victim if victim is not None else -1) <= 9:
            self._score.setdefault(victim, [0, 0])[1] += 1

        vteam = team_of(victim)
        if vteam is None:
            return None
        self._kills.append((t, vteam))
        while self._kills and t - self._kills[0][0] > FIGHT_WINDOW:
            self._kills.popleft()

        if not self.teamfight_enabled:
            return None
        enemy_dead = sum(1 for _, vt in self._kills if vt == enemy)
        ally_dead = sum(1 for _, vt in self._kills if vt == my_team)
        net = enemy_dead - ally_dead
        if abs(net) < 2:
            return None
        if self._last_fight is not None and t - self._last_fight < FIGHT_COOLDOWN:
            return None
        self._last_fight = t

        if net >= 3:
            return f"You're {net} up — take Roshan or push high ground"
        if net == 2:
            return "Two enemies down — good window for Roshan or a tower"
        # net <= -2: your side lost the exchange.
        return "Enemies got a pickup — play safe, skip objectives for now"

    # --- enemy activity: glyph / scan / buyback ----------------------------

    def _activity_ok(self, t: int) -> bool:
        if not self.enemy_activity_enabled:
            return False
        if self._last_activity is not None and t - self._last_activity < ACTIVITY_COOLDOWN:
            return False
        self._last_activity = t
        return True

    def _glyph(self, data: dict, enemy: str, t: int) -> str | None:
        # Only enemy glyph matters: their fortification is now on a ~5 min
        # cooldown, so their next tower has no glyph to save it.
        if team_of(data.get("playerid1")) != enemy or not self._activity_ok(t):
            return None
        return "Enemy glyph used — it's down for five minutes, push a tower"

    def _scan(self, data: dict, enemy: str, t: int) -> str | None:
        # SCAN_USED carries no playerid; `value` is the scanning team's id.
        if data.get("value") != TEAM_NUM.get(enemy):
            return None
        self._seen_scan_t = t
        if not self._activity_ok(t):
            return None
        return "Enemy scan — they're checking Roshan or looking for you"

    def _buyback(self, data: dict, enemy: str, t: int) -> str | None:
        if team_of(data.get("playerid1")) != enemy:
            return None
        self._seen_buyback_t = t
        if not self._activity_ok(t):
            return None
        return "Enemy bought back"

    # --- tactical context for the coach ------------------------------------

    def context(self, state: dict) -> dict:
        """A compact read of the current tactical situation, for the LLM coach.
        Everything is derived from the both-team event feed (things GSI doesn't
        put in your own hero state)."""
        clock = state.get("map", {}).get("clock_time")
        my_team = state.get("player", {}).get("team_name")
        enemy = "dire" if my_team == "radiant" else "radiant"

        def recent(ts):
            return ts is not None and clock is not None and 0 <= clock - ts <= RECENT

        enemy_dead = sum(1 for _, vt in self._kills if vt == enemy)
        ally_dead = sum(1 for _, vt in self._kills if vt == my_team)
        return {
            "fight_net_kills": enemy_dead - ally_dead,  # +ve = you're ahead
            "enemy_smoke_recent": recent(self._seen_smoke_t),
            "enemy_scan_recent": recent(self._seen_scan_t),
            "enemy_buyback_recent": recent(self._seen_buyback_t),
        }

    def scoreboard(self, state: dict) -> list[dict]:
        """All ten players' Kills/Deaths, rebuilt from the kill feed. Assists
        aren't in the feed, so only your own row carries a real assist count
        (from your authoritative hero data); others are left null. Only your
        hero can be named — the rest are labelled by team + slot."""
        my_id = own_player_id(state)
        my_hero = hero_short(state)
        my_assists = state.get("player", {}).get("assists")
        rows = []
        for pid in range(10):
            k, d = self._score.get(pid, [0, 0])
            rows.append({
                "id": pid,
                "team": "radiant" if pid <= 4 else "dire",
                "slot": pid % 5 + 1,
                "label": slot_label(pid, my_id, my_hero),
                "you": pid == my_id,
                "k": k,
                "d": d,
                "a": my_assists if pid == my_id else None,
            })
        return rows
