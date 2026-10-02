"""Shared LLM helper — talk to Claude via whichever auth you have.

Two providers, tried in order per the `provider` setting:

- **claude_code** — shells out to the `claude` CLI in headless mode
  (`claude -p`). This reuses your Claude Code login (your Max/Pro subscription),
  so **no ANTHROPIC_API_KEY is needed**. The prompt goes in on stdin (no
  command-line length limit); the system prompt via --append-system-prompt.
- **api** — the anthropic SDK, authenticated by ANTHROPIC_API_KEY.

`provider="auto"` (the default) prefers the Claude Code CLI when it's on PATH and
falls back to the API key otherwise, so it "just works" with either setup.

complete() returns {"text": ..., "provider": ...} on success or {"error": ...}
so callers can degrade gracefully — the deterministic analysis always stands on
its own without the write-up.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile

from .config import DEFAULT_MODEL


def cli_available() -> bool:
    return shutil.which("claude") is not None


def _via_cli(system: str, user: str, timeout: int) -> tuple[str | None, str | None]:
    """One-shot `claude -p` using the Claude Code login. Model is intentionally
    left to the user's Claude Code default so we never pass an id the CLI
    rejects. Runs in a temp cwd so it doesn't pick up project context."""
    cmd = ["claude", "-p", "--output-format", "text"]
    if system:
        cmd += ["--append-system-prompt", system]
    try:
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, timeout=timeout,
            cwd=tempfile.gettempdir(), encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "claude CLI not found"
    except subprocess.TimeoutExpired:
        return None, f"claude CLI timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"claude CLI error: {exc}"
    if proc.returncode != 0:
        return None, f"claude CLI exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    text = (proc.stdout or "").strip()
    return (text, None) if text else (None, "claude CLI returned no text")


def _via_api(system: str, user: str, model: str | None, max_tokens: int,
             effort: str) -> tuple[str | None, str | None]:
    try:
        import anthropic
        client = anthropic.Anthropic()
    except Exception as exc:
        return None, f"anthropic SDK unavailable ({exc})"
    try:
        resp = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens,
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            return None, "model declined to respond"
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        return (text, None) if text else (None, "empty response")
    except Exception as exc:
        return None, f"API error: {exc}"


def complete(system: str, user: str, provider: str = "auto", model: str | None = None,
             max_tokens: int = 900, effort: str = "medium",
             timeout: int = 120) -> dict:
    """Get a completion from Claude. `provider`: 'auto' | 'claude_code' | 'api'."""
    if provider == "claude_code":
        order = ["cli"]
    elif provider == "api":
        order = ["api"]
    else:  # auto — prefer the subscription-backed CLI, fall back to the API key
        order = (["cli"] if cli_available() else []) + ["api"]

    errors = []
    for prov in order:
        if prov == "cli":
            text, err = _via_cli(system, user, timeout)
        else:
            text, err = _via_api(system, user, model, max_tokens, effort)
        if text:
            return {"text": text, "provider": "claude_code" if prov == "cli" else "api"}
        if err:
            errors.append(err)
    return {"error": "; ".join(errors) or "no LLM provider available"}
