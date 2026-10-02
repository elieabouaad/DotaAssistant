"""Phase D: weakness tracker.

Turns the match history already sitting in stats.db into a personal "what to
practice" report — the between-games training plan. Everything here is a
deterministic query over your own data; no network, no LLM. Each finding pairs a
number with a concrete recommendation, and the report leads with the single
highest-severity focus so there's always one clear thing to work on.

Signals mined:
- **Laning (CS@10)** — share of games you came out of lane behind your hero's
  target pace (the same target the live Phase-B coach uses).
- **Death timing** — which 10-minute window you die in most, and deaths/game.
- **Gold discipline** — how much gold you carry into the grave on average
  (dying rich = items unbought / greed).
- **Closing games** — win rate by match length (throwing leads vs. falling off).
- **Hero pool** — your best and worst heroes by win rate.
"""

from __future__ import annotations

MIN_HERO_GAMES = 2       # don't judge a hero on a single game
BEHIND_LANE = 0.85       # CS@10 below this * target = "lost lane" (matches Phase B)


def _cs_finding(conn) -> dict | None:
    rows = conn.execute(
        "SELECT hero_id, cs_at_10 FROM matches WHERE cs_at_10 IS NOT NULL AND hero_id IS NOT NULL"
    ).fetchall()
    if not rows:
        return None
    from . import laning
    targets: dict[int, int] = {}
    behind = 0
    deficit_sum = 0
    for r in rows:
        hid = r["hero_id"]
        if hid not in targets:
            targets[hid] = round(laning.target_pace(hid) * 10)
        target = targets[hid] or 1
        if r["cs_at_10"] < target * BEHIND_LANE:
            behind += 1
        deficit_sum += max(0, target - r["cs_at_10"])
    n = len(rows)
    pct = round(100 * behind / n)
    avg_deficit = round(deficit_sum / n)
    sev = "high" if pct >= 50 else "med" if pct >= 30 else "low"
    return {
        "kind": "laning",
        "title": "Laning / CS@10",
        "metric": f"{pct}% of games left lane behind pace",
        "detail": (f"You came out of the lane under your CS target in {behind} of {n} games "
                   f"(avg {avg_deficit} creeps short). Consistent last-hitting is one of the "
                   f"fastest MMR gains — freeze the lane and prioritise the last hit over "
                   f"harass."),
        "severity": sev,
    }


def _death_findings(conn) -> list[dict]:
    out: list[dict] = []
    games = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"] or 1

    clocks = [r["clock"] for r in conn.execute(
        "SELECT clock FROM deaths WHERE clock IS NOT NULL").fetchall()]
    if clocks:
        buckets = {"0-10": 0, "10-20": 0, "20-30": 0, "30-40": 0, "40+": 0}
        for c in clocks:
            m = c / 60
            key = "0-10" if m < 10 else "10-20" if m < 20 else "20-30" if m < 30 \
                else "30-40" if m < 40 else "40+"
            buckets[key] += 1
        worst = max(buckets, key=buckets.get)
        dpg = round(len(clocks) / games, 1)
        sev = "high" if dpg >= 8 else "med" if dpg >= 6 else "low"
        out.append({
            "kind": "deaths",
            "title": "Deaths",
            "metric": f"{dpg} deaths/game, worst window {worst} min",
            "detail": (f"Most of your deaths land in the {worst}-minute window "
                       f"({buckets[worst]} of {len(clocks)}). Buy detection, hold a TP, and "
                       f"respect the map in that window — check where you overextend."),
            "severity": sev,
            "buckets": buckets,
        })

    gold = [r["gold"] for r in conn.execute(
        "SELECT gold FROM deaths WHERE gold IS NOT NULL").fetchall()]
    if gold:
        avg_gold = round(sum(gold) / len(gold))
        if avg_gold >= 800:
            out.append({
                "kind": "gold",
                "title": "Gold discipline",
                "metric": f"~{avg_gold} gold carried into death",
                "detail": (f"You die holding ~{avg_gold} unspent gold on average. Buy before you "
                           f"fight (or a courier back-buy) — gold in the grave risks a big bounty "
                           f"and buys nothing."),
                "severity": "med" if avg_gold >= 1200 else "low",
            })
    return out


def _closing_finding(conn) -> dict | None:
    rows = conn.execute(
        "SELECT win, duration_s FROM matches WHERE win IS NOT NULL AND duration_s IS NOT NULL"
    ).fetchall()
    if len(rows) < 4:
        return None
    early = [r for r in rows if r["duration_s"] < 35 * 60]
    late = [r for r in rows if r["duration_s"] >= 35 * 60]
    if not early or not late:
        return None
    ewr = round(100 * sum(r["win"] for r in early) / len(early))
    lwr = round(100 * sum(r["win"] for r in late) / len(late))
    if ewr - lwr >= 20:
        return {"kind": "closing", "title": "Closing games out",
                "metric": f"{ewr}% win under 35min vs {lwr}% after",
                "detail": (f"You win {ewr}% of games under 35 minutes but only {lwr}% after — you're "
                           f"letting leads slip. Take objectives (towers, Roshan) with a lead instead "
                           f"of farming; end before the enemy carries come online."),
                "severity": "high"}
    if lwr - ewr >= 20:
        return {"kind": "closing", "title": "Early tempo",
                "metric": f"{lwr}% win after 35min vs {ewr}% before",
                "detail": (f"You win {lwr}% of long games but only {ewr}% of short ones — you start "
                           f"slow. Push your laning/early tempo so you're not always playing catch-up."),
                "severity": "med"}
    return None


def _hero_pool(conn) -> dict:
    rows = conn.execute(
        """SELECT hero, COUNT(*) games,
                  SUM(CASE WHEN win=1 THEN 1 ELSE 0 END) wins,
                  AVG(kills) k, AVG(deaths) d, AVG(assists) a
           FROM matches WHERE win IS NOT NULL
           GROUP BY hero HAVING games >= ?""",
        (MIN_HERO_GAMES,),
    ).fetchall()
    heroes = [{"hero": r["hero"], "games": r["games"],
               "winrate": round(100 * r["wins"] / r["games"]),
               "kda": f"{r['k']:.0f}/{r['d']:.0f}/{r['a']:.0f}"} for r in rows]
    heroes.sort(key=lambda h: (h["winrate"], h["games"]), reverse=True)
    return {"best": heroes[:5], "worst": [h for h in heroes[::-1] if h["winrate"] < 50][:5]}


def report(conn) -> dict:
    games = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]
    if not games:
        return {"games": 0, "findings": [], "hero_pool": {"best": [], "worst": []}}

    findings: list[dict] = []
    cs = _cs_finding(conn)
    if cs:
        findings.append(cs)
    findings += _death_findings(conn)
    closing = _closing_finding(conn)
    if closing:
        findings.append(closing)

    order = {"high": 0, "med": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return {
        "games": games,
        "focus": findings[0] if findings else None,  # the single thing to work on
        "findings": findings,
        "hero_pool": _hero_pool(conn),
    }
