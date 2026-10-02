"""Phase A: itemization engine.

Two things climb MMR through itemization and both are knowable *before* you
right-click a creep:

1. **What winners build on your hero** — pulled live from OpenDota's
   itemPopularity endpoint (start / early / mid / late game item frequencies),
   so the core build reflects real winning games, not a static guide.
2. **What to build *against this enemy draft*** — a curated hero-threat table
   (magic burst, healing, invisibility, evasion, key passives, hard disables…)
   feeds a small rules engine that outputs situational items with the *reason*
   and which enemy heroes triggered each one. Understanding the "why" is the
   part that actually transfers to your next game.

Design choices:
- **Accuracy over coverage.** A hero only carries a tag when the counter-item
  response is clearly correct. An untagged hero simply contributes no
  situational suggestion — that's fine. A *wrong* suggestion is worse than a
  missing one, so the table below is conservative and easy to correct.
- **Deterministic at runtime.** No LLM in the hot path — this is a lookup plus
  a handful of rules, so it's instant and free. Edit ENEMY_TAGS / ITEM_RULES
  when a patch shifts things, same as timings.toml tracks the patch.
- OpenDota responses reuse draft.py's on-disk cache, so we stay well under the
  free rate limit.
"""

from __future__ import annotations

from . import draft

# --------------------------------------------------------------------------- #
# Threat vocabulary
# --------------------------------------------------------------------------- #
# Each tag names a threat that has a clear item answer. Keep the vocabulary
# small so the rules stay legible.
#
#   magic_burst      heavy magical nuke damage            -> BKB, Pipe, magic resist
#   silence          has a silence                        -> Manta, Lotus, status resist
#   hard_disable     long / AoE lockdown (stun, hex, ...) -> BKB, Lotus, Manta
#   single_target    targeted pickoff spell Linken's eats -> Linken's, Lotus
#   healing          strong healing or high HP regen      -> Spirit Vessel, break
#   invisibility     can go invisible                     -> Sentry/Dust/Gem, detection
#   evasion          grants passive evasion (miss chance) -> Monkey King Bar
#   illusions        creates illusions                    -> cleave / chain lightning
#   summons          summons units / creeps               -> AoE, Crimson Guard
#   break_target     strong passive worth removing        -> Silver Edge / Break
#   physical_carry   scaling physical right-click carry    -> Halberd, armor, Ghost
#   blink_initiator  hard blink initiation (positioning)  -> Force/Glimmer, awareness

