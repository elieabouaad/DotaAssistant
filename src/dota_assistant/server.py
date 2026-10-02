"""Dota 2 live assistant server.

Receives GSI POSTs from the Dota 2 client, logs raw payloads to
logs/session_<timestamp>.jsonl (capped & rotated, see [logging] in config),
prints a one-line summary per update, and drives the voice announcer, alert
engines, and the web dashboard.

Run:  dota-assistant   (then start Dota 2 with -gamestateintegration)
"""

import json
import os
import sys
import threading
import time
from collections import deque

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from . import config as app_config
from . import draft, enrich, itemization, laning, review, settings, stats, weakness
from .alerts import make_alert_engine
from .announcer import Announcer
from .coach import Coach
from .discord_bot import DiscordAnnouncer
from .game_events import GameEventEngine, hero_short, own_player_id, slot_label
from .session_log import SessionLogger
from .timer_engine import TimerEngine

# Windows consoles often use cp1252, which can't encode the 🔊 marker and
# crashes the request handler mid-print. Degrade unprintable chars to '?'.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

_server_cfg = app_config.section("server")
HOST = _server_cfg.get("host", "127.0.0.1")
PORT = int(_server_cfg.get("port", 53100))

# Resolved talent trees (hero npc name -> {name, tiers[...]}), baked offline by
# `dota-assistant build-talents` and shipped as package data. Optional: the
# dashboard just shows "no talent data" if it's missing.
TALENTS: dict = app_config.read_resource_json("talents.json", {})

# Static world coordinates for every tower/rax/ancient (keyed by GSI building
# name) plus Roshan/fountains, so the dashboard can draw a minimap. GSI never
# sends building positions, so these are baked from map data; the hero's live
# xpos/ypos share the same world-coordinate space, so everything lines up.
MAP_COORDS: dict = app_config.read_resource_json(
    "map_coords.json", {"towers": {}, "extras": {}})

app = FastAPI()

# Turbo mode: GSI doesn't expose the game mode in player view, so it's a
# manual switch — start with --turbo (or DOTA_TURBO=1) for Turbo matches.
TURBO = "--turbo" in sys.argv or os.environ.get("DOTA_TURBO") == "1"

engine, announcer_cfg = TimerEngine.from_config(
    app_config.resource("timings.toml"), turbo=TURBO)
alert_engine = make_alert_engine(app_config.log_dir())
event_engine = GameEventEngine()
lane_coach = laning.LaneCoach()
config = app_config.load()
session_logger = SessionLogger(config.get("logging", {}), app_config.log_dir())
ACCOUNT_ID = config.get("player", {}).get("account_id") or None
BRACKET = config.get("player", {}).get("bracket") or "all"
announcer = Announcer(
    voice=announcer_cfg.get("voice", ""),
    rate=announcer_cfg.get("rate", 210),
)
coach = Coach(config.get("coach", {}), announcer)
discord_announcer = DiscordAnnouncer(config.get("discord", {}))
if discord_announcer.enabled:
    announcer.sinks.append(discord_announcer.say)
    # Don't double up: while the Discord bot is connected to the voice channel,
    # speak only there. Local OS voice resumes as a fallback if the bot isn't
    # joined yet (or drops), so you're never left with no audio.
    announcer.suppress_local = discord_announcer.ready.is_set

