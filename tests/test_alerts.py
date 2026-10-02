"""Exercise the state-aware alert rules with simulated GSI states."""

import json

import pytest

from dota_assistant.alerts import AlertEngine


def make_state(
    clock=600,
    alive=True,
    tp_name="item_tpscroll",
    tp_charges=1,
    gold=1000,
    buyback_cost=800,
    buyback_cooldown=0,
    respawn=26,
    items_extra=None,
):
    items = {"teleport0": {"name": tp_name, "charges": tp_charges}}
    if items_extra:
        items.update(items_extra)
    return {
        "map": {"clock_time": clock, "paused": False,
                "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "hero": {"name": "npc_dota_hero_pudge", "alive": alive,
                 "respawn_seconds": 0 if alive else respawn,
                 "buyback_cost": buyback_cost, "buyback_cooldown": buyback_cooldown},
        "player": {"gold": gold},
        "items": items,
    }


@pytest.fixture
def log(tmp_path):
    return tmp_path / "deaths.jsonl"


def engine(log):
    return AlertEngine(deaths_log=log)


def test_tp_reminders(log):
    eng = engine(log)
    assert eng.update(make_state(tp_name="empty")) == ["No TP scroll"]
    # cooldown: no immediate repeat
    assert eng.update(make_state(tp_name="empty")) == []
    # has TP -> quiet
    assert engine(log).update(make_state()) == []
    # TP slot present but zero charges -> counts as no TP
    assert engine(log).update(make_state(tp_charges=0)) == ["No TP scroll"]
    # Boots of Travel -> quiet even without scroll
    state = make_state(tp_name="empty",
                       items_extra={"slot0": {"name": "item_travel_boots"}})
    assert engine(log).update(state) == []
    # too early in the game -> quiet
    assert engine(log).update(make_state(clock=30, tp_name="empty")) == []


def test_death_and_buyback(log):
    eng = engine(log)
    eng.update(make_state(alive=True))
    msgs = eng.update(make_state(alive=False, gold=1000, buyback_cost=800))
    assert msgs == ["Respawn in 26 seconds. Buyback available, costs 800."], msgs
    # staying dead doesn't re-announce
    assert eng.update(make_state(alive=False)) == []

    eng2 = engine(log)
    eng2.update(make_state(alive=True))
    msgs = eng2.update(make_state(alive=False, gold=500, buyback_cost=800))
    assert msgs == ["Respawn in 26 seconds. No buyback. Need 300 more gold."], msgs

    eng3 = engine(log)
    eng3.update(make_state(alive=True))
    msgs = eng3.update(make_state(alive=False, buyback_cooldown=120))
    assert msgs == ["Respawn in 26 seconds. Buyback on cooldown."], msgs

    # snapshots were written (one per death above)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(lines) == 3 and lines[0]["clock_time"] == 600


def bstate(hp, clock=1200, key="dota_badguys_tower3_mid", mx=2500):
    s = make_state(clock=clock)
    s["map"]["matchid"] = "1"
    s["buildings"] = {"dire": {key: {"health": hp, "max_health": mx}}}
    return s


def test_building_defense(log):
    eng = engine(log)
    assert eng.update(bstate(2500)) == []                    # full, baseline
    assert eng.update(bstate(2000)) == ["Tier three mid under attack"]  # took dmg
    assert eng.update(bstate(1800, clock=1205)) == []        # still in cooldown
    eng.update(bstate(2500, clock=1400))                     # recovered -> re-arm
    assert eng.update(bstate(2400, clock=1500)) == ["Tier three mid under attack"]


def test_t1_t2_towers_ignored(log):
    # Creep chip damage on outer towers is constant; no siren for those.
    eng = engine(log)
    eng.update(bstate(1800, key="dota_badguys_tower1_top", mx=1800))
    assert eng.update(bstate(1400, key="dota_badguys_tower1_top", mx=1800)) == []


def ustate(cd, level=3, maxcd=60):
    s = make_state()
    s["map"]["matchid"] = "1"
    s["abilities"] = {"ability4": {
        "name": "batrider_flaming_lasso", "ultimate": True, "level": level,
        "cooldown": cd, "max_cooldown": maxcd, "can_cast": True}}
    return s


def test_ultimate_ready(log):
    eng = engine(log)
    assert eng.update(ustate(10)) == []                     # on cooldown
    assert eng.update(ustate(0)) == ["Your ultimate is ready"]  # came off cd
    assert eng.update(ustate(0)) == []                      # already ready, quiet
    assert eng.update(ustate(60)) == []                     # used again
    assert eng.update(ustate(0)) == ["Your ultimate is ready"]  # ready again


def test_short_cooldown_ult_is_quiet(log):
    eng = engine(log)
    eng.update(ustate(5, maxcd=8))
    assert eng.update(ustate(0, maxcd=8)) == []
