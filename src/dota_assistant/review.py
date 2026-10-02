"""Phase C: post-game deep review.

Between-game learning is the highest-leverage MMR lever, and it's where the full
data finally exists. This module merges two sources:

- **OpenDota** (`/matches/{id}`) — the whole match: all ten players, their hero
  names (GSI never exposed the other nine), final K/D/A, net worth, and — even
  on an *unparsed* match — per-hero **benchmark percentiles** (how your GPM / XPM
  / last-hits / hero-damage rank for that hero across all games).
- **Your local `stats.db`** — the lane benchmark (CS@10), the net-worth/deaths
  curves, the death map, and your build order with timings, all captured live
  from GSI.

From the merge it computes a deterministic analysis (lane outcome vs your CS
target, benchmark percentiles, where you sat among the ten players, recurring
patterns across recent games) and — when Anthropic credentials are present —
asks Claude for a short, concrete "what to fix next game" review on top.

User-initiated (History tab button), so there's no surprise API cost. Unparsed
matches still review fine; we also fire a one-off parse request so a later
re-review can add lane_role / gold_t / purchase-log detail.

Stratz upgrade path (needs an API key): lane-phase outcomes and per-minute
lane_role data without waiting on OpenDota's parse queue.
"""

from __future__ import annotations

import json
import time

import requests

from . import config, draft, laning, stats

API = draft.API

# Benchmark stats we surface as percentiles, with friendly labels.
_BENCH_LABELS = {
    "gold_per_min": "GPM",
    "xp_per_min": "XPM",
    "last_hits_per_min": "Last hits/min",
    "hero_damage_per_min": "Hero dmg/min",
    "hero_healing_per_min": "Healing/min",
    "tower_damage": "Tower dmg",
}


# --------------------------------------------------------------------------- #
# OpenDota match fetch
# --------------------------------------------------------------------------- #

def fetch_match(match_id: str, force: bool = False) -> dict:
    """GET the OpenDota match. Cached; parsed matches are immutable so they're
    cached long. For an unparsed match we fire a one-off parse request so a
    later re-review can be richer.

    force=True skips the "unparsed, fetched recently" guard. Without it a user
    hitting Regenerate within 6 hours got the same stale unparsed payload back
    and nothing appeared to happen.
    """
    cache = config.cache_dir() / f"matches_{match_id}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 30 * 86400:
        try:
            data = json.loads(cache.read_text())
            if data.get("version"):  # fully parsed — never changes again
                return data
            if not force and time.time() - cache.stat().st_mtime < 6 * 3600:
                return data  # unparsed, but fetched recently — don't hammer
        except (json.JSONDecodeError, OSError):
            pass
    resp = requests.get(f"{API}/matches/{match_id}", timeout=25)
    resp.raise_for_status()
    data = resp.json()
    cache.write_text(json.dumps(data))
    if not data.get("version"):
        try:  # ask OpenDota to parse it for next time (best-effort, non-blocking)
            requests.post(f"{API}/request/{match_id}", timeout=8)
        except Exception:
            pass
    return data


# --------------------------------------------------------------------------- #
# Deterministic analysis
# --------------------------------------------------------------------------- #

def _net_worth(p: dict) -> int:
    return p.get("net_worth") or p.get("total_gold") or 0


def _scoreboard(od: dict, my_is_radiant: bool) -> list[dict]:
    all_heroes = draft.heroes()
    rows = []
    for p in od.get("players", []):
        hid = p.get("hero_id")
        hero = all_heroes.get(hid, {}).get("localized_name", f"Hero {hid}")
        rows.append({
            "hero": hero,
            "side": "ally" if p.get("isRadiant") == my_is_radiant else "enemy",
            "k": p.get("kills"), "d": p.get("deaths"), "a": p.get("assists"),
            "gpm": p.get("gold_per_min"), "net_worth": _net_worth(p),
            "last_hits": p.get("last_hits"), "hero_damage": p.get("hero_damage"),
        })
    rows.sort(key=lambda r: r["net_worth"], reverse=True)
    return rows


def _percentiles(me: dict) -> list[dict]:
    out = []
    for key, label in _BENCH_LABELS.items():
        bench = (me.get("benchmarks") or {}).get(key) or {}
        pct, raw = bench.get("pct"), bench.get("raw")
        # A zero raw value (e.g. a carry's healing) has a meaningless percentile.
        if pct is not None and raw:
            out.append({"stat": label, "pct": round(pct * 100), "raw": raw})
    return out