# --- announcement settings (dashboard Settings page) ----------------------- #
# Toggles that live outside the timer engine. Timer-event toggles come from
# engine.registry(); these cover the state-aware alerts and Roshan timers.
announce_state = {"roshan_timers": True}
EXTRA_ANNOUNCE = [
    {"key": "alert_no_tp", "label": "No-TP reminder", "category": "Alerts",
     "description": "Warns when you have no TP scroll after the laning stage"},
    {"key": "alert_death", "label": "Death recap", "category": "Alerts",
     "description": "Respawn time + buyback status when you die"},
    {"key": "roshan_timers", "label": "Roshan & Aegis timers", "category": "Roshan",
     "description": "Respawn window and Aegis expiry after Roshan is killed"},
    {"key": "alert_smoke", "label": "Enemy smoke warning", "category": "Alerts",
     "description": "Warns when the enemy pops smoke — a gank is moving"},
    {"key": "alert_roshan_contest", "label": "Roshan contested", "category": "Roshan",
     "description": "Warns when Roshan is being hit (roar), so you can contest"},
    {"key": "alert_teamfight", "label": "Teamfight swing", "category": "Alerts",
     "description": "Calls a man-advantage window (take Roshan / push) after a fight"},
    {"key": "alert_enemy_activity", "label": "Enemy glyph / scan / buyback", "category": "Alerts",
     "description": "Calls out enemy glyph, scan, and buyback — push/objective windows"},
    {"key": "alert_defense", "label": "Base under attack", "category": "Alerts",
     "description": "Sirens when your tier-3+, barracks, or throne take damage"},
    {"key": "alert_ult", "label": "Ultimate ready", "category": "Alerts",
     "description": "Tells you when your ultimate comes off cooldown"},
    {"key": "alert_cs_benchmark", "label": "CS benchmark check", "category": "Alerts",
     "description": "Laning CS checks at 5/8/10 min vs your hero benchmark and history"},
]


def _announce_enabled(key: str) -> bool:
    if key == "alert_no_tp":
        return alert_engine.no_tp_enabled
    if key == "alert_death":
        return alert_engine.death_recap_enabled
    if key == "alert_smoke":
        return event_engine.smoke_enabled
    if key == "alert_roshan_contest":
        return event_engine.roshan_contest_enabled
    if key == "alert_teamfight":
        return event_engine.teamfight_enabled
    if key == "alert_enemy_activity":
        return event_engine.enemy_activity_enabled
    if key == "alert_defense":
        return alert_engine.defense_enabled
    if key == "alert_ult":
        return alert_engine.ult_enabled
    if key == "alert_cs_benchmark":
        return lane_coach.enabled
    return announce_state.get(key, True)


def apply_announce_setting(key: str, enabled: bool) -> bool:
    """Route a toggle to the right engine/flag. Returns True if key is known."""
    if key == "alert_no_tp":
        alert_engine.no_tp_enabled = enabled
    elif key == "alert_death":
        alert_engine.death_recap_enabled = enabled
    elif key == "roshan_timers":
        announce_state["roshan_timers"] = enabled
    elif key == "alert_smoke":
        event_engine.smoke_enabled = enabled
    elif key == "alert_roshan_contest":
        event_engine.roshan_contest_enabled = enabled
    elif key == "alert_teamfight":
        event_engine.teamfight_enabled = enabled
    elif key == "alert_enemy_activity":
        event_engine.enemy_activity_enabled = enabled
    elif key == "alert_defense":
        alert_engine.defense_enabled = enabled
    elif key == "alert_ult":
        alert_engine.ult_enabled = enabled
    elif key == "alert_cs_benchmark":
        lane_coach.enabled = enabled
    else:
        return engine.set_enabled(key, enabled)
    return True


def announce_registry() -> list[dict]:
    reg = engine.registry()
    for item in EXTRA_ANNOUNCE:
        reg.append({**item, "enabled": _announce_enabled(item["key"])})
    return reg


# Apply the user's saved overrides on top of the timings.toml / alert defaults.
for _key, _enabled in settings.announce_overrides().items():
    apply_announce_setting(_key, _enabled)

# Latest full state, kept for later phases (dashboard, coach).
latest_state: dict = {}
_update_count = 0
_last_summary = ""
_seen_events: set = set()
feed: deque = deque(maxlen=40)  # recent human-readable game events
_ingested_matches: set = set()  # match_ids already saved to stats.db this run


