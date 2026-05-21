"""Background poller that refreshes bookmark metadata.

Each tick walks every bookmark and probes ``gh pr view --json
state,title,author,url`` to keep the row's cached search-driven fields
current. ``state`` transitions (open → merged / closed) are reflected
on the row but never trigger deletion — bookmarks are user-curated;
removal requires an explicit unbookmark.

Cadence is fixed at 5 minutes for v1. Tweaking via config is deferred
to the backlog (kept simple while bookmark counts stay small).
"""
from __future__ import annotations

import asyncio
import logging

from app.config.loader import load_config
from app.models.worktree import now_iso
from app.services import bookmark_db
from app.services.gh_cli import GhFailed, GhNotFound, run_gh_json
from app.services.inbox_poll import _extract_ticket

log = logging.getLogger(__name__)

_INTERVAL_SECONDS = 300


async def bookmark_poll_loop(state) -> None:  # type: ignore[no-untyped-def]
    """Long-lived asyncio task. Tick failures log + retry; cancellation
    propagates so the lifespan teardown is clean.

    ``state`` matches the other poll-loop signatures for symmetry; this
    poller doesn't read or write it."""
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "bookmark poll tick failed: %s; will retry next interval", e
            )
        try:
            await asyncio.sleep(_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def _tick() -> None:
    bookmarks = await asyncio.to_thread(bookmark_db.list_bookmarks_sync)
    if not bookmarks:
        return
    config = load_config()
    now = now_iso()
    for b in bookmarks:
        try:
            data = await run_gh_json(
                [
                    "pr", "view", str(b.pr_number),
                    "--repo", b.pr_repo,
                    "--json", "title,author,state",
                ],
                swallow_errors=True,
            )
        except GhNotFound:
            log.info(
                "gh CLI not on PATH; bookmark refresh skipped this tick"
            )
            return
        except GhFailed as e:
            log.info(
                "gh pr view failed for bookmark %s#%s: %s",
                b.pr_repo, b.pr_number, e,
            )
            continue
        if not isinstance(data, dict):
            continue

        title_raw = data.get("title")
        title = title_raw if isinstance(title_raw, str) else b.title
        author_raw = (data.get("author") or {}).get("login")
        author = author_raw if isinstance(author_raw, str) else b.author_login

        gh_state = data.get("state")
        new_state = b.state
        if isinstance(gh_state, str):
            s = gh_state.lower()
            if s in ("open", "closed", "merged"):
                new_state = s  # type: ignore[assignment]

        ticket = _extract_ticket(title, config.repos) if title else b.ticket

        await asyncio.to_thread(
            bookmark_db.refresh_bookmark_state_sync,
            b.pr_repo, b.pr_number,
            state=new_state,
            title=title,
            author_login=author,
            ticket=ticket,
            last_refreshed_at=now,
        )
