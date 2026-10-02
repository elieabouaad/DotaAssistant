# Dota Assistant

A real-time coaching assistant for Dota 2, built on Valve's official **Game
State Integration (GSI)** — the same zero-risk mechanism broadcast tools use.
It speaks timing reminders and tactical alerts while you play, helps you
draft and itemize, overlays the essentials in-game, and turns your match
history into a personal training plan with optional AI reviews.

> Not affiliated with, endorsed by, or sponsored by Valve Corporation. The
> assistant only reads the JSON the game itself sends to a local server you
> run — it never touches the game process, so it's VAC-safe by construction.

## What it does

- **Voice timer announcer** — bounty/power/water/wisdom runes, camp stacking,
  day/night, Tormentor, Roshan respawn window and Aegis expiry (auto-detected
  from GSI, F8 hotkey as backup). Timings live in a patch-editable file.
- **State-aware alerts** — no-TP reminder, death recap with buyback math,
  base-under-attack siren, "your ultimate is ready".
- **Both-team intelligence** — enemy smoke warnings, Roshan being contested,
  teamfight swing windows, enemy glyph/scan/buyback. GSI sends these for both
  teams; the game's own UI never surfaces them.
- **Live dashboard** (`http://127.0.0.1:53100/`) — everything GSI sends on
  one screen: hero vitals, cooldowns, gold breakdown, minimap position, live
  ten-player scoreboard rebuilt from the kill feed, event feed, and the next
  timed events counting down.
- **In-game overlay** — a frameless, click-through HUD for single-monitor
  setups: next timers, Roshan window, buyback status when dead, CS pace, and
  smoke/scan flags. F9 toggles it.
- **Draft assistant** — counter-picks ranked from OpenDota head-to-head data,
  filtered by role, blended with win rates in *your* bracket, annotated with
  your own games on each hero, and explained in plain English.
- **Itemization assistant** — the core build winners actually buy on your
  hero by game phase, plus situational counter-items vs the enemy draft
  (BKB vs magic burst, detection vs invis, MKB vs evasion, …), each with the
  reason and the heroes that triggered it. Deterministic and instant.
- **Laning coach** — a live CS benchmark for your hero (green/red on the
  dashboard and overlay) with quiet spoken check-ins at 5/8/10 minutes only
  when you're behind.
- **Match history & training plan** — every game is distilled into a local
  SQLite database: KPIs, sparklines, per-hero stats, net-worth curves, death
  maps, build orders. The **Improve** tab mines it into a ranked list of
  weaknesses with one clear top focus.
- **AI coaching (optional)** — a live coach that speaks one short tip every
  couple of minutes, post-game deep reviews, and a full-history improvement
  plan. Works with your **Claude Code login (no API key)** or
  `ANTHROPIC_API_KEY`; every AI feature degrades gracefully to the
  deterministic analysis without credentials.
- **Discord voice (optional)** — a bot speaks the announcements in your voice
  channel so your whole party hears the timers.

## Quickstart

Requires Python 3.11+ and Dota 2 on Steam. Windows is fully supported; macOS
and Linux work with a degraded overlay.

```sh
git clone https://github.com/<you>/DotaAssistant
cd DotaAssistant
python -m venv .venv
.venv/Scripts/pip install -e .          # Windows
# .venv/bin/pip install -e .            # macOS / Linux
.venv/Scripts/activate                  # or source .venv/bin/activate
```

1. **Install the GSI config** so Dota knows where to send game state:
   ```sh
   dota-assistant gsi-config > "C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game\dota\cfg\gamestate_integration\gamestate_integration_assistant.cfg"
   ```
   (Create the `gamestate_integration` folder if needed. macOS path:
   `~/Library/Application Support/Steam/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/`.)
2. In Steam: Dota 2 → Properties → Launch Options → add
   `-gamestateintegration`.
3. **Configure (optional but recommended):** copy
   [config.example.toml](config.example.toml) to `config.toml` in the folder
   you'll run from, and set your OpenDota `account_id` (enables personal
   draft/history features; needs "Expose Public Match Data" in the Dota
   client), your bracket/roles for calibrated coaching, and anything else.
