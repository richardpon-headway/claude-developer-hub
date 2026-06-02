"""Terminal adapter package — picks the active backend per ``config.terminal.kind``.

Routes that need to spawn a terminal window, focus an existing one, or
check availability call into this package rather than importing
iTerm2-specific helpers directly. The module-level functions here
dispatch to the right adapter (``iterm.py`` or ``ghostty.py``) and
translate adapter-specific failure modes into ``HTTPException`` codes
the route can return as-is.

Public surface:

- :func:`spawn_one_tab_claude` — fresh window, single tab, Claude only,
  with ``initial_prompt`` consumed as Claude's startup arg. Used by
  send-text, run-skill, and the global-skill buttons.
- :func:`spawn_two_tab_window` — fresh window, Claude + shell tabs.
  Used by the explicit "Open in <terminal>" button.
- :func:`focus_window` — bring an existing tracked window to the front.
  Reads ``terminal_kind`` from the persisted row so it knows which
  adapter to ask, even if the user has since toggled
  ``config.terminal.kind``.
- :func:`display_name` — human-readable terminal name, used in 503
  messages and exposed to the frontend for button labels.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

from app.config.loader import load_config

if TYPE_CHECKING:
    from app.services.iterm_spawn import SpawnResult

log = logging.getLogger(__name__)


@dataclass
class GenericSpawnResult:
    """Adapter-neutral two-tab spawn result.

    iTerm2's :class:`SpawnResult` has window_id, claude_session_id, and
    shell_session_id; Ghostty exposes equivalent ids via AppleScript.
    Routes that persist a row use this shape.
    """

    window_id: str
    claude_session_id: str
    shell_session_id: str
    terminal_kind: str  # 'iterm2' | 'ghostty'


def display_name(kind: str) -> str:
    return {"iterm2": "iTerm2", "ghostty": "Ghostty"}.get(kind, kind)


def active_kind() -> str:
    return load_config().terminal.kind


async def spawn_one_tab_claude(
    request: Request, cwd: Path, initial_prompt: str | None = None
) -> None:
    """Open a fresh 1-tab Claude window. When ``initial_prompt`` is
    ``None`` a plain ``claude`` (blank session) is launched. The window
    is *not* tracked in ``terminal_session`` — repeat sends are allowed
    to proliferate windows by design (PR #108).
    """
    config = load_config()
    kind = config.terminal.kind

    if kind == "iterm2":
        from app.services.iterm_spawn import spawn_global_claude_window

        iterm = _require_iterm_connection(request)
        frame = config.terminal.iterm2.default_window
        try:
            await spawn_global_claude_window(iterm.connection, cwd, frame, initial_prompt)
        except Exception as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
            ) from e
        return

    if kind == "ghostty":
        from app.services.terminal import ghostty

        size = config.terminal.ghostty.default_window
        try:
            await ghostty.spawn_one_tab_claude(cwd, initial_prompt, size)
        except ghostty.GhosttyUnavailable as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
        except Exception as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Ghostty spawn failed: {e}"
            ) from e
        return

    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        f"unsupported terminal.kind: {kind!r}",
    )


async def spawn_two_tab_window(request: Request, cwd: Path) -> GenericSpawnResult:
    """Open a 2-tab window (Claude + shell) for the worktree path.
    Returns the ids the caller persists in ``terminal_session`` to
    record the spawn.
    """
    config = load_config()
    kind = config.terminal.kind

    if kind == "iterm2":
        from app.services.iterm_spawn import spawn_two_tab_window as iterm_spawn_two

        iterm = _require_iterm_connection(request)
        frame = config.terminal.iterm2.default_window
        try:
            res: SpawnResult = await iterm_spawn_two(iterm.connection, cwd, frame)
        except Exception as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
            ) from e
        return GenericSpawnResult(
            window_id=res.window_id,
            claude_session_id=res.claude_session_id,
            shell_session_id=res.shell_session_id,
            terminal_kind="iterm2",
        )

    if kind == "ghostty":
        from app.services.terminal import ghostty

        size = config.terminal.ghostty.default_window
        try:
            return await ghostty.spawn_two_tab_window(cwd, size)
        except ghostty.GhosttyUnavailable as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
        except Exception as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Ghostty spawn failed: {e}"
            ) from e

    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        f"unsupported terminal.kind: {kind!r}",
    )


def _require_iterm_connection(request: Request):
    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python API "
            "and approve the first-connection auth dialog, then wait a few seconds.",
        )
    return iterm