def ingest_match(match_id: str) -> None:
    """Parse this session's log into stats.db (own connection — called off the
    request path in a thread, and sqlite connections aren't shareable)."""
    if session_logger.path is None:
        print(f"stats: no session log to ingest for match {match_id} "
              f"([logging] session_logs is off)", flush=True)
        return
    try:
        conn = stats.connect()
        written = stats.ingest_current(conn, session_logger.path)
        conn.close()
        print(f"Saved match {match_id} to stats.db ({len(written)} total in file)", flush=True)
    except Exception as exc:
        print(f"stats: failed to ingest match {match_id}: {exc}", flush=True)


def maybe_ingest_on_end(state: dict) -> None:
    """When a match ends (a winner is declared, or we reach POST_GAME), roll the
    raw log up into the stats database so it shows in the history tab."""
    map_ = state.get("map", {})
    match_id = map_.get("matchid")
    if not match_id or match_id in ("0", "") or match_id in _ingested_matches:
        return
    ended = map_.get("win_team", "none") not in ("none", None) or \
        map_.get("game_state") == "DOTA_GAMERULES_STATE_POST_GAME"
    if not ended:
        return
    _ingested_matches.add(match_id)
    threading.Thread(target=ingest_match, args=(match_id,), daemon=True).start()

FRIENDLY_EVENTS = {
    "CHAT_MESSAGE_FIRSTBLOOD": "First blood!",
    "CHAT_MESSAGE_HERO_KILL": "Hero kill",
    "CHAT_MESSAGE_STREAK_KILL": "Kill streak ended",
    "CHAT_MESSAGE_ROSHAN_KILL": "Roshan killed",
    "CHAT_MESSAGE_AEGIS": "Aegis picked up",
    "CHAT_MESSAGE_AEGIS_STOLEN": "Aegis stolen!",
    "CHAT_MESSAGE_COURIER_LOST": "Courier sniped",
    "CHAT_MESSAGE_TOWER_KILL": "Tower destroyed",
    "CHAT_MESSAGE_TOWER_DENY": "Tower denied",
    "CHAT_MESSAGE_BARRACKS_KILL": "Barracks destroyed",
    "CHAT_MESSAGE_GLYPH_USED": "Glyph used",
    "CHAT_MESSAGE_RUNE_PICKUP": "Rune taken",
}
IGNORED_EVENTS = {"CHAT_MESSAGE_ITEM_PURCHASE"}  # pure spam in the feed


def watch_events(state: dict) -> None:
    """GSI repeats each game event across many consecutive payloads, so dedupe
    by content. Feeds the dashboard event feed, and auto-triggers the Roshan
    timers when the kill shows up in chat events (F8 remains as backup)."""
    global _seen_events
    clock = state.get("map", {}).get("clock_time")
    my_id = own_player_id(state)
    my_hero = hero_short(state)
    for e in state.get("events", []):
        key = (e.get("game_time"), e.get("event_type"), e.get("data", ""))
        if key in _seen_events:
            continue
        _seen_events.add(key)

        etype = e.get("event_type", "")
        text = None
        team = None  # killer's team, so the dashboard can colour the line
        if etype == "generic_event":
            try:
                data = json.loads(e.get("data", "{}"))
                mtype = data.get("type", "")
            except (json.JSONDecodeError, AttributeError):
                data, mtype = {}, ""
            if mtype in IGNORED_EVENTS:
                continue
            if mtype == "CHAT_MESSAGE_HERO_KILL":
                killer, victim = data.get("playerid1"), data.get("playerid2")
                kl = slot_label(killer, my_id, my_hero)
                vl = slot_label(victim, my_id, my_hero)
                text = f"{kl} killed {vl}"
                team = "radiant" if (killer is not None and 0 <= killer <= 4) else \
                       "dire" if (killer is not None and 5 <= killer <= 9) else None
            else:
                text = FRIENDLY_EVENTS.get(
                    mtype, mtype.removeprefix("CHAT_MESSAGE_").replace("_", " ").capitalize()
                )
        elif etype == "roshan_killed":
            # GSI reports Roshan's death directly (top-level event, not a chat
            # message), so the timer starts itself — F8 is just a backup now.
            team = e.get("killed_by_team", "")
            text = f"Roshan killed{f' by {team}' if team else ''}"
            if announce_state["roshan_timers"]:
                announcer.say(engine.roshan_killed())
        elif etype == "aegis_picked_up":
            text = "Aegis snatched!" if e.get("snatched") else "Aegis picked up"
        elif etype == "bounty_rune_pickup":
            text = "Bounty rune taken"
        elif etype:
            text = etype.replace("_", " ").capitalize()
        if text:
            item = {"clock": clock, "text": text}
            if team:
                item["team"] = team
            feed.appendleft(item)
    if len(_seen_events) > 20000:  # new match hygiene
        _seen_events = set()


