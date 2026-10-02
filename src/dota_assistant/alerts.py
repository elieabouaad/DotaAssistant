"""Phase 2: state-aware alerts driven by your own hero's GSI data.

Rules (all parse GSI defensively — fields vary slightly by patch):
  - No-TP reminder: teleport slot empty after the laning stage starts
  - Death: announces respawn time and whether buyback is affordable
  - Death snapshots: full game state appended to logs/deaths_<ts>.jsonl
    for post-game review (Phase 4 feeds on these)
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ACTIVE = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"

NO_TP_START = 60        # don't nag before 1:00
NO_TP_COOLDOWN = 90     # seconds between repeated no-TP reminders

# Building-defense: GSI only exposes *your own* team's buildings, so this is a
# defend siren, not a push tracker. Re-arm each building's alert only after it
# recovers above this fraction (glyph/repair), so one push = one callout.
DEFENSE_COOLDOWN = 20
DEFENSE_REARM = 0.85
# An ult only earns a "ready" callout if its full cooldown is at least this
# long — no point announcing a 6-second ultimate coming back.
ULT_MIN_COOLDOWN = 40


def _lane(key: str) -> str:
    for lane in ("top", "mid", "bot"):
        if key.endswith(lane):
            return lane
    return ""


def _building_label(key: str) -> str | None:
    """Human name for one of your buildings, or None to ignore it (T1/T2 lose
    HP constantly to creeps — only later structures are worth a siren)."""
    lane = _lane(key)
    if "fort" in key:
        return "Your throne is under attack"
    if "rax_melee" in key:
        return f"Barracks under attack {lane}".strip()
    if "rax_range" in key:
        return f"Barracks under attack {lane}".strip()
    if "tower4" in key:
        return "Base towers under attack"
    if "tower3" in key:
        return f"Tier three {lane} under attack".strip()
    return None


@dataclass
class AlertEngine:
    deaths_log: Path
    prev_alive: bool | None = None
    last_no_tp: float = 0.0
    death_count: int = 0
    # Announcement toggles (set from the Settings page). Death snapshots are
    # always logged for post-game stats regardless of the spoken-recap toggle.
    no_tp_enabled: bool = True
    death_recap_enabled: bool = True
    defense_enabled: bool = True
    ult_enabled: bool = True
    # Per-building: True while its alert is "spent" (waiting to recover before
    # it can fire again). {building_key: {"prev": hp, "armed": bool, "last": t}}
    _buildings: dict = field(default_factory=dict)
    _prev_ult_cd: dict = field(default_factory=dict)  # ability name -> cooldown
    _last_match: str | None = None
    _extra: dict = field(default_factory=dict)

    def update(self, state: dict) -> list[str]:
        map_ = state.get("map", {})
        hero = state.get("hero", {})
        if map_.get("game_state") != ACTIVE or map_.get("paused") or not hero:
            return []

        self._maybe_reset(map_)
        msgs: list[str] = []
        msgs += self._check_tp(state)
        msgs += self._check_death(state)
        msgs += self._check_buildings(state)
        msgs += self._check_ult(state)
        return msgs

    def _maybe_reset(self, map_: dict) -> None:
        match_id = map_.get("matchid")
        if match_id and match_id != self._last_match:
            self._buildings.clear()
            self._prev_ult_cd.clear()
        self._last_match = match_id or self._last_match

    # --- building defense ----------------------------------------------------

    def _check_buildings(self, state: dict) -> list[str]:
        if not self.defense_enabled:
            return []
        clock = state.get("map", {}).get("clock_time") or 0
        buildings = state.get("buildings") or {}
        msgs: list[str] = []
        for team_blds in buildings.values():
            if not isinstance(team_blds, dict):
                continue
            for key, info in team_blds.items():
                label = _building_label(key)
                if not label or not isinstance(info, dict):
                    continue
                hp, mx = info.get("health"), info.get("max_health") or 1
                if hp is None:
                    continue
                st = self._buildings.setdefault(key, {"prev": hp, "armed": True, "last": -999})
                took_damage = hp < st["prev"]
                if took_damage and st["armed"] and clock - st["last"] >= DEFENSE_COOLDOWN:
                    msgs.append(label)
                    st["armed"] = False
                    st["last"] = clock
                if hp >= mx * DEFENSE_REARM:  # recovered → can warn again
                    st["armed"] = True
                st["prev"] = hp
        return msgs

    # --- ultimate ready ------------------------------------------------------

    def _check_ult(self, state: dict) -> list[str]:
        if not self.ult_enabled or not state.get("hero", {}).get("alive"):
            self._sync_ult_baseline(state)  # keep baseline fresh while dead/off
            return []
        msgs: list[str] = []
        for ab in state.get("abilities", {}).values():
            if not isinstance(ab, dict) or not ab.get("ultimate"):
                continue
            name = ab.get("name", "")
            if not name or ab.get("level", 0) < 1 or ab.get("max_cooldown", 0) < ULT_MIN_COOLDOWN:
                continue
            cd = ab.get("cooldown", 0)
            prev = self._prev_ult_cd.get(name)
            if prev is not None and prev > 0 and cd == 0:
                msgs.append("Your ultimate is ready")
            self._prev_ult_cd[name] = cd
        return msgs

    def _sync_ult_baseline(self, state: dict) -> None:
        for ab in state.get("abilities", {}).values():
            if isinstance(ab, dict) and ab.get("ultimate") and ab.get("name"):
                self._prev_ult_cd[ab["name"]] = ab.get("cooldown", 0)

    # --- TP scroll -----------------------------------------------------------

    def _check_tp(self, state: dict) -> list[str]:
        if not self.no_tp_enabled:
            return []
        clock = state.get("map", {}).get("clock_time", 0)
        hero = state.get("hero", {})
        items = state.get("items", {})
        if clock < NO_TP_START or not hero.get("alive") or not items:
            return []

        tp = items.get("teleport0", {})
        has_tp = tp.get("name", "empty") != "empty" and tp.get("charges", 1) != 0
        # Boots of Travel make TP scrolls unnecessary.
        has_bots = any(
            v.get("name", "").startswith("item_travel_boots")
            for v in items.values()
            if isinstance(v, dict)
        )
        if has_tp or has_bots:
            return []
        now = time.monotonic()
        if now - self.last_no_tp < NO_TP_COOLDOWN:
            return []
        self.last_no_tp = now
        return ["No TP scroll"]

    # --- death ---------------------------------------------------------------

    def _check_death(self, state: dict) -> list[str]:
        hero = state.get("hero", {})
        alive = hero.get("alive")
        msgs: list[str] = []

        if self.prev_alive is True and alive is False:
            self.death_count += 1
            self._snapshot_death(state)  # always logged for post-game stats

            respawn = hero.get("respawn_seconds")
            parts = []
            if not self.death_recap_enabled:
                self.prev_alive = alive
                return msgs
            if respawn:
                parts.append(f"Respawn in {respawn} seconds.")
            bb_cost = hero.get("buyback_cost")
            bb_cd = hero.get("buyback_cooldown", 0)
            gold = state.get("player", {}).get("gold")
            if bb_cost is not None and gold is not None:
                if bb_cd and bb_cd > 0:
                    parts.append("Buyback on cooldown.")
                elif gold >= bb_cost:
                    parts.append(f"Buyback available, costs {bb_cost}.")
                else:
                    parts.append(f"No buyback. Need {bb_cost - gold} more gold.")
            if parts:
                msgs.append(" ".join(parts))

        self.prev_alive = alive
        return msgs

    def _snapshot_death(self, state: dict) -> None:
        record = {
            "death_number": self.death_count,
            "received_at": time.time(),
            "clock_time": state.get("map", {}).get("clock_time"),
            "state": state,
        }
        with self.deaths_log.open("a") as f:
            f.write(json.dumps(record) + "\n")


def make_alert_engine(log_dir: Path) -> AlertEngine:
    log_dir.mkdir(exist_ok=True)
    return AlertEngine(
        deaths_log=log_dir / f"deaths_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    )
