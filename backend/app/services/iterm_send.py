"""Send text to a known iTerm2 session, with the P0 send-gate.

The send-gate (plan §11) is a correctness mechanism, not just UX polish:
without it, a slash command landing as ``y`` to a permission prompt would
execute arbitrary commands. Before any ``async_send_text`` we read the
session's recent screen contents and refuse the send if the trailing
visible line matches any pattern from ``config.iterm2.send_gate_patterns``.

Session lookup is currently O(N) over all open sessions; a follow-up
slice maintains a session-id → session index via iTerm2 notification
subscriptions for spec-mandated O(1) sends.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.config.loader import load_config

if TYPE_CHECKING:
    import iterm2

log = logging.getLogger(__name__)

SCREEN_GATE_LINES = 10


class SendGateError(Exception):
    """Raised when the send-gate refuses a send because the session looks
    like it's at a prompt awaiting input."""

    def __init__(self, matched_pattern: str, trailing: str) -> None:
        super().__init__(
            f"send-gate matched pattern {matched_pattern!r}; "
            f"trailing screen text: {trailing!r}"
        )
        self.matched_pattern = matched_pattern
        self.trailing = trailing


class SessionNotFoundError(Exception):
    """Raised when the session_id can't be located in the running iTerm2.
    Typically means iTerm2 was restarted or the user closed the window."""


async def find_session_by_id(
    connection: iterm2.Connection, session_id: str
) -> iterm2.Session | None:
    """Linear scan through all open iTerm2 sessions. Returns None if not
    found. The connection is assumed live; the caller checks ``app.state``."""
    import iterm2

    app = await iterm2.async_get_app(connection)
    if app is None:
        return None
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if session.session_id == session_id:
                    return session
    return None


def _extract_trailing_text(lines: list[str]) -> str:
    """Return the last non-blank line, stripped of trailing whitespace."""
    for line in reversed(lines):
        stripped = line.rstrip()
        if stripped:
            return stripped
    return ""


async def _read_trailing(
    session: iterm2.Session, num_lines: int = SCREEN_GATE_LINES
) -> str:
    """Pull the last ``num_lines`` rendered lines from the session screen
    and return the trailing non-blank one. Iterates the iterm2
    ``ScreenContents`` API."""
    contents = await session.async_get_screen_contents()
    n = contents.number_of_lines
    lines = [contents.line(i).string for i in range(max(0, n - num_lines), n)]
    return _extract_trailing_text(lines)


def _send_gate_check(trailing: str, patterns: list[str]) -> str | None:
    """Return the first matching pattern, or None if the trailing text
    looks safe to send into."""
    for pattern in patterns:
        try:
            if re.search(pattern, trailing):
                return pattern
        except re.error as e:
            log.warning("invalid send_gate_pattern %r: %s", pattern, e)
            continue
    return None


async def send_to_session(
    connection: iterm2.Connection,
    session_id: str,
    text: str,
    press_enter: bool = True,
    send_gate: bool = True,
) -> None:
    """Send ``text`` to the iTerm2 session with the given id.

    With ``send_gate`` enabled (the default), refuses to send if the
    session's trailing line matches one of the configured gate patterns.

    Raises:
      SessionNotFoundError — no session with that id (iTerm2 restart,
        window closed). The caller typically maps this to HTTP 404.
      SendGateError — gate pattern matched; caller maps to HTTP 409 with
        a "resolve the prompt first" message.
    """
    session = await find_session_by_id(connection, session_id)
    if session is None:
        raise SessionNotFoundError(f"iTerm2 session {session_id} not found")

    if send_gate:
        config = load_config()
        trailing = await _read_trailing(session)
        matched = _send_gate_check(trailing, config.iterm2.send_gate_patterns)
        if matched:
            raise SendGateError(matched, trailing)

    # Use CR (\r), not LF (\n), to trigger Enter. Claude Code's TUI runs
    # the terminal in raw mode and treats \n as a newline-within-input
    # (i.e. shift+Enter behavior), only submitting on \r. This is the
    # byte a physical Return key produces on macOS, so it also matches
    # how a human types into the prompt. Any LFs already in `text` are
    # preserved as intra-message newlines, which is the desired
    # behavior for multi-line sends.
    payload = text + ("\r" if press_enter else "")
    await session.async_send_text(payload)
