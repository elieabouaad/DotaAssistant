# Architecture

## Why GSI

| Approach | What it gives | Latency | Risk | Verdict |
|---|---|---|---|---|
| **Game State Integration (GSI)** | Official Valve API. The game POSTs JSON (your hero, HP/mana, abilities+cooldowns, items, gold, KDA, clock time, day/night, draft picks) to a local HTTP server you run. | ~0.1–0.5s | **Zero** — officially supported (same mechanism casters/tools use) | ✅ **Core data source** |
| Screen capture + CV/OCR | Minimap, enemy HUD info GSI hides | 0.5–2s, fragile | Safe (external) but brittle across patches/resolutions | Deferred (see ROADMAP) |
| Memory reading / injection | Everything | Instant | **VAC ban** | ❌ Excluded, forever |
| OpenDota / Stratz APIs | Hero matchups, meta win rates, player histories, post-game replays | Not live for your own pub | Zero | ✅ For draft + post-game |
| Manual hotkeys | Events GSI can't see in player mode | Instant | Zero | ✅ Supplement (F8 Roshan backup) |

### What GSI actually sends (verified against captured logs)

In a normal pub you only get **your own** hero's detailed data (HP, mana,
items, abilities). But the payload is richer than that — for **both teams**
GSI also sends:

- a live **event feed** (`events[]`): hero kills (killer→victim ids),
  tower/rax kills, Roshan killed + Aegis pickup, Roshan roar, **enemy
  smoke**, glyph/scan/buyback — player ids 0–4 = Radiant, 5–9 = Dire, so each
  is attributable to a team.
- **building HP** for every tower/rax/fort on *your own* team (`buildings`).
- team kill scores (`radiant_score`/`dire_score`).

Still hidden: **enemy hero positions and items**. Anything needing those uses
manual hotkeys today (vision layer is a possible future, see ROADMAP).

The GSI schema is undocumented and shifts slightly between patches — session
logs of raw payloads (`logs/session_*.jsonl`) are the ground truth for what
the current patch actually sends.

## Data flow

```
Dota 2 client ──GSI POST──▶ server.py (FastAPI, 127.0.0.1)
                                │
          ┌──────────┬──────────┼──────────────┬──────────────┐
          ▼          ▼          ▼              ▼              ▼
     timer_engine  alerts   game_events    laning.LaneCoach  coach (Claude)
     (clock math)  (own     (both-team     (CS benchmarks)   (periodic
                   hero)    event feed)                      snapshots)
          └──────────┴──────────┴──────┬───────┴──────────────┘
                                       ▼
                       announcer.py (queued TTS: SAPI5 / say /
                       espeak) + discord_bot.py (edge-tts sink)
                                       │
          web/dashboard.html ◀── REST /api/* ──▶ overlay.py (own process,
          (Live/Draft/History/Improve tabs)      polls /api/live)
```

**Voice-first output.** You can't read an overlay mid-fight; spoken alerts
("stack in 15 seconds") are the highest-value UX. The dashboard is for
between fights and between games; the overlay shows only what's readable at
a glance.

## Module map

| Module | Role |
|---|---|
| `server.py` | GSI receiver + REST API + static dashboard; wires all engines together |
| `cli.py` | `dota-assistant` subcommand dispatcher |
| `config.py` | Single config loader, data-dir resolution, packaged-resource access |
| `session_log.py` | Capped, rotating raw-payload logs |
| `timer_engine.py` | Clock-driven recurring events + Roshan one-shots (data: `data/timings.toml`) |
| `alerts.py` | Own-hero alerts: TP, death/buyback, base defense, ultimate ready |
| `game_events.py` | Both-team event intelligence: smoke, Roshan contest, teamfight swing, scoreboard |
| `laning.py` | Live CS benchmark coach + draft laning advisory |
| `draft.py` | OpenDota counter-pick ranking (bracket/role/pool aware, cached) |
| `itemization.py` | Core build from item popularity + curated counter-item rules engine |
| `review.py` | Post-game deep review: OpenDota + local stats merge, optional Claude write-up |
| `weakness.py` | Deterministic "what to practice" report over stats.db |
| `stats.py` | Distills session logs into SQLite (`stats.db`) |
| `enrich.py` | Background OpenDota parse backfill |
| `opendota_scraper.py` | Full-history scrape + improvement report |
| `coach.py` | Live AI tips (compact snapshots → one spoken sentence) |
| `llm.py` | Claude access: Claude Code CLI login or `ANTHROPIC_API_KEY` |
| `announcer.py` | Queued cross-platform TTS |
| `discord_bot.py` | Voice-channel announcement sink |
| `overlay.py` | Click-through in-game HUD (separate process) |
| `settings.py` | Dashboard-set runtime toggles, persisted to `settings.json` |
| `build_talents.py` | Offline builder for `data/talents.json` |

## Design principles

- **Deterministic first, LLM second.** Itemization, laning advice, and the
  weakness report are rules + real data — instant, free, and honest. Claude
  layers a narrative on top only where judgment genuinely helps (post-game
  reviews, live tips), and everything degrades gracefully without it.
- **No fake numbers.** Where the data can't support a claim (lane-specific
  winrates, hero synergy), we show a deterministic advisory or nothing —
  never an invented stat. The Stratz upgrade path in ROADMAP.md lifts these.
- **Patch data in config, not code.** Timings live in `data/timings.toml`;
  the itemization threat table is an explicitly curated, editable table.
- **Everything local.** The server binds to localhost; data (stats.db, logs,
  cache) lives in your working directory (`$DOTA_ASSISTANT_HOME` to
  override); network calls are OpenDota (cached), optional edge-tts, and
  optional Claude.
