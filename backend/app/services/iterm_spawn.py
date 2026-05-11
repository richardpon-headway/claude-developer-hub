"""Spawn an iTerm2 window for a worktree.

Two tabs, both ``cd``'d into the worktree path:

- tab 1: runs ``claude`` (Claude Code CLI)
- tab 2: an idle shell

Window frame (size + position) comes from the user config's
``iterm2.default_window`` block. Shipped defaults are intentionally
generic (1024×768 at 50,50); the user overrides locally via Claude-driven
onboarding (or by hand-editing ``~/.config/cdh/config.yaml``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.config.schema import ITermWindow
from app.db import open_db
from app.models.worktree import now_iso

if TYPE_CHECKING:
    import iterm2

log = logging.getLogger(__name__)


@dataclass
class SpawnResult:
    window_id: str
    claude_session_id: str
    shell_session_id: str


async def spawn_worktree_window(
    connection: "iterm2.Connection",
    worktree_path: Path,
    frame: ITermWindow,
) -> SpawnResult:
    """Open a new iTerm2 window at ``frame`` and seed it with Claude +
    shell tabs in ``worktree_path``. Returns the iTerm2-assigned ids
    so callers can persist them in the ``iterm_session`` table.

    Raises any underlying ``iterm2.RPCException`` so the caller turns it
    into an HTTP 5xx with a useful detail.
    """
    import iterm2

    app = await iterm2.async_get_app(connection)
    if app is None:
        raise RuntimeError("iTerm2 reports no app; is the Python API enabled?")

    window = await app.async_create_window_with_default_profile()
    await window.async_set_frame(
        iterm2.Frame(
            origin=iterm2.Point(frame.x, frame.y),
            size=iterm2.Size(frame.width, frame.height),
        )
    )

    # Tab 1: Claude
    tab1 = window.current_tab
    claude_session = tab1.current_session
    # \n triggers Enter; we send both lines in one call so the shell
    # treats them as separate commands rather than partial input.
    await claude_session.async_send_text(f"cd {worktree_path}\nclaude\n")

    # Tab 2: shell only
    tab2 = await window.async_create_tab()
    shell_session = tab2.current_session
    await shell_session.async_send_text(f"cd {worktree_path}\n")

    return SpawnResult(
        window_id=window.window_id,
        claude_session_id=claude_session.session_id,
        shell_session_id=shell_session.session_id,
    )


def get_claude_session_id_sync(repo: str, worktree_name: str) -> str | None:
    """Look up the persisted iTerm2 session_id for the Claude tab of a
    worktree. Returns None if no spawn-iterm has happened (or if rows
    were invalidated by an iTerm2 restart)."""
    conn = open_db()
    try:
        row = conn.execute(
            "SELECT iterm_session_id FROM iterm_session "
            "WHERE repo = ? AND worktree_name = ? AND role = 'claude'",
            (repo, worktree_name),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def upsert_iterm_sessions_sync(
    repo: str, worktree_name: str, result: SpawnResult
) -> None:
    """Replace any prior iterm_session rows for this worktree with the
    pair from a fresh spawn. INSERT-OR-REPLACE keyed on
    (repo, worktree_name, role) guarantees we don't accumulate stale
    rows if the user spawns a window twice.
    """
    spawned_at = now_iso()
    conn = open_db()
    try:
        conn.executemany(
            "INSERT INTO iterm_session "
            "(repo, worktree_name, role, iterm_window_id, iterm_session_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo, worktree_name, role) DO UPDATE SET "
            "  iterm_window_id = excluded.iterm_window_id, "
            "  iterm_session_id = excluded.iterm_session_id, "
            "  spawned_at = excluded.spawned_at",
            [
                (repo, worktree_name, "claude", result.window_id,
                 result.claude_session_id, spawned_at),
                (repo, worktree_name, "shell", result.window_id,
                 result.shell_session_id, spawned_at),
            ],
        )
        conn.commit()
    finally:
        conn.close()
