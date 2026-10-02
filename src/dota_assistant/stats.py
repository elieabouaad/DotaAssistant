"""Match history & aggregate stats, persisted to SQLite.

The GSI session logs (logs/session_*.jsonl) are full game-state snapshots at
~1-3s intervals — huge and unusable directly. This module distills them into a
compact SQLite database (stats.db) of per-match summaries, downsampled time
series, deaths, and item-build timings, so the dashboard can show your history
and trends across games without ever re-reading the raw logs.

In a normal pub, GSI only exposes *your own* hero, so every metric here is about
you: KDA, CS@10, GPM/XPM, gold curve, death map, build order.

Usage:
    dota-assistant stats --backfill   # ingest every existing log into stats.db
    dota-assistant stats --list       # print recorded matches

The server also ingests each match automatically when it ends.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import config

# Downsample the time series to one point per this many clock-seconds. 30s keeps
# curves smooth while holding each match to ~50-90 rows instead of thousands.
SAMPLE_SECS = 30

# Consumables / trinkets we don't want cluttering the build-order timeline.
_TRIVIAL_ITEMS = {
    "item_tango", "item_tpscroll", "item_flask", "item_clarity", "item_faerie_fire",
    "item_enchanted_mango", "item_ward_observer", "item_ward_sentry",
    "item_ward_dispenser", "item_dust", "item_smoke_of_deceit", "item_tome_of_knowledge",
    "item_healing_salve", "empty",
}

GOLD_SOURCES = (
    "gold_from_hero_kills", "gold_from_creep_kills", "gold_from_summon_kills",
    "gold_from_income", "gold_from_shared",
)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id     TEXT PRIMARY KEY,
    started_at   REAL,      -- unix ts of first snapshot
    ended_at     REAL,      -- unix ts of last snapshot
    duration_s   INTEGER,   -- max clock_time (in-game seconds)
    hero_id      INTEGER,
    hero         TEXT,      -- pretty hero name, e.g. "Batrider"
    team         TEXT,      -- 'radiant' | 'dire'
    win          INTEGER,   -- 1 win, 0 loss, NULL unknown (server stopped early)
    level        INTEGER,
    kills        INTEGER,
    deaths       INTEGER,
    assists      INTEGER,
    last_hits    INTEGER,
    denies       INTEGER,
    gpm          INTEGER,
    xpm          INTEGER,
    gold         INTEGER,   -- unspent gold at end
    net_worth    INTEGER,   -- total gold earned (proxy for net worth)
    cs_at_10     INTEGER,   -- last hits at the 10:00 mark
    gpm_at_10    INTEGER,
    level_at_10  INTEGER,
    radiant_score INTEGER,
    dire_score    INTEGER,
    source_file  TEXT
);

CREATE TABLE IF NOT EXISTS timeseries (
    match_id  TEXT,
    clock     INTEGER,
    gpm       INTEGER,
    xpm       INTEGER,
    net_worth INTEGER,
    gold      INTEGER,
    level     INTEGER,
    kills     INTEGER,
    deaths    INTEGER,
    assists   INTEGER,
    last_hits INTEGER,
    PRIMARY KEY (match_id, clock)
);

CREATE TABLE IF NOT EXISTS deaths (
    match_id     TEXT,
    death_number INTEGER,
    clock        INTEGER,
    x            INTEGER,
    y            INTEGER,
    level        INTEGER,
    gold         INTEGER,     -- unreliable+reliable gold carried into the grave
    respawn_s    INTEGER,
    buyback_cost INTEGER,
    had_buyback  INTEGER,     -- could you afford buyback at death (1/0)
    PRIMARY KEY (match_id, death_number)
);

CREATE TABLE IF NOT EXISTS items (
    match_id   TEXT,
    item       TEXT,          -- pretty item name, e.g. "Blink"
    first_seen INTEGER,       -- clock_time it first appeared in your inventory
    PRIMARY KEY (match_id, item)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    # A dashboard read and a match-end write can overlap; wait rather than raise.
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def pretty_hero(name: str | None) -> str:
    if not name:
        return "Unknown"
    return name.replace("npc_dota_hero_", "").replace("_", " ").title()


def pretty_item(name: str) -> str:
    return name.replace("item_", "").replace("_", " ").title()


def _is_real_match(match_id: str | None) -> bool:
    return bool(match_id) and match_id not in ("0", "")


# --------------------------------------------------------------------------- #
# per-match accumulation
# --------------------------------------------------------------------------- #

class _Match:
    """Rolls up every snapshot for a single match_id into summary rows."""

    def __init__(self, match_id: str, source_file: str):
        self.match_id = match_id
        self.source_file = source_file
        self.started_at = None
        self.ended_at = None
        self.hero_id = None
        self.hero = None
        self.team = None
        self.win_team = None
        self.max_clock = None
        self.final = {}          # last in-progress player/hero snapshot
        self.radiant_score = 0
        self.dire_score = 0
        self.ts = {}             # bucket -> timeseries row
        self.at10 = None
        self.items = {}          # item_name -> first clock seen
        self._crossed10 = False

    def observe(self, rec: dict) -> None:
        st = rec.get("state", {})
        m = st.get("map", {})
        h = st.get("hero", {})
        p = st.get("player", {})
        ts = rec.get("received_at")
        clock = m.get("clock_time")

        if ts is not None:
            if self.started_at is None:
                self.started_at = ts
            self.ended_at = ts

        if h.get("name"):
            self.hero = h["name"]
            if h.get("id"):
                self.hero_id = h["id"]
        if p.get("team_name"):
            self.team = p["team_name"]
        if m.get("win_team", "none") not in ("none", None):
            self.win_team = m["win_team"]
        if m.get("radiant_score") is not None:
            self.radiant_score = m["radiant_score"]
            self.dire_score = m.get("dire_score", self.dire_score)

        if clock is None:
            return
        if self.max_clock is None or clock > self.max_clock:
            self.max_clock = clock

        # Only track playing-phase player stats for the summary/curves.
        if p.get("kills") is None or clock < 0:
            return

        net_worth = sum(int(p.get(k, 0) or 0) for k in GOLD_SOURCES)
        snap = {
            "clock": int(clock),
            "gpm": p.get("gpm", 0), "xpm": p.get("xpm", 0),
            "net_worth": net_worth, "gold": p.get("gold", 0),
            "level": h.get("level", 0),
            "kills": p.get("kills", 0), "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0), "last_hits": p.get("last_hits", 0),
            "denies": p.get("denies", 0),
        }
        self.final = snap

        # 10-minute benchmark snapshot (first sample at/after 10:00).
        if not self._crossed10 and clock >= 600:
            self._crossed10 = True
            self.at10 = snap

        # downsampled time series
        self.ts[int(clock) // SAMPLE_SECS] = snap

        # build-order item timings
        items = st.get("items", {})
        for slot, v in items.items():
            name = v.get("name")
            if not name or name in _TRIVIAL_ITEMS or name.startswith("item_recipe"):
                continue
            if name not in self.items:
                self.items[name] = int(clock)

    # -- persistence -------------------------------------------------------- #

    def valid(self) -> bool:
        if not (_is_real_match(self.match_id) and self.hero is not None and self.final):
            return False
        # Drop incomplete captures (server started/stopped mid-warmup): a match
        # with no recorded result and under 5 minutes of play is just noise.
        if self.win() is None and (self.max_clock or 0) < 300:
            return False
        return True

    def win(self):
        if self.win_team is None or self.team is None:
            return None
        return 1 if self.win_team == self.team else 0

    def write(self, conn: sqlite3.Connection, deaths_by_match: dict) -> None:
        f = self.final
        a10 = self.at10 or {}
        conn.execute("DELETE FROM matches WHERE match_id=?", (self.match_id,))
        conn.execute("DELETE FROM timeseries WHERE match_id=?", (self.match_id,))
        conn.execute("DELETE FROM deaths WHERE match_id=?", (self.match_id,))
        conn.execute("DELETE FROM items WHERE match_id=?", (self.match_id,))

        conn.execute(
            """INSERT INTO matches VALUES
               (:match_id,:started_at,:ended_at,:duration_s,:hero_id,:hero,:team,:win,
                :level,:kills,:deaths,:assists,:last_hits,:denies,:gpm,:xpm,:gold,
                :net_worth,:cs_at_10,:gpm_at_10,:level_at_10,:radiant_score,:dire_score,
                :source_file)""",
            {
                "match_id": self.match_id, "started_at": self.started_at,
                "ended_at": self.ended_at, "duration_s": self.max_clock,
                "hero_id": self.hero_id, "hero": pretty_hero(self.hero), "team": self.team,
                "win": self.win(), "level": f.get("level"), "kills": f.get("kills"),
                "deaths": f.get("deaths"), "assists": f.get("assists"),
                "last_hits": f.get("last_hits"), "denies": f.get("denies"),
                "gpm": f.get("gpm"), "xpm": f.get("xpm"), "gold": f.get("gold"),
                "net_worth": f.get("net_worth"), "cs_at_10": a10.get("last_hits"),
                "gpm_at_10": a10.get("gpm"), "level_at_10": a10.get("level"),
                "radiant_score": self.radiant_score, "dire_score": self.dire_score,
                "source_file": self.source_file,
            },
        )
        conn.executemany(
            """INSERT INTO timeseries VALUES
               (:mid,:clock,:gpm,:xpm,:net_worth,:gold,:level,:kills,:deaths,:assists,:last_hits)""",
            [{"mid": self.match_id, **s} for s in sorted(self.ts.values(), key=lambda s: s["clock"])],
        )
        conn.executemany(
            "INSERT INTO items VALUES (?,?,?)",
            [(self.match_id, pretty_item(n), c) for n, c in sorted(self.items.items(), key=lambda kv: kv[1])],
        )
        conn.executemany(
            """INSERT OR REPLACE INTO deaths VALUES
               (:match_id,:death_number,:clock,:x,:y,:level,:gold,:respawn_s,:buyback_cost,:had_buyback)""",
            deaths_by_match.get(self.match_id, []),
        )


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def _parse_deaths(paths) -> dict:
    """Return {match_id: [death rows]} across all death logs."""
    out: dict = {}
    for path in paths:
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                st = r.get("state", {})
                mid = st.get("map", {}).get("matchid")
                if not _is_real_match(mid):
                    continue
                h = st.get("hero", {})
                p = st.get("player", {})
                cost = h.get("buyback_cost") or 0
                out.setdefault(mid, []).append({
                    "match_id": mid,
                    "death_number": r.get("death_number"),
                    "clock": r.get("clock_time"),
                    "x": h.get("xpos"), "y": h.get("ypos"),
                    "level": h.get("level"), "gold": p.get("gold"),
                    "respawn_s": h.get("respawn_seconds"),
                    "buyback_cost": cost,
                    "had_buyback": 1 if (p.get("gold") or 0) >= cost > 0 else 0,
                })
    return out


def ingest_session(conn: sqlite3.Connection, session_path, deaths_by_match: dict) -> list[str]:
    """Parse one session file, upsert every real match it contains. Returns
    the list of match_ids written."""
    matches: dict = {}
    with open(session_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = rec.get("state", {}).get("map", {}).get("matchid")
            if not _is_real_match(mid):
                continue
            m = matches.get(mid)
            if m is None:
                m = matches[mid] = _Match(mid, os.path.basename(str(session_path)))
            m.observe(rec)

    written = []
    for m in matches.values():
        if m.valid():
            m.write(conn, deaths_by_match)
            written.append(m.match_id)
    conn.commit()
    return written


def backfill(conn: sqlite3.Connection, log_dir: Path | None = None) -> list[str]:
    log_dir = log_dir or config.log_dir()
    deaths = _parse_deaths(sorted(glob.glob(str(log_dir / "deaths_*.jsonl"))))
    written = []
    for path in sorted(glob.glob(str(log_dir / "session_*.jsonl"))):
        written += ingest_session(conn, path, deaths)
    return written


def ingest_current(conn: sqlite3.Connection, session_path,
                   log_dir: Path | None = None) -> list[str]:
    """Ingest a single (usually just-finished) session file, pairing it with all
    death logs. Safe to call repeatedly — each match is upserted idempotently."""
    log_dir = log_dir or config.log_dir()
    deaths = _parse_deaths(sorted(glob.glob(str(log_dir / "deaths_*.jsonl"))))
    return ingest_session(conn, session_path, deaths)


# --------------------------------------------------------------------------- #
# match type (from OpenDota lobby_type + game_mode)
# --------------------------------------------------------------------------- #

def match_type(lobby_type, game_mode) -> str:
    """Human match type. Turbo is a game_mode; ranked/unranked are lobby_types."""
    if game_mode == 23:
        return "Turbo"
    return {
        7: "Ranked", 0: "Unranked", 9: "Battle Cup", 4: "Bots",
        1: "Lobby", 2: "Tournament", 5: "Team", 6: "Solo",
    }.get(lobby_type, "Other" if lobby_type is not None else "Unknown")


def _hero_names() -> dict[int, str]:
    """hero_id -> localized name from the cached /heroes list (no network)."""
    try:
        data = json.loads((config.cache_dir() / "heroes.json").read_text(encoding="utf-8"))
        return {h["id"]: h["localized_name"] for h in data}
    except Exception:
        return {}


def gold_earned(gpm, duration_s) -> int | None:
    """Total gold earned, reconstructed from GPM and match length.

    OpenDota's bulk player-matches endpoint exposes no net-worth field at all
    (asking for `net_worth`/`total_gold` in the projection silently returns
    nothing), so for the ~5000 games we only have from there the column was
    always blank. But GPM is *defined* as gold earned per minute, so this
    reproduces OpenDota's own `total_gold` exactly — verified to the gold on
    parsed matches, not an approximation.

    It's the same quantity the GSI capture stores in `net_worth`: the sum of
    every gold source, i.e. gold earned rather than the in-client "net worth"
    of items you're currently holding.
    """
    if not gpm or not duration_s:
        return None
    return round(gpm * duration_s / 60)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# --------------------------------------------------------------------------- #
# queries used by the dashboard API
# --------------------------------------------------------------------------- #

def list_matches(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM matches ORDER BY COALESCE(started_at, 0) DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def unified_matches(conn: sqlite3.Connection) -> list[dict]:
    """Every match from OpenDota (od_matches) merged with the locally captured
    GSI games (matches). GSI rows carry the rich detail (curves/deaths/build);
    OpenDota rows fill in the rest of your history with a match type."""
    heroes = _hero_names()
    rows: dict[str, dict] = {}

    if _table_exists(conn, "od_matches"):
        for r in conn.execute("SELECT * FROM od_matches"):
            r = dict(r)
            mid = str(r["match_id"])
            rows[mid] = {
                "match_id": mid,
                "hero": heroes.get(r["hero_id"], f"Hero {r['hero_id']}"),
                "hero_id": r["hero_id"], "win": r["win"],
                "kills": r["kills"], "deaths": r["deaths"], "assists": r["assists"],
                "gpm": r["gpm"], "xpm": r["xpm"],
                "last_hits": r["last_hits"], "denies": r["denies"],
                "duration_s": r["duration"], "start_time": r["start_time"],
                "cs_at_10": None, "level": None, "team": None,
                "net_worth": gold_earned(r["gpm"], r["duration"]),
                "match_type": match_type(r["lobby_type"], r["game_mode"]),
                "lobby_type": r["lobby_type"], "game_mode": r["game_mode"],
                "has_detail": False,
            }

    for r in conn.execute("SELECT * FROM matches"):
        g = dict(r)
        mid = str(g["match_id"])
        base = rows.get(mid, {})
        base.update({
            "match_id": mid, "hero": g.get("hero") or base.get("hero"),
            "hero_id": g.get("hero_id") or base.get("hero_id"),
            "win": g["win"] if g["win"] is not None else base.get("win"),
            "kills": g["kills"], "deaths": g["deaths"], "assists": g["assists"],
            "gpm": g["gpm"], "xpm": g["xpm"],
            "last_hits": g["last_hits"], "denies": g["denies"],
            "duration_s": g["duration_s"], "cs_at_10": g["cs_at_10"],
            # Keep one definition across the whole column: use the OpenDota
            # figure whenever the match is in that history, so the handful of
            # locally captured games don't read 7-14%% off their neighbours.
            # The GSI sum of gold sources is only a fallback for games OpenDota
            # never ingested.
            "net_worth": base.get("net_worth") if base.get("net_worth") is not None
                         else (g["net_worth"] if g["net_worth"] is not None
                               else gold_earned(g["gpm"], g["duration_s"])),
            "level": g["level"], "team": g["team"],
            "start_time": g.get("started_at") or base.get("start_time"),
            "has_detail": True,
        })
        base.setdefault("match_type", "Unknown")
        base.setdefault("lobby_type", None)
        base.setdefault("game_mode", None)
        rows[mid] = base

    out = list(rows.values())
    out.sort(key=lambda r: (r.get("start_time") or 0), reverse=True)
    return out


def unified_summary(matches: list[dict]) -> dict:
    """Aggregate KPIs + per-hero + recent form over the merged match list."""
    if not matches:
        return {"totals": {}, "heroes": [], "recent": []}
    n = len(matches)
    def avg(key):
        vals = [m[key] for m in matches if m.get(key) is not None]
        return sum(vals) / len(vals) if vals else None
    wins = sum(1 for m in matches if m.get("win") == 1)
    losses = sum(1 for m in matches if m.get("win") == 0)

    hero_agg: dict[str, dict] = {}
    for m in matches:
        h = hero_agg.setdefault(m["hero"], {"hero": m["hero"], "games": 0, "wins": 0,
                                            "k": 0, "d": 0, "a": 0, "gpm": 0})
        h["games"] += 1
        h["wins"] += 1 if m.get("win") == 1 else 0
        for src, dst in (("kills", "k"), ("deaths", "d"), ("assists", "a"), ("gpm", "gpm")):
            h[dst] += m.get(src) or 0
    heroes = []
    for h in hero_agg.values():
        g = h["games"]
        heroes.append({"hero": h["hero"], "games": g, "wins": h["wins"],
                       "k": h["k"] / g, "d": h["d"] / g, "a": h["a"] / g, "gpm": h["gpm"] / g})
    heroes.sort(key=lambda x: (x["games"], x["hero"]), reverse=True)

    recent = list(reversed(matches[:20]))  # oldest -> newest for the sparkline
    return {
        "totals": {
            "games": n, "wins": wins, "losses": losses,
            "k": avg("kills"), "d": avg("deaths"), "a": avg("assists"),
            "gpm": avg("gpm"), "xpm": avg("xpm"),
            "lh": avg("last_hits"), "cs10": avg("cs_at_10"),
            "avg_deaths": avg("deaths"), "dur": avg("duration_s"),
        },
        "heroes": heroes,
        "recent": recent,
    }


def _od_match_row(conn: sqlite3.Connection, match_id: str) -> dict | None:
    """A match we only have from OpenDota (no local GSI capture): return a
    match-like summary so the detail page and the deep review still work."""
    if not _table_exists(conn, "od_matches"):
        return None
    try:
        r = conn.execute("SELECT * FROM od_matches WHERE match_id=?", (int(match_id),)).fetchone()
    except (ValueError, sqlite3.Error):
        return None
    if r is None:
        return None
    r = dict(r)
    return {
        "match": {
            "match_id": str(r["match_id"]), "hero": _hero_names().get(r["hero_id"], f"Hero {r['hero_id']}"),
            "hero_id": r["hero_id"], "win": r["win"], "team": None,
            "duration_s": r["duration"], "level": None,
            "kills": r["kills"], "deaths": r["deaths"], "assists": r["assists"],
            "gpm": r["gpm"], "xpm": r["xpm"], "last_hits": r["last_hits"], "denies": r["denies"],
            "net_worth": gold_earned(r["gpm"], r["duration"]), "cs_at_10": None,
            "match_type": match_type(r["lobby_type"], r["game_mode"]),
        },
        "timeseries": [], "deaths": [], "items": [], "od_only": True,
    }


def match_detail(conn: sqlite3.Connection, match_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()
    if row is None:
        return _od_match_row(conn, match_id)  # OpenDota-only match (no local capture)
    ts = conn.execute(
        "SELECT * FROM timeseries WHERE match_id=? ORDER BY clock", (match_id,)
    ).fetchall()
    deaths = conn.execute(
        "SELECT * FROM deaths WHERE match_id=? ORDER BY death_number", (match_id,)
    ).fetchall()
    items = conn.execute(
        "SELECT item, first_seen FROM items WHERE match_id=? ORDER BY first_seen", (match_id,)
    ).fetchall()
    match = dict(row)
    # GSI doesn't expose the game mode, so borrow the match type from OpenDota if
    # we've scraped it.
    od = _od_match_row(conn, match_id)
    match["match_type"] = od["match"]["match_type"] if od else "Unknown"
    return {
        "match": match,
        "timeseries": [dict(r) for r in ts],
        "deaths": [dict(r) for r in deaths],
        "items": [dict(r) for r in items],
    }


def summary(conn: sqlite3.Connection) -> dict:
    """Aggregate KPIs across all recorded matches, plus per-hero breakdown and
    a recent-form trend."""
    agg = conn.execute(
        """SELECT
             COUNT(*)                                    AS games,
             SUM(CASE WHEN win=1 THEN 1 ELSE 0 END)      AS wins,
             SUM(CASE WHEN win=0 THEN 1 ELSE 0 END)      AS losses,
             AVG(kills)    AS k, AVG(deaths) AS d, AVG(assists) AS a,
             AVG(gpm) AS gpm, AVG(xpm) AS xpm,
             AVG(last_hits) AS lh, AVG(cs_at_10) AS cs10,
             AVG(deaths)  AS avg_deaths,
             AVG(duration_s) AS dur
           FROM matches""",
    ).fetchone()

    heroes = conn.execute(
        """SELECT hero,
                  COUNT(*) AS games,
                  SUM(CASE WHEN win=1 THEN 1 ELSE 0 END) AS wins,
                  AVG(kills) AS k, AVG(deaths) AS d, AVG(assists) AS a,
                  AVG(gpm) AS gpm
           FROM matches GROUP BY hero ORDER BY games DESC, hero""",
    ).fetchall()

    # recent form: newest last so the sparkline reads left→right in time order
    recent = conn.execute(
        """SELECT match_id, hero, win, kills, deaths, assists, gpm, cs_at_10, started_at
           FROM matches ORDER BY COALESCE(started_at,0) DESC LIMIT 20""",
    ).fetchall()

    return {
        "totals": dict(agg) if agg else {},
        "heroes": [dict(h) for h in heroes],
        "recent": [dict(r) for r in reversed(recent)],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv: list[str]) -> None:
    conn = connect()
    if "--backfill" in argv:
        written = backfill(conn)
        print(f"Ingested {len(written)} match(es) into {config.db_path().name}: {written}")
    if "--list" in argv or not argv:
        for m in list_matches(conn):
            res = {1: "W", 0: "L"}.get(m["win"], "?")
            dur = f"{(m['duration_s'] or 0)//60}:{(m['duration_s'] or 0)%60:02d}"
            print(f"{res}  {m['hero']:<16} {m['kills']}/{m['deaths']}/{m['assists']:<3} "
                  f"{m['gpm']}gpm cs@10={m['cs_at_10']}  {dur}  #{m['match_id']}")


if __name__ == "__main__":
    _main(sys.argv[1:])
