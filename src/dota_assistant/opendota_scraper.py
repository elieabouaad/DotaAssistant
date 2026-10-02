"""OpenDota history scraper + improvement report.

Run it and it pulls your entire match history from OpenDota, stores it in the
same stats.db, and prints improvement suggestions computed over your **ranked**
games — a far bigger sample than the handful of games captured live by GSI, so
the weakness read is statistically real.

    dota-assistant scrape                 # uses [player] account_id from config.toml
    dota-assistant scrape 123456789       # or pass an account id
    dota-assistant scrape --no-claude     # skip the written summary (deterministic only)

Needs "Expose Public Match Data" enabled in the Dota 2 client for OpenDota to
see your matches. Everything is cached in stats.db, so re-running is cheap and
incremental (matches are upserted by id).

Improvement suggestions are deterministic (farming vs hero benchmarks, deaths,
role win rate, hero pool, closing games); a Claude write-up is layered on top
when ANTHROPIC_API_KEY is set, same as the post-game review.
"""

from __future__ import annotations

import json
import sys

import requests

from . import config, laning, stats

# Windows consoles default to cp1252 and can't encode the em-dashes / ellipses
# in the report, which would crash print(). Emit UTF-8, degrading unprintable
# characters rather than raising (same guard server.py uses).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

API = "https://api.opendota.com/api"
RANKED_LOBBY = 7  # OpenDota lobby_type for ranked matchmaking

# Fields projected from the player_matches table (one call, no per-match fetch).
_PROJECT = [
    "hero_id", "kills", "deaths", "assists", "last_hits", "denies",
    "gold_per_min", "xp_per_min", "duration", "lobby_type", "game_mode",
    "start_time", "radiant_win", "player_slot", "average_rank", "lane_role",
    "party_size",
]

LANE_ROLES = {1: "Safelane (carry)", 2: "Mid", 3: "Offlane", 4: "Jungle"}

OD_SCHEMA = """
CREATE TABLE IF NOT EXISTS od_matches (
    match_id     INTEGER PRIMARY KEY,
    account_id   INTEGER,
    hero_id      INTEGER,
    win          INTEGER,
    duration     INTEGER,
    lobby_type   INTEGER,
    game_mode    INTEGER,
    lane_role    INTEGER,
    kills        INTEGER, deaths INTEGER, assists INTEGER,
    last_hits    INTEGER, denies INTEGER,
    gpm          INTEGER, xpm INTEGER,
    start_time   INTEGER,
    average_rank INTEGER,
    party_size   INTEGER
);
"""


# --------------------------------------------------------------------------- #
# fetch + store
# --------------------------------------------------------------------------- #

def _account_id(argv: list[str]) -> int:
    for a in argv:
        if a.isdigit():
            return int(a)
    return int(config.section("player").get("account_id") or 0)


def _is_win(radiant_win: bool, player_slot: int) -> int:
    return 1 if (player_slot < 128) == bool(radiant_win) else 0


def fetch_all(account_id: int) -> list[dict]:
    """Every match on the account (one projected call).

    significant=0 is essential: OpenDota defaults to significant=1, which hides
    every non-standard game mode — Turbo above all. On a Turbo-heavy account
    that silently drops more than half the history.
    """
    params = [("project", p) for p in _PROJECT] + [("significant", "0")]
    resp = requests.get(f"{API}/players/{account_id}/matches", params=params, timeout=90)
    resp.raise_for_status()
    return resp.json()


def store(conn, account_id: int, matches: list[dict]) -> int:
    conn.executescript(OD_SCHEMA)
    rows = [(
        m.get("match_id"), account_id, m.get("hero_id"),
        _is_win(m.get("radiant_win"), m.get("player_slot") or 0),
        m.get("duration"), m.get("lobby_type"), m.get("game_mode"), m.get("lane_role"),
        m.get("kills"), m.get("deaths"), m.get("assists"),
        m.get("last_hits"), m.get("denies"), m.get("gold_per_min"), m.get("xp_per_min"),
        m.get("start_time"), m.get("average_rank"), m.get("party_size"),
    ) for m in matches if m.get("match_id") is not None]
    conn.executemany(
        "INSERT OR REPLACE INTO od_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------- #
# analysis (ranked only)
# --------------------------------------------------------------------------- #

def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def analyze(conn, account_id: int) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM od_matches WHERE account_id=? AND lobby_type=?",
        (account_id, RANKED_LOBBY),
    ).fetchall()]
    n = len(rows)
    if not n:
        return {"ranked_games": 0}

    wins = sum(r["win"] for r in rows)

    def avg(k):
        return round(sum((r[k] or 0) for r in rows) / n, 1)

    # rough bracket from OpenDota's average_rank (rank_tier*10-ish)
    ranks = [r["average_rank"] for r in rows if r["average_rank"]]
    bracket = None
    if ranks:
        med = sorted(ranks)[len(ranks) // 2]
        names = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
                 5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}
        bracket = names.get(med // 10)

    findings: list[dict] = []
    findings += _farming_finding(rows)
    d = avg("deaths")
    if d >= 7:
        findings.append({"severity": "high" if d >= 8.5 else "med", "title": "Deaths",
                         "metric": f"{d} deaths/game",
                         "detail": "You're dying a lot. Buy detection, hold a TP, and stop "
                                   "taking greedy fights you don't need — every death is ~30s of "
                                   "map control handed over."})
    findings += _role_finding(rows)
    findings += _closing_finding(rows)

    order = {"high": 0, "med": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))

    return {
        "account_id": account_id,
        "bracket": bracket,
        "ranked_games": n,
        "winrate": _pct(wins, n),
        "avg_kda": f"{avg('kills')}/{avg('deaths')}/{avg('assists')}",
        "avg_gpm": round(avg("gpm")),
        "avg_last_hits": round(avg("last_hits")),
        "focus": findings[0] if findings else None,
        "findings": findings,
        "hero_pool": _hero_pool(rows),
        "role_split": _role_split(rows),
    }