# Keyed by localized hero name, lower-cased. Only tag when the response is
# clearly correct — see the module docstring.
ENEMY_TAGS: dict[str, set[str]] = {
    # --- magical burst casters ---
    "lina": {"magic_burst", "silence", "single_target"},
    "lion": {"magic_burst", "hard_disable", "single_target"},
    "zeus": {"magic_burst"},
    "skywrath mage": {"magic_burst", "silence", "single_target"},
    "tinker": {"magic_burst"},
    "leshrac": {"magic_burst"},
    "queen of pain": {"magic_burst"},
    "puck": {"magic_burst", "hard_disable"},
    "storm spirit": {"magic_burst"},
    "pugna": {"magic_burst"},
    "invoker": {"magic_burst", "hard_disable"},
    "shadow fiend": {"magic_burst"},
    "nyx assassin": {"magic_burst"},
    "sand king": {"magic_burst", "invisibility", "blink_initiator"},
    "disruptor": {"magic_burst", "hard_disable"},
    "jakiro": {"magic_burst"},
    "warlock": {"magic_burst", "healing", "summons"},
    "witch doctor": {"magic_burst"},
    "bane": {"magic_burst", "hard_disable", "single_target"},
    "death prophet": {"magic_burst", "silence"},
    "grimstroke": {"magic_burst", "silence"},
    "dazzle": {"magic_burst", "healing"},
    "necrophos": {"magic_burst", "healing", "single_target"},
    "ancient apparition": {"magic_burst", "single_target"},  # ice blast shuts off healing too
    "silencer": {"magic_burst", "silence"},
    "outworld destroyer": {"magic_burst", "hard_disable"},
    "enigma": {"hard_disable", "summons", "blink_initiator"},
    "dark seer": {"magic_burst", "illusions"},

    # --- hard disablers / initiators ---
    "shadow shaman": {"hard_disable", "single_target", "summons"},
    "faceless void": {"hard_disable", "break_target", "physical_carry"},
    "magnus": {"hard_disable", "blink_initiator"},
    "tidehunter": {"hard_disable", "break_target", "blink_initiator"},
    "axe": {"hard_disable", "blink_initiator", "single_target"},
    "centaur warrunner": {"blink_initiator", "break_target"},
    "earthshaker": {"hard_disable", "blink_initiator"},
    "slardar": {"hard_disable", "blink_initiator", "physical_carry"},
    "naga siren": {"hard_disable", "illusions", "physical_carry"},
    "winter wyvern": {"hard_disable", "healing"},
    "legion commander": {"single_target"},  # duel
    "doom": {"single_target", "silence"},
    "pudge": {"single_target", "hard_disable"},
    "sniper": {"single_target", "physical_carry"},

    # --- physical / right-click carries ---
    "anti-mage": {"physical_carry"},
    "phantom assassin": {"physical_carry", "evasion", "break_target"},
    "juggernaut": {"physical_carry"},
    "sven": {"physical_carry"},
    "wraith king": {"physical_carry"},
    "troll warlord": {"physical_carry"},
    "drow ranger": {"physical_carry", "silence"},
    "luna": {"physical_carry"},
    "gyrocopter": {"physical_carry"},
    "terrorblade": {"physical_carry", "illusions"},
    "medusa": {"physical_carry"},
    "morphling": {"physical_carry"},
    "monkey king": {"physical_carry"},
    "ursa": {"physical_carry", "break_target"},
    "slark": {"physical_carry", "break_target"},
    "spectre": {"physical_carry", "break_target", "illusions"},  # haunt
    "lifestealer": {"physical_carry", "healing", "break_target"},
    "bloodseeker": {"physical_carry"},
    "templar assassin": {"physical_carry", "invisibility"},
    "razor": {"physical_carry"},
    "clinkz": {"physical_carry", "invisibility"},
    "riki": {"physical_carry", "invisibility", "silence", "break_target"},
    "chaos knight": {"physical_carry", "illusions"},
    "phantom lancer": {"physical_carry", "illusions"},
    "meepo": {"physical_carry", "illusions"},
    "arc warden": {"physical_carry", "illusions"},
    "weaver": {"physical_carry"},
    "ember spirit": {"physical_carry"},
    "huskar": {"healing", "break_target"},

    # --- healing / high-regen ---
    "omniknight": {"healing"},
    "oracle": {"healing"},
    "abaddon": {"healing"},
    "chen": {"healing", "summons"},
    "enchantress": {"healing", "summons"},
    "treant protector": {"healing"},
    "io": {"healing"},
    "undying": {"healing"},
    "bristleback": {"healing", "break_target"},
    "timbersaw": {"healing", "break_target", "magic_burst"},
    "alchemist": {"healing"},
    "broodmother": {"healing", "summons", "break_target"},

    # --- invisibility ---
    "bounty hunter": {"invisibility"},
    "mirana": {"invisibility"},  # moonlight shadow (team invis)

    # --- summoners ---
    "nature's prophet": {"summons"},
    "beastmaster": {"summons"},
    "visage": {"summons"},
    "lycan": {"summons", "physical_carry"},
    "lone druid": {"summons", "physical_carry"},
    "venomancer": {"summons"},
}


# --------------------------------------------------------------------------- #
# Item rules
# --------------------------------------------------------------------------- #
# Each rule fires when at least `min` distinct enemy heroes carry any of the
# trigger tags AND the player's role is in `roles`. `tier` orders the output
# (core > strong > consider) and `base` breaks ties within a tier.

ROLES_CORE = ("carry", "mid", "offlane")
ROLES_ALL = ("carry", "mid", "offlane", "support")