def clock_str(seconds: int | None) -> str:
    if seconds is None:
        return "--:--"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"


def summarize(state: dict) -> str:
    """One-line human summary of a GSI payload for the console."""
    map_ = state.get("map", {})
    hero = state.get("hero", {})
    player = state.get("player", {})

    parts = []
    game_state = map_.get("game_state", "?")
    parts.append(f"[{clock_str(map_.get('clock_time'))}]")
    parts.append(game_state.replace("DOTA_GAMERULES_STATE_", ""))
    if hero.get("name"):
        parts.append(
            f"{hero['name'].removeprefix('npc_dota_hero_')} "
            f"lvl{hero.get('level', '?')} "
            f"hp {hero.get('health', '?')}/{hero.get('max_health', '?')} "
            f"mana {hero.get('mana', '?')}/{hero.get('max_mana', '?')}"
        )
    if "gold" in player:
        parts.append(f"gold {player['gold']}")
    if map_.get("daytime") is not None:
        parts.append("day" if map_["daytime"] else "night")
    return "  ".join(parts)


@app.post("/gsi")
async def gsi(request: Request):
    global latest_state, _update_count, _last_summary
    state = await request.json()
    latest_state = state
    _update_count += 1

    session_logger.write({"received_at": time.time(), "state": state})

    # Only print when something visible changed — GSI posts ~10x/second.
    summary = summarize(state)
    if summary != _last_summary:
        _last_summary = summary
        print(f"#{_update_count:<6} {summary}", flush=True)

    for message in (engine.update(state) + alert_engine.update(state)
                    + event_engine.update(state) + lane_coach.update(state)):
        announcer.say(message)
    watch_events(state)
    coach.maybe_tip(state, event_engine.context(state))
    maybe_ingest_on_end(state)
    return {"ok": True}


@app.get("/state")
async def get_state():
    """Latest raw state — handy for poking at the schema in a browser."""
    return latest_state


@app.get("/")
async def dashboard():
    return HTMLResponse(app_config.read_web("dashboard.html"))


@app.get("/api/live")
async def api_live():
    """Everything the live dashboard needs in one poll."""
    hero_name = (latest_state.get("hero") or {}).get("name") if latest_state else None
    return {
        "state": latest_state,
        "upcoming": engine.upcoming(),
        "feed": list(feed),
        "turbo": TURBO,
        # Resolved talent tree for the current hero (None if unknown), so the
        # dashboard can render it alongside the live talent_1..8 flags.
        "talents": TALENTS.get(hero_name) if hero_name else None,
        # Computed both-team tactical read (things not in the raw hero state).
        "context": event_engine.context(latest_state) if latest_state else {},
        # Ten-player K/D reconstructed from the kill feed (assists only for you).
        "scoreboard": event_engine.scoreboard(latest_state) if latest_state else [],
        # Live CS-vs-benchmark read for the laning widget (Phase B).
        "lane": lane_coach.last_read,
    }