def _hero_median(hero_id: int, stat: str) -> float | None:
    try:
        data = laning._benchmarks(hero_id)
        return laning._percentile_value(data.get("result", {}).get(stat, []), 0.5)
    except Exception:
        return None


def _farming_finding(rows: list[dict]) -> list[dict]:
    """Compare last-hits/min and GPM on your most-played heroes to the hero's
    benchmark median."""
    by_hero: dict[int, list[dict]] = {}
    for r in rows:
        if r["hero_id"] and r["duration"]:
            by_hero.setdefault(r["hero_id"], []).append(r)
    ranked_heroes = sorted(by_hero.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]

    from . import draft
    below = 0
    checked = 0
    worst = None
    heroes = draft.heroes()
    for hid, hrows in ranked_heroes:
        if len(hrows) < 4:
            continue
        med_lh = _hero_median(hid, "last_hits_per_min")
        if not med_lh:
            continue
        my_lh = sum(r["last_hits"] / (r["duration"] / 60) for r in hrows) / len(hrows)
        checked += 1
        deficit = med_lh - my_lh
        if my_lh < med_lh * 0.9:
            below += 1
            if worst is None or deficit > worst[1]:
                worst = (heroes.get(hid, {}).get("localized_name", f"Hero {hid}"), deficit, round(my_lh, 1), round(med_lh, 1))
    if not checked:
        return []
    if below == 0:
        return [{"severity": "low", "title": "Farming", "metric": "at or above benchmark",
                 "detail": "Your last-hitting is on par with the hero benchmark across your "
                           "most-played heroes — keep it up."}]
    sev = "high" if below >= checked / 2 else "med"
    w = worst
    detail = (f"On {below} of your {checked} most-played heroes you last-hit below the hero "
              f"benchmark.")
    if w:
        detail += (f" Worst: {w[0]} — {w[2]} LH/min vs a {w[3]} benchmark. "
                   f"Prioritise the last hit over harass and cut jungle detours in the laning phase.")
    return [{"severity": sev, "title": "Farming / last hits", "metric": f"below benchmark on {below}/{checked} heroes",
             "detail": detail}]


def _role_finding(rows: list[dict]) -> list[dict]:
    split = _role_split(rows)
    played = [s for s in split if s["games"] >= 8]
    if len(played) < 2:
        return []
    best = max(played, key=lambda s: s["winrate"])
    worst = min(played, key=lambda s: s["winrate"])
    if best["winrate"] - worst["winrate"] >= 12:
        return [{"severity": "med", "title": "Role win rate",
                 "metric": f"{best['role']} {best['winrate']}% vs {worst['role']} {worst['winrate']}%",
                 "detail": f"You win far more from {best['role']} ({best['winrate']}%) than "
                           f"{worst['role']} ({worst['winrate']}%). When you can pick your role, "
                           f"lean into {best['role']} to climb faster."}]
    return []


def _role_split(rows: list[dict]) -> list[dict]:
    out = []
    for code, name in LANE_ROLES.items():
        rr = [r for r in rows if r["lane_role"] == code]
        if rr:
            out.append({"role": name, "games": len(rr),
                        "winrate": _pct(sum(r["win"] for r in rr), len(rr))})
    return sorted(out, key=lambda s: s["games"], reverse=True)


def _closing_finding(rows: list[dict]) -> list[dict]:
    early = [r for r in rows if r["duration"] and r["duration"] < 35 * 60]
    late = [r for r in rows if r["duration"] and r["duration"] >= 35 * 60]
    if len(early) < 10 or len(late) < 10:
        return []
    ewr, lwr = _pct(sum(r["win"] for r in early), len(early)), _pct(sum(r["win"] for r in late), len(late))
    if ewr - lwr >= 12:
        return [{"severity": "med", "title": "Closing games out",
                 "metric": f"{ewr}% under 35min vs {lwr}% after",
                 "detail": f"You win {ewr}% of games under 35 minutes but only {lwr}% after — take "
                           f"objectives with a lead (towers, Roshan) instead of farming it out."}]
    if lwr - ewr >= 12:
        return [{"severity": "med", "title": "Early tempo",
                 "metric": f"{lwr}% after 35min vs {ewr}% before",
                 "detail": f"You win {lwr}% of long games but only {ewr}% of short ones — you start "
                           f"slow. Sharpen your laning and early tempo."}]
    return []


