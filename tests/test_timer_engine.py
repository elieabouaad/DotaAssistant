"""Simulate a match clock through the TimerEngine and check what fires when."""

from dota_assistant import config
from dota_assistant.timer_engine import TimerEngine


def make_state(clock: int, paused: bool = False) -> dict:
    return {
        "map": {
            "clock_time": clock,
            "paused": paused,
            "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
        }
    }


def make_engine(turbo: bool = False) -> TimerEngine:
    engine, _ = TimerEngine.from_config(config.resource("timings.toml"), turbo=turbo)
    return engine


def simulate(engine: TimerEngine, roshan_at: int | None = None) -> list[tuple[int, str]]:
    """Run the clock to 28:20 (past the Roshan respawn window)."""
    fired: list[tuple[int, str]] = []
    for clock in range(-30, 1701):
        if roshan_at is not None and clock == roshan_at:
            engine.roshan_killed()
        for msg in engine.update(make_state(clock)):
            fired.append((clock, msg))
    return fired


def test_normal_mode_schedule():
    fired = simulate(make_engine(), roshan_at=900)
    msgs = [m for _, m in fired]
    counts = {m: msgs.count(m) for m in set(msgs)}

    def count_of(prefix: str) -> int:
        return sum(n for m, n in counts.items() if m.startswith(prefix))

    assert count_of("Bounty runes") == 8, counts        # 0,4,...,28 min (7.41: every 4)
    assert count_of("Water rune") == 2, counts          # 2, 4 min
    assert count_of("Power rune") == 12, counts         # 6,8,...,28 min
    assert count_of("Wisdom shrine") == 4, counts       # 7, 14, 21, 28 min
    assert count_of("Night in") == 3, counts            # 5:00, 15:00, 25:00
    assert count_of("Day in") == 2, counts              # 10:00, 20:00
    assert count_of("Tormentor") == 1, counts           # 20:00 one-shot
    assert count_of("Stack now") == 27, counts          # every minute 1:53..27:53
    assert count_of("Roshan dead") == 0                 # trigger line isn't via update()
    assert count_of("Roshan possible in one minute") == 1
    assert count_of("Roshan may be up") == 1
    assert count_of("Roshan is up") == 1
    assert count_of("Aegis expires") == 1


def test_pause_suppresses_events():
    engine = make_engine()
    assert engine.update(make_state(160, paused=True)) == []


def test_clock_rewind_resets_fired_state():
    engine = make_engine()
    engine.update(make_state(160))
    # Rewinding to before the 2:00 water rune means a new match: it fires again.
    assert any("Water rune" in m
               for m in engine.update(make_state(100)) + engine.update(make_state(101)))


def test_turbo_mode_overrides():
    turbo = make_engine(turbo=True)
    fired = simulate(turbo)
    msgs = [m for _, m in fired]
    # Tormentor at 10:00 in Turbo (announced 9:30)
    assert (570, "Tormentor spawns in 30 seconds") in fired, fired[:5]
    # Wisdom shrines every 3:30 in Turbo: 7:00, 10:30, 14:00, ... within 28:20 -> 7
    assert sum(m.startswith("Wisdom shrine") for m in msgs) == 7, msgs
    # Bounty cadence unchanged in Turbo
    assert sum(m.startswith("Bounty") for m in msgs) == 8


def test_turbo_roshan_window():
    turbo = make_engine(turbo=True)
    turbo.update(make_state(900))
    line = turbo.roshan_killed()
    # Halved respawn window, 4-minute aegis
    assert "between 19:00 and 20:30" in line, line
    assert any(s.message.startswith("Aegis") and s.at == 900 + 240 - 30
               for s in turbo.oneshots)
