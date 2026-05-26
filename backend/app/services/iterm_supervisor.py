"""Long-lived asyncio task that owns the connection to iTerm2.

Responsibilities (plan §5 "Connection lifecycle"):

- Open ``iterm2.Connection.async_create()`` and stash it on
  ``app.state.iterm`` (a small dataclass-style state object).
- On any exception (iTerm2 not running, Python API disabled, lost
  socket): clear the cached connection, log, and retry with exponential
  backoff (1s, 2s, 4s, capped at 30s).
- On (re)connect, probe ``iterm2_started_at`` and compare to the value
  persisted in ``iterm_lifecycle``. On mismatch, mark every
  ``iterm_session`` row stale (its window/session ids no longer point at
  anything in the new iTerm2 process) and record the new value.

Endpoints that need a live connection read ``app.state.iterm.connection``
themselves and return 503 if it's ``None``. The supervisor doesn't
short-circuit them; it just makes the connection appear when iTerm2 is
available and disappear when it isn't.

Notification subscriptions (the per-session id-index used by skill-runner
sends) are deferred to a follow-up slice.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.db import open_db

if TYPE_CHECKING:
    import iterm2

log = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0


@dataclass
class ItermState:
    """Cached on ``app.state.iterm``. ``connection`` is None while we're
    disconnected; endpoints check truthiness before using."""

    connection: iterm2.Connection | None = None
    started_at: str | None = None
    # Reserved for the notification-driven session-id index added in a
    # follow-up slice.
    session_index: dict[str, Any] = field(default_factory=dict)


async def _get_iterm2_started_at(connection: iterm2.Connection) -> str | None:
    """Probe the ``iterm2_started_at`` app variable. Returns None if the
    variable isn't accessible (e.g., older iTerm2 build)."""
    import iterm2

    try:
        app = await iterm2.async_get_app(connection)
        if app is None:
            return None
        value = await app.async_get_variable("iterm2_started_at")
        return str(value) if value is not None else None
    except Exception as e:  # pragma: no cover — depends on iTerm2 build
        log.debug("could not read iterm2_started_at: %s", e)
        return None


def _read_persisted_started_at_sync() -> str | None:
    conn = open_db()
    try:
        row = conn.execute(
            "SELECT value FROM iterm_lifecycle WHERE key = 'iterm2_started_at'"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _write_persisted_started_at_sync(value: str) -> None:
    conn = open_db()
    try:
        conn.execute(
            "INSERT INTO iterm_lifecycle (key, value) VALUES ('iterm2_started_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (value,),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_iterm_sessions_stale_sync() -> int:
    """All cached iTerm2 session/window ids no longer point at anything
    after an iTerm2 restart. Drop only the iTerm2-flavored rows so a
    parallel Ghostty session (if any) survives. Returns the number of
    rows removed.
    """
    conn = open_db()
    try:
        cur = conn.execute(
            "DELETE FROM terminal_session WHERE terminal_kind = 'iterm2'"
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


async def _detect_and_handle_restart(connection: iterm2.Connection) -> None:
    """If the probed ``iterm2_started_at`` differs from what we have
    persisted, iTerm2 restarted: invalidate ``iterm_session`` rows and
    update the persisted value. First-ever connect (no persisted value)
    just records the value without invalidating anything."""
    current = await _get_iterm2_started_at(connection)
    if current is None:
        return  # can't tell; skip
    persisted = await asyncio.to_thread(_read_persisted_started_at_sync)
    if persisted is None:
        await asyncio.to_thread(_write_persisted_started_at_sync, current)
        return
    if persisted != current:
        removed = await asyncio.to_thread(_mark_iterm_sessions_stale_sync)
        log.warning("iTerm2 restart detected; invalidated %d iterm_session rows", removed)
        await asyncio.to_thread(_write_persisted_started_at_sync, current)


async def _wait_for_disconnect(connection: iterm2.Connection) -> None:
    """Block until the connection is closed. Done by waiting on the
    underlying websocket. Falls back to a long sleep if the underlying
    handle isn't accessible (we'll detect disconnect on the next API call
    from an endpoint anyway)."""
    ws = getattr(connection, "websocket", None)
    if ws is not None and hasattr(ws, "wait_closed"):
        try:
            await ws.wait_closed()
            return
        except Exception:  # pragma: no cover — depends on iTerm2 build
            pass
    # Fallback: don't busy-loop. Endpoints will detect a dead connection
    # via their own API calls and the supervisor task can be re-entered
    # via cancellation in lifespan shutdown.
    while True:
        await asyncio.sleep(60)


async def iterm_supervisor(state: Any) -> None:
    """Loop forever: connect, hold the connection until disconnect, retry
    with exponential backoff on failure.

    Cancellation (via lifespan shutdown) propagates as ``CancelledError``,
    which exits the loop cleanly.
    """
    import iterm2

    iterm = ItermState()
    state.iterm = iterm

    backoff = INITIAL_BACKOFF_SECONDS
    while True:
        try:
            log.info("iterm_supervisor: connecting to iTerm2…")
            connection = await iterm2.Connection.async_create()
            iterm.connection = connection
            await _detect_and_handle_restart(connection)
            iterm.started_at = await _get_iterm2_started_at(connection)
            log.info("iterm_supervisor: connected (iterm2_started_at=%s)", iterm.started_at)
            backoff = INITIAL_BACKOFF_SECONDS

            await _wait_for_disconnect(connection)
            log.warning("iterm_supervisor: connection closed; will reconnect")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("iterm_supervisor: connect failed: %s; retrying in %.1fs", e, backoff)
        finally:
            iterm.connection = None

        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            raise
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
