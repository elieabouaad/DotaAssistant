"""SessionLogger: lazy creation, size cap, retention, and the off switch."""

import json

from dota_assistant.session_log import SessionLogger


def test_lazy_creation_and_write(tmp_path):
    logger = SessionLogger({}, tmp_path / "logs")
    assert logger.path is None
    assert not (tmp_path / "logs").exists()  # nothing written yet
    logger.write({"n": 1})
    assert logger.path is not None and logger.path.exists()
    assert json.loads(logger.path.read_text().splitlines()[0]) == {"n": 1}


def test_disabled_writes_nothing(tmp_path):
    logger = SessionLogger({"session_logs": False}, tmp_path / "logs")
    logger.write({"n": 1})
    assert logger.path is None
    assert not (tmp_path / "logs").exists()


def test_size_cap_stops_appending(tmp_path):
    logger = SessionLogger({"max_mb": 64}, tmp_path / "logs")
    logger.max_bytes = 200  # shrink the cap for the test
    for i in range(50):
        logger.write({"payload": "x" * 20, "n": i})
    lines = logger.path.read_text().splitlines()
    assert json.loads(lines[-1]).get("log_capped") is True
    assert len(lines) < 50  # stopped well before all 50
    before = logger.path.read_text()
    logger.write({"n": 999})  # further writes are dropped
    assert logger.path.read_text() == before


def test_cleanup_keeps_newest(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for i in range(5):
        f = log_dir / f"session_2026010{i}_000000.jsonl"
        f.write_text("{}\n")
        # distinct mtimes so the sort is deterministic
        import os
        os.utime(f, (1000 + i, 1000 + i))
    logger = SessionLogger({"keep_sessions": 2}, log_dir)
    logger.cleanup()
    remaining = sorted(p.name for p in log_dir.glob("session_*.jsonl"))
    assert remaining == ["session_20260103_000000.jsonl", "session_20260104_000000.jsonl"]
