"""Discover the Claude session_id after spawn and write the
token-monitor sidecar JSON file.

Background (plan §7):

Claude Code generates a fresh UUID per session and writes
``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``. CDH doesn't see the
UUID directly; it has to poll for the new jsonl file after spawning
Claude in iTerm2. The ``<encoded-cwd>`` rule is ``absolute_cwd.replace('/', '-')``
(verified at implementation time against real Claude output).

Once discovered, CDH writes
``<token_monitor.sidecar_dir>/<session_id>.json`` describing how the
session was started + which worktree / ticket / PR it's bound to. CTM
reads these sidecars (via the additive PR in Slice I) and uses the
``ticket`` field to classify sessions that its prompt-scan heuristic
would otherwise leave as ``unclassified:`` buckets.

The discovery + sidecar write is run inline from the spawn endpoint
rather than as a background task. Claude typically writes the jsonl
within 1–2s, so the spawn response only blocks for a moment; the 30s
timeout is a safety bound, not the expected case. On timeout the
endpoint still returns success — sidecar just isn't written and
``claude_session_uuid`` is ``None``.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncio

from app.config.loader import load_config

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_TIMEOUT_SECONDS = 30.0


def encode_project_dir(absolute_path: Path) -> str:
    """Apply Claude Code's project-dir naming rule: each '/' becomes '-'
    on the absolute path. ``/Users/octocat/foo`` →
    ``-Users-octocat-foo``."""
    return str(absolute_path).replace("/", "-")


def claude_projects_dir(absolute_path: Path) -> Path:
    return Path.home() / ".claude" / "projects" / encode_project_dir(absolute_path)


async def discover_session_id(
    worktree_path: Path,
    mtime_floor: float,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> str | None:
    """Poll Claude's project dir for the worktree until a ``*.jsonl`` file
    with mtime strictly greater than ``mtime_floor`` appears.

    Returns the file's stem (the UUID = session_id) or ``None`` on timeout.

    ``mtime_floor`` should be ``time.time()`` captured immediately before
    we sent ``claude\\n`` to iTerm2, so we don't accidentally pick up an
    existing jsonl from a previous Claude session in the same directory.

    Defaults for ``timeout`` and ``poll_interval`` are resolved at call
    time from the module-level constants so tests can override via
    ``monkeypatch.setattr(sidecar, "DEFAULT_TIMEOUT_SECONDS", ...)``.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_SECONDS
    if poll_interval is None:
        poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
    proj_dir = claude_projects_dir(worktree_path)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if proj_dir.is_dir():
            best: Path | None = None
            best_mtime = mtime_floor
            for jsonl in proj_dir.glob("*.jsonl"):
                try:
                    m = jsonl.stat().st_mtime
                except FileNotFoundError:
                    continue
                if m > best_mtime:
                    best = jsonl
                    best_mtime = m
            if best is not None:
                return best.stem
        await asyncio.sleep(poll_interval)
    return None


def build_sidecar(
    session_id: str,
    *,
    worktree: str,
    ticket: str | None = None,
    pr_number: int | None = None,
    pr_repo: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Construct the sidecar dict per plan §7 schema. Optional fields
    are simply omitted (not set to ``null``) when the spawn site doesn't
    know them — CTM should handle missing keys gracefully."""
    sidecar: dict = {
        "session_id": session_id,
        "started_via": "cdh",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "worktree": worktree,
    }
    if ticket:
        sidecar["ticket"] = ticket
    if pr_number is not None:
        sidecar["pr_number"] = pr_number
    if pr_repo:
        sidecar["pr_repo"] = pr_repo
    if extra:
        sidecar["metadata"] = extra
    return sidecar


def write_sidecar_sync(session_id: str, sidecar: dict) -> Path:
    """Atomically write the sidecar JSON to ``<sidecar_dir>/<session_id>.json``.

    Tempfile + ``os.replace`` so a half-written file never appears at the
    canonical path if the process dies mid-write."""
    config = load_config()
    sidecar_dir = Path(config.token_monitor.sidecar_dir)
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    final = sidecar_dir / f"{session_id}.json"
    fd, tmp = tempfile.mkstemp(
        prefix=f".{final.name}.",
        suffix=".tmp",
        dir=str(sidecar_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2)
        os.replace(tmp, final)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return final
