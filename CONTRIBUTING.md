# Contributing

Thanks for your interest! This is a young project — for anything bigger than
a bugfix, please open an issue first so we can agree on the direction.

## Dev setup

```sh
git clone <your fork>
cd DotaAssistant
python -m venv .venv
.venv/Scripts/pip install -e .[dev]     # Windows
# .venv/bin/pip install -e .[dev]       # macOS / Linux
```

Run the checks CI runs:

```sh
pytest
ruff check src tests
```

## Things to know

- **Config defaults live in two places on purpose:** `config.DEFAULTS` in
  [src/dota_assistant/config.py](src/dota_assistant/config.py) (what the code
  uses) and [config.example.toml](config.example.toml) (what users copy).
  Keep them in sync — `tests/test_config.py` asserts the example's keys are a
  subset of `DEFAULTS`.
- **`laning` ↔ `itemization` import cycle:** these two modules deliberately
  import each other inside function bodies (deferred imports). Don't hoist
  those imports to module level — it breaks the cycle guard.
- **Writable data goes through `config.py`** (`data_dir()`, `cache_dir()`,
  `log_dir()`, `db_path()`); read-only shipped files go through the resource
  helpers. Never write next to the source files.
- **OpenDota is a free shared API.** Anything that adds calls must be cached
  (see `draft._cached_get`) and rate-friendly.
- **GSI payloads are undocumented** and shift between patches. When in doubt,
  log a raw session and build against what the game actually sends.
- Patch-dependent numbers (rune timings, Roshan windows) belong in
  `src/dota_assistant/data/timings.toml`, never in code.

## Testing

Only a few modules have tests today (`timer_engine`, `alerts`, `config`,
`session_log`) — adding coverage for the untested ones is a very welcome
contribution. Tests must not hit the network; simulate GSI states like the
existing suites do.