ITEM_RULES: list[dict] = [
    {"item": "Black King Bar", "any_of": {"magic_burst", "hard_disable", "silence"},
     "min": 2, "roles": ROLES_CORE, "tier": "core", "base": 100,
     "reason": "Fight through their magic damage and disables."},

    {"item": "Detection (Sentry + Dust/Gem)", "any_of": {"invisibility"},
     "min": 1, "roles": ROLES_ALL, "tier": "core", "base": 95,
     "reason": "They have invisibility — carry detection or you die to pickoffs."},

    {"item": "Silver Edge", "any_of": {"break_target"},
     "min": 1, "roles": ROLES_CORE, "tier": "strong", "base": 80,
     "reason": "Break their key passive (Bristleback / Fury Swipes / Dispersion / Blur…), plus a pickoff blink."},

    {"item": "Monkey King Bar", "any_of": {"evasion"},
     "min": 1, "roles": ("carry", "mid", "offlane"), "tier": "strong", "base": 78,
     "reason": "Pierce their evasion — your right-clicks stop missing."},

    {"item": "Spirit Vessel", "any_of": {"healing"},
     "min": 1, "roles": ("offlane", "support", "mid"), "tier": "strong", "base": 75,
     "reason": "Cut their healing and high HP regen (and it chips their max HP)."},

    {"item": "Heaven's Halberd", "any_of": {"physical_carry"},
     "min": 1, "roles": ("offlane", "support"), "tier": "strong", "base": 74,
     "reason": "Disarm their right-click carry for 5s, plus evasion to survive lane."},

    {"item": "Manta Style", "any_of": {"silence"},
     "min": 1, "roles": ("carry", "mid", "offlane"), "tier": "strong", "base": 70,
     "reason": "Dispel their silences/roots on demand (also dodges targeted spells)."},

    {"item": "Linken's Sphere", "any_of": {"single_target"},
     "min": 1, "roles": ("carry", "mid"), "tier": "strong", "base": 68,
     "reason": "Eat their key single-target spell (Doom / Duel / Hex / Ice Blast / Assassinate)."},

    {"item": "Battle Fury / Mjollnir", "any_of": {"illusions", "summons"},
     "min": 1, "roles": ("carry",), "tier": "consider", "base": 60,
     "reason": "Cleave / chain-lightning clears their illusions and summons instantly."},

    {"item": "Crimson Guard", "any_of": {"physical_carry", "illusions", "summons"},
     "min": 2, "roles": ("offlane",), "tier": "consider", "base": 58,
     "reason": "Block a wall of physical hits from right-click, illusions and summons."},

    {"item": "Pipe of Insight", "any_of": {"magic_burst"},
     "min": 3, "roles": ("offlane", "support"), "tier": "consider", "base": 57,
     "reason": "Team magic shield — their lineup is a nuke fest."},

    {"item": "Assault Cuirass", "any_of": {"physical_carry"},
     "min": 3, "roles": ("carry", "offlane"), "tier": "consider", "base": 55,
     "reason": "Team armor and minus-armor vs a physical-heavy enemy team."},

    {"item": "Lotus Orb", "any_of": {"single_target", "hard_disable", "silence"},
     "min": 1, "roles": ("support", "offlane"), "tier": "consider", "base": 52,
     "reason": "Reflect their targeted spells and dispel disables/silences."},
]

_TIER_RANK = {"core": 3, "strong": 2, "consider": 1}

# Consumables we don't surface in the mid/late core columns (they're always-buys,
# not build decisions). Starting-item column keeps them — they matter there.
_LATE_TRIVIAL = {
    "tpscroll", "tango", "flask", "clarity", "faerie_fire", "enchanted_mango",
    "ward_observer", "ward_sentry", "ward_dispenser", "dust", "smoke_of_deceit",
    "tome_of_knowledge", "healing_salve", "branches", "recipe",
}


# --------------------------------------------------------------------------- #
# OpenDota item constants + popularity
# --------------------------------------------------------------------------- #

def _item_names() -> dict[int, str]:
    """OpenDota item id -> internal name (e.g. 1 -> 'blink'). Cached 7 days."""
    raw = draft._cached_get("/constants/item_ids", 7 * 86400)
    return {int(k): v for k, v in raw.items()}


def _item_meta() -> dict[str, dict]:
    """Internal item name -> {dname, cost, ...}. Cached 7 days."""
    return draft._cached_get("/constants/items", 7 * 86400)


def _pretty_item(internal: str) -> str:
    meta = _item_meta().get(internal) or {}
    return meta.get("dname") or internal.replace("_", " ").title()


