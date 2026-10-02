"""Clock-driven timer engine.

Fed every GSI update; compares the game clock against the recurring events
defined in timings.toml plus dynamically scheduled one-shots (Roshan timers),
and returns the announcements that are due. Each occurrence fires exactly once;
a clock rewind (new match, replay scrub) resets all fired state.
"""

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ACTIVE_STATES = {
    "DOTA_GAMERULES_STATE_PRE_GAME",
    "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
}

# A one-shot still fires if we notice it up to this many seconds late
# (GSI updates can gap around pauses/reconnects).
ONESHOT_GRACE = 15


def dict_list(items) -> list[dict]:
    return [dict(i) for i in items]


def apply_turbo_overrides(d: dict, turbo: bool) -> dict:
    """Fields prefixed turbo_ replace their base field in Turbo mode and are
    stripped either way (e.g. turbo_first=600 overrides first when turbo)."""
    out = {}
    for key, value in d.items():
        if key.startswith("turbo_"):
            if turbo:
                out[key.removeprefix("turbo_")] = value
        elif not (turbo and f"turbo_{key}" in d):
            out[key] = value
    return out


def fmt_clock(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"


@dataclass
class RecurringEvent:
    name: str
    message: str
    first: int
    interval: int
    lead: int
    end: int | None = None
    enabled: bool = True


@dataclass
class OneShot:
    at: int
    message: str


@dataclass
class TimerEngine:
    events: list[RecurringEvent]
    roshan_cfg: dict
    fired: set = field(default_factory=set)
    oneshots: list[OneShot] = field(default_factory=list)
    last_clock: int | None = None

    @classmethod
    def from_config(cls, path: Path, turbo: bool = False) -> tuple["TimerEngine", dict]:
        cfg = tomllib.loads(path.read_text())
        events = []
        for e in dict_list(cfg.get("events", [])):
            e = apply_turbo_overrides(e, turbo)
            events.append(RecurringEvent(**e))
        roshan = apply_turbo_overrides(dict(cfg.get("roshan", {})), turbo)
        return cls(events=events, roshan_cfg=roshan), cfg.get("announcer", {})

    def update(self, state: dict) -> list[str]:
        map_ = state.get("map", {})
        clock = map_.get("clock_time")
        if clock is None or map_.get("paused"):
            return []
        if map_.get("game_state") not in ACTIVE_STATES:
            return []

        if self.last_clock is not None and clock < self.last_clock - 5:
            # New match or clock rewind: forget everything.
            self.fired.clear()
            self.oneshots.clear()
        self.last_clock = clock

        due: list[str] = []
        for ev in self.events:
            if not ev.enabled:
                continue
            occurrence = self._current_occurrence(ev, clock)
            if occurrence is None:
                continue
            key = (ev.name, occurrence)
            if key not in self.fired:
                self.fired.add(key)
                due.append(ev.message)

        remaining: list[OneShot] = []
        for shot in self.oneshots:
            if shot.at <= clock <= shot.at + ONESHOT_GRACE:
                due.append(shot.message)
            elif clock < shot.at:
                remaining.append(shot)
            # else: missed by more than the grace window — drop silently
        self.oneshots = remaining
        return due

    def _current_occurrence(self, ev: RecurringEvent, clock: int) -> int | None:
        """Occurrence time T where T - lead <= clock <= T, else None."""
        if clock < ev.first - ev.lead:
            return None
        if ev.interval <= 0:
            t = ev.first
        else:
            k = math.floor((clock + ev.lead - ev.first) / ev.interval)
            t = ev.first + k * ev.interval
        if ev.end is not None and t > ev.end:
            return None
        if t - ev.lead <= clock <= t:
            return t
        return None

    # --- dashboard support ---------------------------------------------------

    LABELS = {
        "bounty_runes": "Bounty runes",
        "water_runes": "Water rune",
        "power_runes": "Power rune",
        "wisdom_shrine": "Wisdom shrine",
        "stack_reminder": "Stack camps",
        "night_falls": "Night",
        "day_breaks": "Day",
        "tormentor_spawn": "Tormentor",
        "lotus_spawn": "Lotus",
    }

    DESCRIPTIONS = {
        "bounty_runes": "Every 3 min — grab bounties for gold",
        "water_runes": "2:00 and 4:00 in the river",
        "power_runes": "Every 2 min from 6:00 — top/bottom river",
        "wisdom_shrine": "Channel for a burst of XP",
        "stack_reminder": "Pull/stack the neutral camp at X:53",
        "night_falls": "Vision shrinks — night is coming",
        "day_breaks": "Vision returns — day is coming",
        "tormentor_spawn": "Kill for the Shard/Aghs blessing",
        "lotus_spawn": "Healing Lotus in the river — first 3:00, then every 3 min",
    }

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Toggle a recurring event live. Returns True if the name matched."""
        for ev in self.events:
            if ev.name == name:
                ev.enabled = enabled
                return True
        return False

    def registry(self) -> list[dict]:
        """Describe every recurring event for the Settings page."""
        return [
            {
                "key": ev.name,
                "label": self.LABELS.get(ev.name, ev.name.replace("_", " ").capitalize()),
                "description": self.DESCRIPTIONS.get(ev.name, ""),
                "category": "Timers",
                "enabled": ev.enabled,
            }
            for ev in self.events
        ]

    def upcoming(self, horizon: int = 900) -> list[dict]:
        """Next occurrence of each enabled event within `horizon` seconds,
        plus pending one-shots (Roshan/Aegis). For the dashboard."""
        if self.last_clock is None:
            return []
        c = self.last_clock
        out = []
        for ev in self.events:
            if not ev.enabled:
                continue
            if ev.interval <= 0:
                t = ev.first
                if t < c:
                    continue
            else:
                k = max(0, math.ceil((c - ev.first) / ev.interval))
                t = ev.first + k * ev.interval
            if ev.end is not None and t > ev.end:
                continue
            if t - c <= horizon:
                out.append({"at": t, "label": self.LABELS.get(
                    ev.name, ev.name.replace("_", " ").capitalize())})
        for shot in self.oneshots:
            out.append({"at": shot.at, "label": shot.message})
        out.sort(key=lambda e: e["at"])
        return out

    # --- Roshan (manual trigger) -------------------------------------------

    def roshan_killed(self) -> str:
        """Call when the user signals Roshan died. Returns the immediate line."""
        if self.last_clock is None:
            return "No game clock yet — Roshan timer not started"
        c = self.last_clock
        rmin = self.roshan_cfg.get("respawn_min", 480)
        rmax = self.roshan_cfg.get("respawn_max", 660)
        aegis = self.roshan_cfg.get("aegis_expiry", 300)
        # Replace any pending Roshan one-shots from a previous kill.
        self.oneshots = [s for s in self.oneshots if not s.message.startswith(("Roshan", "Aegis"))]
        self.oneshots += [
            OneShot(c + aegis - 30, "Aegis expires in 30 seconds"),
            OneShot(c + rmin - 60, "Roshan possible in one minute"),
            OneShot(c + rmin, "Roshan may be up"),
            OneShot(c + rmax, "Roshan is up"),
        ]
        return (
            f"Roshan dead at {fmt_clock(c)}. "
            f"Back between {fmt_clock(c + rmin)} and {fmt_clock(c + rmax)}"
        )
