"""iTerm2 mock builders + iterm_session row seeder for tests.

These helpers produce ``MagicMock`` trees that mirror the iTerm2
Python API shape that ``app.services.iterm_spawn`` walks. Tests never
touch a real iTerm2 — they patch ``iterm2.Window.async_create`` to
return one of these fakes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def build_fake_window(
    *,
    window_id: str = "W1",
    claude_session_id: str = "S-claude",
    shell_session_id: str = "S-shell",
) -> MagicMock:
    """Two-tab fake used by ``spawn_two_tab_window`` tests.

    The mock tree mirrors what the spawn function walks:
        window → current_tab (claude) → current_session
        window → async_create_tab() → shell_tab → current_session
    """
    claude_session = MagicMock(session_id=claude_session_id)
    claude_session.async_send_text = AsyncMock()
    claude_tab = MagicMock(current_session=claude_session)
    claude_tab.async_select = AsyncMock()

    shell_session = MagicMock(session_id=shell_session_id)
    shell_session.async_send_text = AsyncMock()
    shell_tab = MagicMock(current_session=shell_session)

    window = MagicMock(window_id=window_id, current_tab=claude_tab)
    window.async_set_frame = AsyncMock()
    window.async_create_tab = AsyncMock(return_value=shell_tab)
    window.async_activate = AsyncMock()
    return window


def build_fake_global_window(
    *,
    window_id: str = "GW1",
    session_id: str = "GS1",
) -> MagicMock:
    """Single-tab variant for ``spawn_global_claude_window`` tests —
    global spawns are one-tab only (no shell tab)."""
    session = MagicMock(session_id=session_id)
    session.async_send_text = AsyncMock()
    tab = MagicMock(current_session=session)
    tab.async_select = AsyncMock()
    window = MagicMock(window_id=window_id, current_tab=tab)
    window.async_set_frame = AsyncMock()
    window.async_activate = AsyncMock()
    return window


def seed_iterm_session(
    db_path: Path,
    repo: str,
    worktree_name: str,
    *,
    window_id: str = "W1",
    session_id: str = "S-claude",
    role: str = "claude",
    claude_session_uuid: str | None = None,
    spawned_at: str = "2026-01-01T00:00:00Z",
) -> None:
    """Insert one iterm_session row. Caller must seed a matching
    worktree row first (FK)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO iterm_session "
            "(repo, worktree_name, role, iterm_window_id, iterm_session_id, "
            " claude_session_uuid, spawned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                repo,
                worktree_name,
                role,
                window_id,
                session_id,
                claude_session_uuid,
                spawned_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