def _lane_read(local_match: dict, hero_id: int) -> dict | None:
    """Lane outcome from your captured CS@10 vs your hero's lane target."""
    cs10 = local_match.get("cs_at_10")
    if cs10 is None:
        return None
    target = round(laning.target_pace(hero_id) * 10)
    ratio = cs10 / target if target else 1
    outcome = "won" if ratio >= 1.05 else "lost" if ratio < 0.8 else "even"
    return {"cs_at_10": cs10, "target": target, "outcome": outcome,
            "gpm_at_10": local_match.get("gpm_at_10"),
            "level_at_10": local_match.get("level_at_10")}


def _recent_context(conn, hero: str) -> dict:
    """Patterns across your recent games, so the review can spot recurring
    problems, not just this-match ones."""
    rows = conn.execute(
        """SELECT win, kills, deaths, assists, cs_at_10, gpm, hero
           FROM matches ORDER BY COALESCE(started_at,0) DESC LIMIT 12"""
    ).fetchall()
    rows = [dict(r) for r in rows]
    if not rows:
        return {}
    n = len(rows)

    def avg(k):
        return round(sum((r[k] or 0) for r in rows) / n, 1)

    cs_rows = [r for r in rows if r["cs_at_10"] is not None]
    return {
        "games": n,
        "winrate": round(100 * sum(1 for r in rows if r["win"] == 1) / n),
        "avg_deaths": avg("deaths"),
        "avg_cs_at_10": round(sum(r["cs_at_10"] for r in cs_rows) / len(cs_rows)) if cs_rows else None,
        "hero_games": sum(1 for r in rows if r["hero"] == hero),
    }


def analyze(match_id: str) -> dict:
    """Merge OpenDota + local stats.db into a structured, deterministic review."""
    conn = stats.connect()
    try:
        local = stats.match_detail(conn, match_id)
        if not local:
            return {"error": "This match isn't in your local history yet."}
        m = local["match"]
        hero_id = m.get("hero_id")

        od, od_error = {}, None
        try:
            od = fetch_match(match_id)
        except Exception as exc:
            od_error = str(exc)

        me = next((p for p in od.get("players", []) if p.get("hero_id") == hero_id), None)
        my_is_radiant = (m.get("team") == "radiant")

        analysis = {
            "match_id": match_id,
            "hero": m.get("hero"),
            "win": m.get("win"),
            "match_type": m.get("match_type", "Unknown"),
            "duration_s": m.get("duration_s"),
            "kda": {"k": m.get("kills"), "d": m.get("deaths"), "a": m.get("assists")},
            "gpm": m.get("gpm"), "xpm": m.get("xpm"),
            "lane": _lane_read(m, hero_id),
            "percentiles": _percentiles(me) if me else [],
            "scoreboard": _scoreboard(od, my_is_radiant) if od.get("players") else [],
            "deaths": len(local.get("deaths", [])),
            "build": [{"item": it["item"], "min": round((it["first_seen"] or 0) / 60, 1)}
                      for it in local.get("items", [])],
            "recent": _recent_context(conn, m.get("hero")),
            "parsed": bool(od.get("version")),
            "od_error": od_error,
        }
        return analysis
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Claude narrative (optional)
# --------------------------------------------------------------------------- #

def _persona() -> str:
    """One sentence describing the player, from [player] in config.toml —
    bracket, roles, and self-identified focus areas — so the coaching is
    calibrated to them. Neutral when nothing is configured."""
    p = config.section("player")
    bracket = p.get("bracket") or ""
    label = draft.BRACKETS.get(bracket, (None, ""))[1] if bracket != "all" else ""
    who = f"a {label} player" if label else "an amateur player looking to improve"
    roles = [str(r) for r in (p.get("roles") or []) if r]
    if roles:
        who += f" who mains {' and '.join(roles)}"
    focus = [str(f) for f in (p.get("focus") or []) if f]
    if focus:
        who += f"; their self-identified focus areas are {' and '.join(focus)}"
    return who


def _system_prompt() -> str:
    return (
    f"You are a Dota 2 coach reviewing a finished match for {_persona()}. "
    "You get a JSON analysis merging OpenDota (all ten players, "
    "benchmark percentiles where pct is the hero's percentile rank) with their "
    "own captured lane CS@10, build order, and recent-game patterns.\n\n"
    "IMPORTANT — read `match_type` first and judge accordingly:\n"
    "- 'Turbo': ~2x gold/XP and faster timings, so GPM/CS numbers run much "
    "higher than normal — do NOT praise inflated farm or scold benchmarks meant "
    "for normal games; focus on decisions, deaths, and itemisation.\n"
    "- 'Ranked'/'Unranked': normal economy — benchmarks apply directly.\n"
    "- 'Bots'/'Lobby'/'Tournament': low signal — keep advice light and general.\n"
    "Weight your coaching to what actually matters for that mode.\n\n"
    "Write a tight review in Markdown with EXACTLY these sections:\n"
    "**Verdict** — one sentence.\n"
    "**What went well** — 1-2 bullets, specific to the data.\n"
    "**Top 3 fixes** — a numbered list, each fix concrete, tied to a number in "
    "the data, and appropriate for their bracket (fundamentals over pro theory). "
    "Prioritise their stated focus areas when the data supports it.\n"
    "**Practice next game** — one measurable focus (e.g. a CS@10 target).\n\n"
    "Under 280 words. No preamble. Reference real numbers from the JSON."
    )


