"""Capped, rotating raw GSI session logs.

GSI posts ~10 payloads a second, so an uncapped append-only log grows by
hundreds of MB over an evening of games. This logger is lazy (the file is
created on the first payload, so importing the server writes nothing), stops
at a per-session size cap, and prunes old session/death files at startup.
Configured by the [logging] section of config.toml.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class SessionLogger:
    def __init__(self, cfg: dict, log_dir: Path):
        self.enabled = bool(cfg.get("session_logs", True))
        self.max_bytes = int(cfg.get("max_mb", 64)) * 1024 * 1024
        self.keep = int(cfg.get("keep_sessions", 20))
        self._dir = log_dir
        self._path: Path | None = None
        self._written = 0
        self._capped = False

    @property
    def path(self) -> Path | None:
        """The session file, once anything has been written (else None)."""
        return self._path

    def write(self, record: dict) -> None:
        if not self.enabled or self._capped:
            return
        if self._path is None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path = self._dir / f"session_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        line = json.dumps(record) + "\n"
        if self._written + len(line) > self.max_bytes:
            self._capped = True
            with self._path.open("a") as f:
                f.write(json.dumps({"log_capped": True,
                                    "reason": f"[logging] max_mb={self.max_bytes // 2**20} reached"}) + "\n")
            print(f"Session log reached its {self.max_bytes // 2**20}MB cap — "
                  f"no further raw payloads recorded this session "
                  f"(raise [logging] max_mb in config.toml if you want more)", flush=True)
            return
        with self._path.open("a") as f:
            f.write(line)
        self._written += len(line)

    def cleanup(self) -> None:
        """Keep only the newest `keep_sessions` files of each kind. Run
        `dota-assistant stats --backfill` first if you care about old games."""
        if not self._dir.is_dir() or self.keep <= 0:
            return
        for pattern in ("session_*.jsonl", "deaths_*.jsonl"):
            files = sorted(self._dir.glob(pattern),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[self.keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