@app.get("/api/history")
async def api_history():
    """Aggregate KPIs + per-hero breakdown + recent form for the History tab.
    Merges the locally captured GSI games with your full OpenDota history
    (run opendota_scraper.py to populate the latter), each tagged with a match
    type."""
    conn = stats.connect()
    try:
        matches = stats.unified_matches(conn)
        return {"summary": stats.unified_summary(matches), "matches": matches}
    finally:
        conn.close()


# Sync: an OpenDota-only match enriches from OpenDota's parsed data (network),
# so run in the threadpool rather than blocking the event loop.
@app.get("/api/match/{match_id}")
def api_match(match_id: str, refresh: bool = False):
    """Full detail for one recorded match: curves, deaths, build order. Locally
    captured (GSI) games return their rich detail directly; OpenDota-only games
    are enriched from OpenDota's parsed match (requesting a parse if needed).

    refresh=1 bypasses the unparsed-response cache, so a retry actually re-asks
    OpenDota instead of replaying the same stale answer."""
    conn = stats.connect()
    try:
        detail = stats.match_detail(conn, match_id)
    finally:
        conn.close()
    if detail is None:
        return {"error": "not found"}
    if detail.get("od_only"):
        return review.opendota_detail(match_id, base=detail, force=refresh)
    return detail


# Sync: blocks on OpenDota's parse queue (up to poll_timeout_s), so it belongs
# in the threadpool, not the event loop.
@app.get("/api/match/{match_id}/parse")
def api_match_parse(match_id: str):
    """Force a parse of one OpenDota match and return its detail once done.

    This is what the History tab's Regenerate button calls: it requests the
    parse, waits for OpenDota's job to finish, then re-fetches — rather than
    asking the user to come back in a minute and click again."""
    result = enrich.ensure_parsed(match_id)
    conn = stats.connect()
    try:
        detail = stats.match_detail(conn, match_id)
    finally:
        conn.close()
    if detail is None:
        return {"error": "not found", "parse_status": result}
    if detail.get("od_only"):
        detail = review.opendota_detail(match_id, base=detail, force=True)
    detail["parse_status"] = result
    return detail


@app.get("/api/enrich/status")
def api_enrich_status():
    """Progress of the background OpenDota parse backfill."""
    conn = stats.connect()
    try:
        return enrich.status(conn)
    finally:
        conn.close()


# Sync (not async) on purpose: the review does blocking OpenDota + Claude calls,
# so FastAPI runs it in a threadpool and it never stalls live GSI ingestion.
@app.get("/api/review/{match_id}")
def api_review(match_id: str, coach: bool = True):
    """Phase C: post-game deep review — merged OpenDota + local analysis, plus a
    Claude write-up when credentials are available."""
    return review.review(match_id, with_claude=coach)


# Sync: _cs_finding may hit OpenDota's benchmark endpoint (cached) on a cache
# miss, so run it in the threadpool rather than blocking the event loop.
@app.get("/api/improve")
def api_improve():
    """Phase D: personal weakness report mined from stats.db."""
    conn = stats.connect()
    try:
        return weakness.report(conn)
    finally:
        conn.close()


@app.get("/api/mapcoords")
async def api_mapcoords():
    """Static building/Roshan world coordinates for the minimap (fetched once)."""
    return MAP_COORDS


@app.get("/api/heroes")
async def api_heroes():
    return sorted(draft.heroes().values(), key=lambda h: h["localized_name"])


@app.get("/api/counters")
def api_counters(ids: str, role: str = "", bracket: str = "",
                 pool: bool = False):
    """Phase E: bracket- and role-aware counter picks, optionally limited to
    your own hero pool. Sync so the heroStats fetch runs off the event loop."""
    enemy_ids = [int(i) for i in ids.split(",") if i.strip()]
    return draft.counter_picks(
        enemy_ids, account_id=ACCOUNT_ID, role=role or None,
        bracket=bracket or BRACKET, prioritize_pool=pool,
    )