def claude_review(analysis: dict, cfg: dict | None = None) -> dict:
    """Ask Claude for a written review. Uses your Claude Code login by default
    (no API key needed); falls back to ANTHROPIC_API_KEY. Returns {'text': ...}
    or {'error': ...} — the deterministic analysis stands alone without it."""
    from . import llm
    cfg = cfg or config.section("coach")
    return llm.complete(
        _system_prompt(), json.dumps(analysis, default=str),
        provider=cfg.get("provider", "auto"), model=cfg.get("model"),
        max_tokens=900, effort="medium",
    )


_BUILD_TRIVIAL = {
    "tango", "branches", "clarity", "faerie_fire", "enchanted_mango", "tpscroll",
    "ward_observer", "ward_sentry", "ward_dispenser", "dust", "smoke_of_deceit",
    "flask", "healing_salve", "tome_of_knowledge", "blood_grenade",
}


def _tile_to_world(v: int) -> int:
    """OpenDota position tiles (~64..192) -> Dota world coords (-8300..8300),
    matching the death-map's coordinate space so points overlay correctly."""
    return round(((v - 64) / 128) * 16600 - 8300)


def opendota_detail(match_id: str, base: dict | None = None,
                    force: bool = False) -> dict:
    """Enrich an OpenDota-only match with parsed data (net-worth/last-hit curves,
    build order, and a partial teamfight death map). If the match isn't parsed
    yet, fetch_match requests a parse and we flag it so the UI can offer a retry.
    """
    if base is None:
        conn = stats.connect()
        try:
            base = stats._od_match_row(conn, match_id)
        finally:
            conn.close()
    if not base:
        return {"error": "This match isn't in your OpenDota history."}

    try:
        data = fetch_match(match_id, force=force)
    except Exception as exc:
        base["od_error"] = str(exc)
        return base
    if not data.get("version"):
        base["parse_requested"] = True  # fetch_match already asked OpenDota to parse
        return base

    players = data.get("players", [])
    hero_id = base["match"]["hero_id"]
    me = next((p for p in players if p.get("hero_id") == hero_id), None)
    if not me:
        return base

    gold_t = me.get("gold_t") or []
    xp_t = me.get("xp_t") or []
    lh_t = me.get("lh_t") or []
    ts = [{
        "clock": i * 60,
        "net_worth": gold_t[i],
        "xp": xp_t[i] if i < len(xp_t) else None,
        "last_hits": lh_t[i] if i < len(lh_t) else None,
    } for i in range(len(gold_t))]

    from . import itemization
    items, seen = [], set()
    for pur in (me.get("purchase_log") or []):
        key, t = pur.get("key"), pur.get("time")
        if not key or key in _BUILD_TRIVIAL or key.startswith("recipe") or key in seen:
            continue
        seen.add(key)
        items.append({"item": itemization._pretty_item(key), "first_seen": t})
    items.sort(key=lambda it: it["first_seen"] if it["first_seen"] is not None else 0)

    # Partial death map: OpenDota only records death positions inside teamfights.
    deaths, dn = [], 0
    my_index = players.index(me)
    for tf in (data.get("teamfights") or []):
        tps = tf.get("players") or []
        if my_index >= len(tps):
            continue
        for xs, col in (tps[my_index].get("deaths_pos") or {}).items():
            for ys, cnt in (col or {}).items():
                for _ in range(cnt or 1):
                    dn += 1
                    deaths.append({"death_number": dn, "clock": tf.get("start"),
                                   "x": _tile_to_world(int(xs)), "y": _tile_to_world(int(ys))})

    base.update({"timeseries": ts, "items": items, "deaths": deaths,
                 "parsed": True, "source": "opendota", "partial_deaths": True})
    return base


def review(match_id: str, with_claude: bool = True) -> dict:
    """Full post-game review: deterministic analysis + optional Claude write-up."""
    analysis = analyze(match_id)
    if "error" in analysis:
        return analysis
    result = {"analysis": analysis}
    if with_claude:
        result["coach"] = claude_review(analysis)
    return result
