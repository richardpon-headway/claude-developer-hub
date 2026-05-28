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

import logging

from app.services.gh_cli import GhNotFound, run_gh_json

log = logging.getLogger(__name__)

_cached_login: str | None = None


def reset_cache() -> None:
    """Test hook — clear the cached login so a fresh call re-shells gh."""
    global _cached_login
    _cached_login = None


async def get_user_login() -> str | None:
    """Return the local user's gh login, or None on failure.

    The first successful call caches; subsequent calls return the
    cached string without shelling. Concurrent first calls each
    shell ``gh api user`` and both write the same value to the
    cache — benign and simpler than a per-loop lock (an asyncio.Lock
    pinned to one loop breaks under pytest where each test gets a
    fresh loop).
    """
    global _cached_login
    if _cached_login is not None:
        return _cached_login
    try:
        # Plain ``gh api user`` returns the full GitHub user JSON
        # object including ``login`` and a couple dozen other fields.
        # We could narrow with ``--jq .login`` but jq emits bare
        # values (not JSON-quoted), so the output wouldn't parse as
        # JSON in ``run_gh_json``. Paying for the bigger response is
        # fine — this fires once per process.
        data = await run_gh_json(["api", "user"], swallow_errors=True)
    except GhNotFound:
        log.info("gh not on PATH — REVIEWING tier disabled")
        return None
    if isinstance(data, dict):
        login = data.get("login")
        if isinstance(login, str) and login:
            _cached_login = login
    return _cached_login
