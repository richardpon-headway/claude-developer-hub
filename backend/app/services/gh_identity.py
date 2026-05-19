"""Cache the local user's GitHub login.

The hub uses the login to decide whether a worktree's underlying PR
was authored by the user (→ owner / state-tier) or by someone else
(→ REVIEWING tier). We resolve it once at first call via
``gh api user --jq .login`` and cache the result for process lifetime;
the login doesn't change at runtime.

Failure is fail-open: an unknown login propagates as ``None``, and the
frontend treats every worktree as owner-by-default. That matches the
behavior before the REVIEWING tier existed.
"""
from __future__ import annotations

import asyncio
import logging

from app.services.gh_cli import GhNotFound, run_gh_json

log = logging.getLogger(__name__)

_cached_login: str | None = None
_cache_lock = asyncio.Lock()


def reset_cache() -> None:
    """Test hook — clear the cached login so a fresh call re-shells gh."""
    global _cached_login
    _cached_login = None


async def get_user_login() -> str | None:
    """Return the local user's gh login, or None on failure.

    The first successful call caches; subsequent calls return the
    cached string without shelling. Concurrent first calls are
    serialized so we don't fire two ``gh`` subprocesses for the same
    answer.
    """
    global _cached_login
    if _cached_login is not None:
        return _cached_login
    async with _cache_lock:
        if _cached_login is not None:
            return _cached_login
        try:
            # `--jq .login` makes gh emit the bare login string. That's
            # valid JSON (a JSON string literal), so run_gh_json parses
            # it back to a Python str.
            data = await run_gh_json(
                ["api", "user", "--jq", ".login"], swallow_errors=True,
            )
        except GhNotFound:
            log.info("gh not on PATH — REVIEWING tier disabled")
            return None
        if isinstance(data, str) and data:
            _cached_login = data
        return _cached_login
