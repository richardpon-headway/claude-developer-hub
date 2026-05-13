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


@dataclass
class GlobalSpawnResult:
    """Outcome of a non-worktree iTerm2 spawn (e.g. a hub-level global
    skill button). Only one tab is created — Claude, with the slash
    command pre-loaded via ``claude <initial_prompt>`` — so there's no
    shell_session_id to surface."""

    window_id: str
    claude_session_id: str


async def _bring_window_to_front(
    connection: iterm2.Connection,
    window: iterm2.Window,
    focus_tab: iterm2.Tab,
) -> None:
    """Select the given tab, then activate the iTerm2 app + window so
    the spawn isn't hidden behind the browser. Each step is best-effort
    — a transient iTerm2 state must never sink the spawn."""
    import iterm2

    await focus_tab.async_select()
    try:
        app = await iterm2.async_get_app(connection)
        if app is not None:
            await app.async_activate(raise_all_windows=False)
    except Exception as e:
        log.warning("iTerm2 app activate failed (non-fatal): %s", e)
    try:
        await window.async_activate()
    except Exception as e:
        log.warning("iTerm2 window activate failed (non-fatal): %s", e)


async def spawn_worktree_window(
    connection: iterm2.Connection,
    worktree_path: Path,
    frame: ITermWindow,
    initial_prompt: str | None = None,
) -> SpawnResult:
    """Open a new iTerm2 window at ``frame`` and seed it with Claude +
    shell tabs in ``worktree_path``. Returns the iTerm2-assigned ids
    so callers can persist them in the ``iterm_session`` table.

    If ``initial_prompt`` is set, Claude is launched as
    ``claude '<prompt>'`` so the prompt runs at startup (no race
    between "shell ready" and "Claude ready" that would exist with a
    follow-up keystroke send). Used by ``run_skill`` to fire a slash
    command when no Claude session exists yet.

    Raises any underlying ``iterm2.RPCException`` so the caller turns it
    into an HTTP 5xx with a useful detail.
    """
    import iterm2

    # Window.async_create is the supported constructor (the App object
    # doesn't expose window creation directly). Using the default profile
    # by passing profile=None.
    window = await iterm2.Window.async_create(connection)
    if window is None:
        raise RuntimeError("iTerm2 returned no window — session ended immediately?")
    await window.async_set_frame(
        iterm2.Frame(
            origin=iterm2.Point(frame.x, frame.y),
            size=iterm2.Size(frame.width, frame.height),
        )
    )

    # Tab 1: Claude
    tab1 = window.current_tab
    claude_session = tab1.current_session
    # \n triggers Enter to the shell (cooked mode treats it as Return).
    # We send both lines in one call so the shell treats them as
    # separate commands rather than partial input.
    if initial_prompt is not None:
        quoted = "'" + initial_prompt.replace("'", "'\\''") + "'"
        claude_cmd = f"claude {quoted}"
    else:
        claude_cmd = "claude"
    await claude_session.async_send_text(f"cd {worktree_path}\n{claude_cmd}\n")

    # Tab 2: shell only
    tab2 = await window.async_create_tab()
    shell_session = tab2.current_session
    await shell_session.async_send_text(f"cd {worktree_path}\n")

    await _bring_window_to_front(connection, window, tab1)

    return SpawnResult(
        window_id=window.window_id,
        claude_session_id=claude_session.session_id,
        shell_session_id=shell_session.session_id,
    )


async def spawn_global_claude_window(
    connection: iterm2.Connection,
    cwd: Path,
    initial_prompt: str,
    frame: ITermWindow,
) -> GlobalSpawnResult:
    """Open a one-tab iTerm2 window at ``frame``, ``cd`` into ``cwd``,
    and launch Claude with ``initial_prompt`` as its first message —
    ``claude`` accepts a positional argument that becomes the user's
    opening prompt, so a slash command passed here runs at startup
    with no race between "shell ready" and "Claude ready".

    Unlike :func:`spawn_worktree_window`, this does NOT write to the
    ``iterm_session`` table — global spawns aren't bound to a worktree.
    """
    import iterm2

    window = await iterm2.Window.async_create(connection)
    if window is None:
        raise RuntimeError("iTerm2 returned no window — session ended immediately?")
    await window.async_set_frame(
        iterm2.Frame(
            origin=iterm2.Point(frame.x, frame.y),
            size=iterm2.Size(frame.width, frame.height),
        )
    )

    tab = window.current_tab
    session = tab.current_session
    # Single shell call: cd, then `claude <prompt>`. The prompt is
    # passed as a single positional arg via shell quoting; callers are
    # expected to pass a single token like "/skill-name", so no escaping
    # is needed for the v1 callers, but we wrap in single-quotes anyway
    # so future multi-word prompts don't break.
    quoted_prompt = "'" + initial_prompt.replace("'", "'\\''") + "'"
    await session.async_send_text(f"cd {cwd}\nclaude {quoted_prompt}\n")

    await _bring_window_to_front(connection, window, tab)

    return GlobalSpawnResult(
        window_id=window.window_id,
        claude_session_id=session.session_id,
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


def set_iterm_session_uuid_sync(
    repo: str,
    worktree_name: str,
    window_id: str,
    claude_session_uuid: str,
) -> int:
    """Set ``claude_session_uuid`` on the claude-role ``iterm_session``
    row, but only if it still points at ``window_id``. Used by the
    fire-and-forget post-spawn discovery task to avoid clobbering a row
    that a later spawn already replaced.

    Returns the number of rows actually updated (0 if the row was
    overtaken by a newer spawn, or no row exists)."""
    conn = open_db()
    try:
        cur = conn.execute(
            "UPDATE iterm_session SET claude_session_uuid = ? "
            "WHERE repo = ? AND worktree_name = ? "
            "AND role = 'claude' AND iterm_window_id = ?",
            (claude_session_uuid, repo, worktree_name, window_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def upsert_iterm_sessions_sync(
    repo: str,
    worktree_name: str,
    result: SpawnResult,
    claude_session_uuid: str | None = None,
) -> None:
    """Replace any prior iterm_session rows for this worktree with the
    pair from a fresh spawn. INSERT-OR-REPLACE keyed on
    (repo, worktree_name, role) guarantees we don't accumulate stale
    rows if the user spawns a window twice.

    ``claude_session_uuid`` is the Claude Code session UUID discovered
    by polling ``~/.claude/projects/<encoded-cwd>/*.jsonl`` after spawn
    (Slice H). It applies only to the ``claude`` role row; the shell
    row's ``claude_session_uuid`` is always NULL.
    """
    spawned_at = now_iso()
    conn = open_db()
    try:
        conn.executemany(
            "INSERT INTO iterm_session "
            "(repo, worktree_name, role, iterm_window_id, iterm_session_id, "
            " claude_session_uuid, spawned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo, worktree_name, role) DO UPDATE SET "
            "  iterm_window_id = excluded.iterm_window_id, "
            "  iterm_session_id = excluded.iterm_session_id, "
            "  claude_session_uuid = excluded.claude_session_uuid, "
            "  spawned_at = excluded.spawned_at",
            [
                (repo, worktree_name, "claude", result.window_id,
                 result.claude_session_id, claude_session_uuid, spawned_at),
                (repo, worktree_name, "shell", result.window_id,
                 result.shell_session_id, None, spawned_at),
            ],
        )
        conn.commit()
    finally:
        conn.close()
