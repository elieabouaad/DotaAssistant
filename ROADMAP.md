# Roadmap

Ideas and known gaps, roughly ordered. Open an issue if you want to pick one
up or propose something new.

## Data & coaching depth

- **Stratz integration** (needs an API key; all three are additive behind
  graceful fallbacks):
  - True lane-phase outcomes — a real "you won/lost *this lane*" verdict
    instead of the deterministic threat advisory.
  - Hero **synergy** with allied picks in the draft assistant (OpenDota has
    no clean synergy signal, so we don't fake one today).
  - Parse-free lane/item detail in post-game reviews, skipping OpenDota's
    parse queue.
- **Draft capture from GSI pick phase** — remember the enemy draft
  automatically so in-game nudges ("you can afford BKB — they have 3 stuns")
  fire without re-entering picks manually.
- **Vision layer** (screen capture of the minimap for missing-enemy
  detection) — only if the GSI + hotkey approach proves insufficient; must
  stay external to the game process.

## Engineering

- **Test coverage** for the untested modules — `stats`, `game_events`,
  `draft`, `itemization`, `laning`, `review`, `weakness` are all deterministic
  and very testable. Network modules need fixtures.
- **Split `dashboard.html`** (1,800 lines of inline CSS/JS) into static
  sibling files. It ships as one self-contained file today — zero CDN
  dependencies is a feature, so any split must keep it fully offline.
- **App-factory refactor of `server.py`** — the module-level singletons work
  for a single-instance tool but block proper endpoint tests.
- **Localization** of spoken announcements.
- **Linux overlay** parity (click-through currently needs Windows).

## Patch upkeep

- `data/timings.toml` tracks the current patch's timings — PRs updating it
  after a patch are always welcome.
- `dota-assistant build-talents --out src/dota_assistant/data/talents.json`
  refreshes the shipped talent data (maintainer step after hero changes).
- The itemization hero-threat table (`itemization.py`) is curated by hand —
  accuracy-first; a counter-item is only suggested when it's clearly correct.
