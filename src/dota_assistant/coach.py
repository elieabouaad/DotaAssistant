"""Phase 4: AI coach — short spoken tips from Claude based on live game state.

Disabled by default. Enable in config.toml ([coach] enabled = true) and make
sure Anthropic credentials are available (ANTHROPIC_API_KEY env var, or an
`ant auth login` profile). Tips trigger on death and at most once per
min_interval seconds otherwise; each call sends a compact snapshot of your
hero's state and gets back one actionable sentence, spoken via the announcer.
"""

import json
import threading
import time


def _ult_status(state: dict) -> str | None:
    """'ready' / 'Ns' / 'no mana' / 'unlearned' for the hero's ultimate."""
    for ab in state.get("abilities", {}).values():
        if not isinstance(ab, dict) or not ab.get("ultimate"):
            continue
        if ab.get("level", 0) < 1:
            return "unlearned"
        if ab.get("cooldown", 0) > 0:
            return f"{ab['cooldown']}s"
        return "ready" if ab.get("can_cast", True) else "no mana"
    return None


def _base_threat(state: dict) -> str | None:
    """Lowest-HP own building that's below full, so the coach knows if you're
    being sieged. GSI only exposes your own team's buildings."""
    worst = None
    for team_blds in (state.get("buildings") or {}).values():
        if not isinstance(team_blds, dict):
            continue
        for key, info in team_blds.items():
            if not isinstance(info, dict):
                continue
            hp, mx = info.get("health"), info.get("max_health") or 1
            if hp is None or hp >= mx:
                continue
            pct = round(100 * hp / mx)
            if worst is None or pct < worst[1]:
                short = key.replace("dota_badguys_", "").replace("dota_goodguys_", "")
                worst = (short, pct)
    return f"{worst[0]} at {worst[1]}%" if worst else None


def snapshot(state: dict, extra: dict | None = None) -> dict | None:
    """Compact, low-token summary of the GSI state for the coach prompt."""
    map_ = state.get("map", {})
    hero = state.get("hero", {})
    player = state.get("player", {})
    if not hero.get("name"):
        return None
    items = [
        v["name"].removeprefix("item_")
        for k, v in state.get("items", {}).items()
        if isinstance(v, dict) and v.get("name", "empty") != "empty"
        and k.startswith(("slot", "teleport", "neutral"))
    ]
    my_team = player.get("team_name")
    my_score = map_.get(f"{my_team}_score")
    enemy_score = map_.get("dire_score" if my_team == "radiant" else "radiant_score")
    snap = {
        "clock": map_.get("clock_time"),
        "daytime": map_.get("daytime"),
        "hero": hero.get("name", "").removeprefix("npc_dota_hero_"),
        "level": hero.get("level"),
        "hp_pct": hero.get("health_percent"),
        "mana_pct": hero.get("mana_percent"),
        "alive": hero.get("alive"),
        "gold": player.get("gold"),
        "kills": player.get("kills"),
        "deaths": player.get("deaths"),
        "assists": player.get("assists"),
        "last_hits": player.get("last_hits"),
        "gpm": player.get("gpm"),
        "xpm": player.get("xpm"),
        "team_kills": my_score,
        "enemy_kills": enemy_score,
        "ultimate": _ult_status(state),
        "base_threat": _base_threat(state),
        "items": items,
    }
    if extra:
        # Both-team tactical context (enemy smoke/scan/buyback, fight swing).
        snap.update({k: v for k, v in extra.items() if v})
    return {k: v for k, v in snap.items() if v is not None}


SYSTEM = (
    "You are a Dota 2 coach whispering in the player's ear mid-game. You get a"
    " JSON snapshot of their hero's state. Reply with EXACTLY ONE actionable"
    " tip, maximum 20 words, plain spoken language (it is read aloud by"
    " text-to-speech mid-game). No preamble, no markdown, no emoji. Focus on"
    " the highest-impact next action: itemization, farming pattern, map"
    " objectives, or survival. The snapshot may include both-team context —"
    " team_kills vs enemy_kills, ultimate status, base_threat (your buildings"
    " under siege), and enemy_smoke_recent / enemy_scan_recent /"
    " fight_net_kills. Weight these heavily: react to a siege, a smoke, or a"
    " won/lost fight over generic farming advice. If nothing stands out,"
    " comment on item progression for their net worth and game time."
)


class Coach:
    def __init__(self, cfg: dict, announcer):
        self.announcer = announcer
        self.model = cfg.get("model")
        self.provider = cfg.get("provider", "auto")
        self.min_interval = cfg.get("min_interval", 120)
        self.enabled = bool(cfg.get("enabled", False))
        self.last_tip = 0.0
        self.prev_alive: bool | None = None
        self._busy = threading.Lock()
        if self.enabled:
            from . import llm
            how = "Claude Code" if (self.provider != "api" and llm.cli_available()) else "API key"
            print(f"Coach enabled (via {how})", flush=True)

    def maybe_tip(self, state: dict, extra: dict | None = None) -> None:
        """Called on every GSI update; decides whether a tip is due. `extra` is
        optional both-team tactical context (see GameEventEngine.context)."""
        if not self.enabled:
            return
        map_ = state.get("map", {})
        if map_.get("game_state") != "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS":
            return

        alive = state.get("hero", {}).get("alive")
        died = self.prev_alive is True and alive is False
        self.prev_alive = alive

        now = time.time()
        if not died and now - self.last_tip < self.min_interval:
            return
        snap = snapshot(state, extra)
        if snap is None or not self._busy.acquire(blocking=False):
            return
        self.last_tip = now
        threading.Thread(
            target=self._generate, args=(snap, died), daemon=True
        ).start()

    def _generate(self, snap: dict, died: bool) -> None:
        try:
            from . import llm
            prompt = json.dumps(snap)
            if died:
                prompt += "\n(The player just died — advise on the death timer / what to fix.)"
            res = llm.complete(
                SYSTEM, prompt, provider=self.provider, model=self.model,
                max_tokens=100, effort="low", timeout=60,
            )
            if res.get("text"):
                self.announcer.say(res["text"])
            elif res.get("error"):
                print(f"Coach error: {res['error']}", flush=True)
        finally:
            self._busy.release()