4. **Run it:**
   ```sh
   dota-assistant                # server + dashboard
   dota-assistant --overlay      # ...plus the in-game HUD (one screen)
   ```
   Open `http://127.0.0.1:53100/` and start a match (a bot match works).

For Turbo matches start with `--turbo` (GSI doesn't expose the game mode, so
the switch is manual).

## Commands

| Command | What it does |
|---|---|
| `dota-assistant` | Start the GSI receiver + dashboard (`--turbo`, `--overlay`, `--no-enrich`) |
| `dota-assistant overlay` | In-game HUD in its own process (`--setup` to drag/reposition) |
| `dota-assistant scrape` | Pull your full OpenDota history + print an improvement report |
| `dota-assistant enrich` | Backfill OpenDota parses so match detail is pre-loaded |
| `dota-assistant stats` | Local match DB tools (`--backfill`, `--list`) |
| `dota-assistant gsi-config` | Print the GSI config file (uses your configured port) |
| `dota-assistant build-talents` | Rebuild talent-tree data after a patch |

## Configuration

Everything lives in `config.toml` — see the commented
[config.example.toml](config.example.toml) for all options: your account id
and bracket/roles (`[player]`), host/port (`[server]`), session-log caps
(`[logging]`), AI coach (`[coach]`), overlay placement (`[overlay]`),
Discord voice (`[discord]`), and parse backfill (`[enrich]`).

Game timings (rune cadence, Roshan windows, Turbo overrides) are in
[src/dota_assistant/data/timings.toml](src/dota_assistant/data/timings.toml)
and track the current patch. Which announcements are spoken is toggled live
from the dashboard's **Settings** tab.

Voice: the OS speech engine is used locally (SAPI5 / `say` / espeak). Set
`DOTA_ASSISTANT_MUTE=1` to print announcements without speaking.

### Claude access (optional, for AI features)

Set `provider` under `[coach]`:
- `auto` (default) — uses your **Claude Code** login if the `claude` CLI is
  installed (no API key needed), else falls back to `ANTHROPIC_API_KEY`.
- `claude_code` / `api` — force one or the other.

The live coach is off by default (`[coach] enabled`); post-game reviews are
user-initiated from the History tab, so there's never surprise API cost.

### Discord voice (optional)

Create a bot at discord.com/developers → invite with Connect + Speak → put
your voice channel id in `[discord]` and the token in the
`DISCORD_BOT_TOKEN` env var (preferred over the config file). Needs FFmpeg
on PATH (`winget install Gyan.FFmpeg` / `brew install ffmpeg`).

## Data & privacy

Everything stays on your machine, in the directory you run from (override
with `DOTA_ASSISTANT_HOME`):

- `stats.db` — your distilled match history.
- `logs/session_*.jsonl` — raw GSI payloads (useful for debugging and the
  stats backfill). **These contain your Steam ID**; they're capped per
  session and rotated (`[logging]` in config), and never leave your machine.
- `cache/` — cached OpenDota responses, regenerable at any time.
- `settings.json` — dashboard toggle state and overlay position.

Network calls: OpenDota (cached, rate-friendly), and — only if you enable
them — Microsoft edge-tts for Discord voice and Anthropic Claude for AI
coaching. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Platform notes

- **Overlay**: full click-through transparency on Windows; Dota must be in
  Fullscreen Windowed (Borderless). On macOS/Linux it degrades to a small
  opaque always-on-top box. Linux needs `python3-tk` installed.
- **Hotkeys** (F8 Roshan, F9 overlay): on macOS the terminal needs Input
  Monitoring permission; without it the HTTP endpoint
  (`POST /event/roshan-killed`) still works.
- GSI only exposes *your own* hero's detail in a normal pub — the dashboard
  is honest about what the other nine players' rows can and can't show.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and project
conventions, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it all
fits together, and [ROADMAP.md](ROADMAP.md) for where help is wanted.

## License

[MIT](LICENSE).
