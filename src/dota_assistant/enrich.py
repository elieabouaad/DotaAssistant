"""Background OpenDota parse backfill.

OpenDota stores every public match, but only *parsed* matches carry the minute-
by-minute detail this dashboard wants: net-worth / XP / last-hit curves, the
purchase log behind the build order, and teamfight death positions. A match is
parsed only if somebody asks OpenDota to parse it, and the free API parses on
request — so the History tab was showing "no detail" for essentially every game
that wasn't captured live by GSI.

This module walks your OpenDota history and requests those parses in the
background, so detail is simply *there* when you open a match instead of
demanding a manual Regenerate.

Two hard constraints shape the design:

  * **Replays expire.** A parse request on an old match returns a job that
    completes normally and produces nothing, because Valve no longer serves the
    replay. Empirically ~2 months still parse and ~6 months don't, so we only
    attempt matches inside `max_age_days` and permanently record the ones that
    come back empty as `unavailable` rather than retrying them every launch.
  * **The API is free and shared.** Work is capped per run (`max_matches`),
    batched so parses run concurrently rather than one 30-second wait at a time,
    and spaced by `request_delay_s`. Anything left over is picked up next launch.

Parsed match JSON lands in the same `cache/matches_<id>.json` that review.py
already reads, so once a match is enriched here `review.opendota_detail()`
renders it with no extra network at all.

Usage:
    dota-assistant enrich                 # backfill now, printing progress
    dota-assistant enrich --max 20        # smaller run
    dota-assistant enrich --status        # what's been done so far
"""

from __future__ import annotations

import json
import sys
import time

import requests

from . import config as app_config
from . import review, stats

API = review.API

SCHEMA = """
CREATE TABLE IF NOT EXISTS od_parse (
    match_id     INTEGER PRIMARY KEY,
    status       TEXT,     -- 'parsed' | 'unavailable' | 'error'
    attempts     INTEGER,
    last_attempt REAL,
    version      INTEGER   -- OpenDota parser version, when parsed
);
"""


def config() -> dict:
    return app_config.section("enrich")


def _schema(conn) -> None:
    conn.executescript(SCHEMA)