def item_popularity(hero_id: int) -> dict:
    """Per-phase item purchase frequencies for a hero. Cached 1 day."""
    return draft._cached_get(f"/heroes/{hero_id}/itemPopularity", 86400)


def _top_items(freq: dict, n: int, drop_trivial: bool) -> list[dict]:
    """Map an {item_id: times_bought} bucket to the n most common items."""
    names = _item_names()
    rows = []
    for id_str, count in freq.items():
        internal = names.get(int(id_str))
        if not internal:
            continue
        if drop_trivial and (internal in _LATE_TRIVIAL or internal.startswith("recipe")):
            continue
        rows.append((count, internal))
    rows.sort(reverse=True)
    return [{"name": _pretty_item(internal), "count": count} for count, internal in rows[:n]]


def core_build(hero_id: int) -> dict | None:
    """'What winners build' on this hero, by game phase. None if unavailable."""
    try:
        pop = item_popularity(hero_id)
    except Exception:
        return None
    return {
        "starting": _top_items(pop.get("start_game_items", {}), 6, drop_trivial=False),
        "early": _top_items(pop.get("early_game_items", {}), 5, drop_trivial=True),
        "mid": _top_items(pop.get("mid_game_items", {}), 5, drop_trivial=True),
        "late": _top_items(pop.get("late_game_items", {}), 5, drop_trivial=True),
    }


# --------------------------------------------------------------------------- #
# Plan assembly
# --------------------------------------------------------------------------- #

def _enemy_tagset(enemy_ids: list[int]) -> list[tuple[str, set[str]]]:
    """[(hero_name, tags)] for each enemy id we can resolve."""
    all_heroes = draft.heroes()
    out = []
    for eid in enemy_ids:
        hero = all_heroes.get(eid)
        if not hero:
            continue
        name = hero["localized_name"]
        out.append((name, ENEMY_TAGS.get(name.lower(), set())))
    return out


def situational_items(enemy_ids: list[int], role: str) -> list[dict]:
    """Counter-item suggestions for the given enemy draft and player role,
    ordered core -> strong -> consider, each with the heroes that triggered it."""
    enemies = _enemy_tagset(enemy_ids)
    out = []
    for rule in ITEM_RULES:
        if role not in rule["roles"]:
            continue
        triggered = [name for name, tags in enemies if rule["any_of"] & tags]
        if len(triggered) < rule["min"]:
            continue
        out.append({
            "item": rule["item"],
            "tier": rule["tier"],
            "reason": rule["reason"],
            "vs": triggered,
            "_sort": (_TIER_RANK[rule["tier"]], rule["base"] + len(triggered)),
        })
    out.sort(key=lambda r: r["_sort"], reverse=True)
    for r in out:
        del r["_sort"]
    return out


def enemy_damage_profile(enemy_ids: list[int]) -> dict:
    """A one-line read of the enemy's damage make-up, so you know whether to
    lean magic-resist (BKB/Pipe) or armor (AC/Halberd) overall."""
    enemies = _enemy_tagset(enemy_ids)
    magic = sum(1 for _, t in enemies if "magic_burst" in t)
    physical = sum(1 for _, t in enemies if "physical_carry" in t)
    return {"magic": magic, "physical": physical}


def build_plan(hero_id: int, enemy_ids: list[int], role: str = "carry") -> dict:
    """Full item plan: winners' core build for your hero + situational items vs
    the enemy draft. Every field degrades gracefully if data is missing."""
    role = role if role in ROLES_ALL else "carry"
    all_heroes = draft.heroes()
    hero = all_heroes.get(hero_id)
    # Laning-threat advisory (Phase B) — imported lazily to avoid a circular
    # import (laning imports this module for the shared hero-tag table).
    try:
        from . import laning  # deferred: breaks the laning <-> itemization cycle
        lane_tips = laning.laning_threats(enemy_ids, role)
    except Exception:
        lane_tips = []
    return {
        "hero": {"id": hero_id, "name": hero["localized_name"] if hero else None},
        "role": role,
        "core": core_build(hero_id) if hero_id else None,
        "situational": situational_items(enemy_ids, role),
        "damage_profile": enemy_damage_profile(enemy_ids),
        "laning": lane_tips,
        "enemy_count": len([e for e in enemy_ids if e in all_heroes]),
    }