def _hero_pool(rows: list[dict]) -> dict:
    from . import draft
    heroes = draft.heroes()
    by_hero: dict[int, list[dict]] = {}
    for r in rows:
        if r["hero_id"]:
            by_hero.setdefault(r["hero_id"], []).append(r)
    pool = []
    for hid, hrows in by_hero.items():
        if len(hrows) < 5:
            continue
        pool.append({"hero": heroes.get(hid, {}).get("localized_name", f"Hero {hid}"),
                     "games": len(hrows),
                     "winrate": _pct(sum(r["win"] for r in hrows), len(hrows))})
    pool.sort(key=lambda h: (h["winrate"], h["games"]), reverse=True)
    return {"best": pool[:6], "worst": [h for h in pool[::-1] if h["winrate"] < 48][:6]}


# --------------------------------------------------------------------------- #
# Claude write-up (optional)
# --------------------------------------------------------------------------- #

SYSTEM = (
    "You are a Dota 2 improvement coach. You get a JSON summary of a player's "
    "entire ranked history (win rate, average stats, benchmark-based findings, "
    "role split, hero pool). Write a concise plan in Markdown:\n"
    "**Where you're at** — one sentence.\n"
    "**Biggest levers** — 3 numbered fixes, each tied to a number in the data, "
    "ordered by impact; focus on fundamentals (laning, farming, dying less).\n"
    "**Hero pool** — one line on which heroes to spam and which to drop.\n"
    "**This week** — one measurable goal.\n"
    "Under 300 words. Reference real numbers. No preamble."
)


def claude_suggestions(analysis: dict) -> dict:
    """Written plan from Claude — via your Claude Code login by default (no API
    key), falling back to ANTHROPIC_API_KEY."""
    from . import llm
    cfg = config.section("coach")
    return llm.complete(
        SYSTEM, json.dumps(analysis, default=str),
        provider=cfg.get("provider", "auto"), model=cfg.get("model"),
        max_tokens=900, effort="medium",
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_report(a: dict, coach: dict | None) -> None:
    bar = "=" * 60
    print(bar)
    print(f"OpenDota ranked review — account {a['account_id']}"
          + (f"  ({a['bracket']})" if a.get("bracket") else ""))
    print(bar)
    print(f"Ranked games: {a['ranked_games']}   Win rate: {a['winrate']}%   "
          f"Avg KDA: {a['avg_kda']}   GPM: {a['avg_gpm']}")
    if a.get("focus"):
        print(f"\n>>> TOP FOCUS: {a['focus']['title']} — {a['focus']['metric']}")
        print(f"    {a['focus']['detail']}")
    print("\nFindings:")
    for f in a["findings"]:
        print(f"  [{f['severity']:>4}] {f['title']}: {f['metric']}")
        print(f"         {f['detail']}")
    if a["role_split"]:
        print("\nRole win rate:")
        for s in a["role_split"]:
            print(f"  {s['role']:<20} {s['winrate']:>3}%  ({s['games']} games)")
    hp = a["hero_pool"]
    if hp["best"]:
        print("\nSpam these (best win rate):")
        for h in hp["best"]:
            print(f"  {h['hero']:<18} {h['winrate']:>3}%  ({h['games']}g)")
    if hp["worst"]:
        print("\nConsider dropping (<48%):")
        for h in hp["worst"]:
            print(f"  {h['hero']:<18} {h['winrate']:>3}%  ({h['games']}g)")
    if coach:
        print("\n" + bar + "\nCOACH\n" + bar)
        print(coach.get("text") or f"(no write-up: {coach.get('error')})")
    print()


def main(argv: list[str]) -> None:
    account_id = _account_id(argv)
    if not account_id:
        print("No account id. Set [player] account_id in config.toml or pass it: "
              "dota-assistant scrape <account_id>")
        return
    print(f"Fetching match history for {account_id} from OpenDota…", flush=True)
    matches = fetch_all(account_id)
    conn = stats.connect()
    try:
        stored = store(conn, account_id, matches)
        print(f"Stored {stored} matches ({sum(1 for m in matches if m.get('lobby_type') == RANKED_LOBBY)} ranked).")
        a = analyze(conn, account_id)
    finally:
        conn.close()
    if not a.get("ranked_games"):
        print("No ranked games found (is 'Expose Public Match Data' enabled?).")
        return
    coach = None if "--no-claude" in argv else claude_suggestions(a)
    _print_report(a, coach)


if __name__ == "__main__":
    main(sys.argv[1:])