def _mark(conn, match_id: int, status: str, version=None) -> None:
    conn.execute(
        """INSERT INTO od_parse (match_id, status, attempts, last_attempt, version)
           VALUES (?,?,1,?,?)
           ON CONFLICT(match_id) DO UPDATE SET
             status=excluded.status,
             attempts=od_parse.attempts + 1,
             last_attempt=excluded.last_attempt,
             version=excluded.version""",
        (int(match_id), status, time.time(), version),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# what still needs doing
# --------------------------------------------------------------------------- #

def cached_version(match_id) -> int | None:
    """Parser version from the on-disk match cache, or None if absent/unparsed.
    Parsed match JSON never changes, so a cache hit means we're done forever."""
    path = app_config.cache_dir() / f"matches_{match_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("version")
    except (json.JSONDecodeError, OSError):
        return None


def pending(conn, cfg: dict | None = None) -> list[int]:
    """Match ids worth attempting, newest first.

    Skips anything already parsed on disk and anything previously resolved as
    parsed/unavailable, so repeated launches converge instead of re-walking the
    whole history.
    """
    cfg = cfg or config()
    _schema(conn)
    cutoff = time.time() - float(cfg["max_age_days"]) * 86400
    resolved = {r[0] for r in conn.execute(
        "SELECT match_id FROM od_parse WHERE status IN ('parsed','unavailable')")}
    out = []
    for (mid,) in conn.execute(
        "SELECT match_id FROM od_matches WHERE start_time >= ? ORDER BY start_time DESC",
        (cutoff,),
    ):
        if mid in resolved:
            continue
        if cached_version(mid):
            _mark(conn, mid, "parsed", cached_version(mid))
            continue
        out.append(mid)
    return out


def status(conn) -> dict:
    """Counts for the dashboard / CLI."""
    _schema(conn)
    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM od_parse GROUP BY status").fetchall())
    return {
        "parsed": counts.get("parsed", 0),
        "unavailable": counts.get("unavailable", 0),
        "error": counts.get("error", 0),
        "pending": len(pending(conn)),
        "running": _RUN["running"],
        "done": _RUN["done"],
        "total": _RUN["total"],
    }


# --------------------------------------------------------------------------- #
# OpenDota calls
# --------------------------------------------------------------------------- #

def _get_match(match_id) -> dict | None:
    try:
        r = requests.get(f"{API}/matches/{match_id}", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _cache_match(match_id, data: dict) -> None:
    try:
        (app_config.cache_dir() / f"matches_{match_id}.json").write_text(
            json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _request_parse(match_id) -> int | None:
    """Ask OpenDota to parse the replay. Returns a job id, or None."""
    try:
        r = requests.post(f"{API}/request/{match_id}", timeout=20)
        r.raise_for_status()
        return ((r.json() or {}).get("job") or {}).get("jobId")
    except Exception:
        return None


def _job_done(job_id) -> bool:
    """OpenDota returns the job while it's queued and null once it's finished —
    finished meaning *dequeued*, which is not the same as successful."""
    try:
        r = requests.get(f"{API}/request/{job_id}", timeout=20)
        return r.status_code == 200 and r.json() is None
    except Exception:
        return False


def ensure_parsed(match_id, conn=None, cfg: dict | None = None,
                  wait: bool = True) -> str:
    """Parse one match end to end. Returns 'parsed', 'unavailable' or 'error'.

    Safe to call on an already-parsed match (one cheap GET). Used both by the
    backfill loop and by the dashboard's Regenerate button.
    """
    cfg = cfg or config()
    own = conn is None
    if own:
        conn = stats.connect()
    try:
        _schema(conn)
        data = _get_match(match_id)
        if data is None:
            _mark(conn, match_id, "error")
            return "error"
        if data.get("version"):
            _cache_match(match_id, data)
            _mark(conn, match_id, "parsed", data["version"])
            return "parsed"

        job = _request_parse(match_id)
        if job is None:
            _mark(conn, match_id, "error")
            return "error"
        if not wait:
            return "pending"

        deadline = time.time() + float(cfg["poll_timeout_s"])
        while time.time() < deadline:
            time.sleep(float(cfg["poll_interval_s"]))
            if _job_done(job):
                break

        data = _get_match(match_id)
        if data and data.get("version"):
            _cache_match(match_id, data)
            _mark(conn, match_id, "parsed", data["version"])
            return "parsed"
        # Job finished but produced nothing: the replay is gone for good.
        _mark(conn, match_id, "unavailable")
        return "unavailable"
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# backfill
# --------------------------------------------------------------------------- #

# Live progress for /api/enrich/status. A single backfill runs at a time.
_RUN = {"running": False, "done": 0, "total": 0}


def backfill(cfg: dict | None = None, log=print) -> dict:
    """Walk pending matches newest-first, parsing in small concurrent batches.

    Batching matters: a parse takes ~30s, so ten at a time turns an hour of
    sequential waiting into a few minutes.
    """
    cfg = cfg or config()
    if _RUN["running"]:
        return {"skipped": "already running"}

    conn = stats.connect()
    try:
        todo = pending(conn, cfg)[: int(cfg["max_matches"])]
        _RUN.update(running=True, done=0, total=len(todo))
        if not todo:
            log("OpenDota enrich: nothing to do — recent matches are all parsed.")
            return {"parsed": 0, "unavailable": 0, "error": 0, "total": 0}

        log(f"OpenDota enrich: {len(todo)} match(es) to parse "
            f"(<= {cfg['max_age_days']}d old); this runs in the background.")
        tally = {"parsed": 0, "unavailable": 0, "error": 0}
        batch = max(1, int(cfg["batch_size"]))
        delay = float(cfg["request_delay_s"])

        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            jobs: dict[int, int] = {}

            # Phase 1 — check, and request a parse for anything unparsed.
            for mid in chunk:
                data = _get_match(mid)
                if data is None:
                    _mark(conn, mid, "error")
                    tally["error"] += 1
                    _RUN["done"] += 1
                elif data.get("version"):
                    _cache_match(mid, data)
                    _mark(conn, mid, "parsed", data["version"])
                    tally["parsed"] += 1
                    _RUN["done"] += 1
                else:
                    job = _request_parse(mid)
                    if job is None:
                        _mark(conn, mid, "error")
                        tally["error"] += 1
                        _RUN["done"] += 1
                    else:
                        jobs[mid] = job
                time.sleep(delay)

            if not jobs:
                continue

            # Phase 2 — wait for the whole batch, then re-fetch each one.
            deadline = time.time() + float(cfg["poll_timeout_s"])
            outstanding = dict(jobs)
            while outstanding and time.time() < deadline:
                time.sleep(float(cfg["poll_interval_s"]))
                for mid, job in list(outstanding.items()):
                    if _job_done(job):
                        del outstanding[mid]

            for mid in jobs:
                data = _get_match(mid)
                if data and data.get("version"):
                    _cache_match(mid, data)
                    _mark(conn, mid, "parsed", data["version"])
                    tally["parsed"] += 1
                else:
                    # Replay unavailable (too old) — don't ask again.
                    _mark(conn, mid, "unavailable")
                    tally["unavailable"] += 1
                _RUN["done"] += 1
                time.sleep(delay)

            log(f"OpenDota enrich: {_RUN['done']}/{_RUN['total']} "
                f"({tally['parsed']} parsed, {tally['unavailable']} no replay)")

        log(f"OpenDota enrich: done — {tally['parsed']} parsed, "
            f"{tally['unavailable']} without a replay, {tally['error']} errored.")
        tally["total"] = len(todo)
        return tally
    finally:
        _RUN["running"] = False
        conn.close()


def start_background(cfg: dict | None = None) -> bool:
    """Kick the backfill off in a daemon thread. Returns False if disabled."""
    import threading
    cfg = cfg or config()
    if not cfg.get("on_launch", True):
        return False

    def run():
        try:
            backfill(cfg)
        except Exception as exc:
            print(f"OpenDota enrich stopped: {exc}", flush=True)

    threading.Thread(target=run, daemon=True, name="od-enrich").start()
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv: list[str]) -> None:
    cfg = config()
    for i, a in enumerate(argv):
        if a == "--max" and i + 1 < len(argv):
            cfg["max_matches"] = int(argv[i + 1])
        if a == "--days" and i + 1 < len(argv):
            cfg["max_age_days"] = int(argv[i + 1])

    conn = stats.connect()
    try:
        st = status(conn)
    finally:
        conn.close()

    if "--status" in argv:
        print(f"parsed:      {st['parsed']}")
        print(f"no replay:   {st['unavailable']}")
        print(f"errored:     {st['error']}")
        print(f"still to do: {st['pending']} (within {cfg['max_age_days']} days)")
        return

    backfill(cfg)


if __name__ == "__main__":
    _main(sys.argv[1:])