@app.get("/api/brackets")
async def api_brackets():
    """Bracket keys + labels for the draft controls; `default` marks the one
    from [player] bracket in config.toml (all brackets when unset)."""
    return [{"key": k, "label": label, "default": k == BRACKET}
            for k, (_, label) in draft.BRACKETS.items()]


@app.get("/api/itemplan")
async def api_itemplan(hero: int, enemies: str = "", role: str = "carry"):
    """Phase A: winners' core build for `hero` + situational items vs the enemy
    draft (comma-separated enemy hero ids) for the given role."""
    enemy_ids = [int(i) for i in enemies.split(",") if i.strip()]
    return itemization.build_plan(hero, enemy_ids, role)


@app.post("/event/roshan-killed")
async def roshan_killed():
    if not announce_state["roshan_timers"]:
        return {"ok": True, "announced": None, "note": "Roshan timers disabled in settings"}
    line = engine.roshan_killed()
    announcer.say(line)
    return {"ok": True, "announced": line}


@app.get("/api/settings")
async def get_settings():
    """Every announcement toggle with its current state, for the Settings page."""
    return {"announce": announce_registry()}


@app.post("/api/settings")
async def post_settings(request: Request):
    body = await request.json()
    key, enabled = body.get("key"), bool(body.get("enabled"))
    if not key or not apply_announce_setting(key, enabled):
        return {"ok": False, "error": "unknown setting key"}
    settings.set_announce(key, enabled)
    return {"ok": True, "key": key, "enabled": enabled}


def start_hotkeys() -> None:
    """F8 = Roshan killed. Requires macOS Input Monitoring permission for the
    terminal; degrades to the HTTP endpoint if unavailable."""
    try:
        from pynput import keyboard

        def on_press(key):
            if key == keyboard.Key.f8 and announce_state["roshan_timers"]:
                announcer.say(engine.roshan_killed())

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
        print("Hotkeys active: F8 = Roshan killed", flush=True)
    except Exception as exc:  # no pynput / no input-monitoring permission
        print(
            f"Global hotkeys unavailable ({exc}); use "
            f"curl -X POST http://{HOST}:{PORT}/event/roshan-killed instead",
            flush=True,
        )


def start_overlay() -> None:
    """--overlay: launch the in-game HUD alongside the server, so a
    single-monitor setup is one command. It's a separate process on purpose
    (tkinter wants the main thread, and a crashed HUD shouldn't take GSI with
    it); it polls /api/live like any other client."""
    import subprocess
    try:
        subprocess.Popen([sys.executable, "-m", "dota_assistant.overlay"])
        print("Overlay launched (F9 = show/hide)", flush=True)
    except Exception as exc:
        print(f"Could not start overlay ({exc}); run `dota-assistant overlay` yourself",
              flush=True)


def main() -> None:
    print(f"Mode: {'TURBO' if TURBO else 'normal'}")
    print(f"GSI receiver listening on http://{HOST}:{PORT}/gsi")
    if session_logger.enabled:
        print(f"Raw payload logs: {app_config.log_dir()} "
              f"(cap {session_logger.max_bytes // 2**20}MB/session, "
              f"keeping {session_logger.keep} sessions)")
    print("Start Dota 2 with the -gamestateintegration launch option "
          "(install the GSI file with: dota-assistant gsi-config).")
    session_logger.cleanup()
    threading.Thread(target=start_hotkeys, daemon=True).start()
    # Backfill OpenDota parses for recent matches that have no detail yet, so
    # the History tab has curves/builds/death maps without anyone clicking
    # Regenerate. Daemon thread: it never delays startup or blocks GSI.
    if "--no-enrich" in sys.argv:
        print("OpenDota enrich: skipped (--no-enrich)", flush=True)
    elif not enrich.start_background():
        print("OpenDota enrich: disabled ([enrich] on_launch = false)", flush=True)
    if "--overlay" in sys.argv:
        start_overlay()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
