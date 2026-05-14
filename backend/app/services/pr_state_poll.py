"""Long-lived asyncio task that refreshes the pr_state cache every
~3 minutes for every tracked worktree.

Modeled on iterm_supervisor: catches CancelledError and re-raises so
lifespan shutdown is clean, swallows all other exceptions per-tick so
one bad row can't kill the loop.

Initial tick fires on startup (no leading sleep) so the hub has PR
state within seconds of `make run`, not at the first interval mark.

Fan-out is bounded by a semaphore so we don't shell ~N gh processes
at once on first tick when N is large.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.services import pr_state
from app.services.gh_cli import GhNotFound
from app.services.worktree import list_worktrees_sync

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 180.0
PARALLELISM = 4


async def pr_state_poll_loop(state: Any) -> None:  # noqa: ARG001 — state kept for symmetry
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "pr_state poll tick failed: %s; will retry on next interval", e
            )
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def _tick() -> None:
    rows = await asyncio.to_thread(list_worktrees_sync)
    if not rows:
        return
    sem = asyncio.Semaphore(PARALLELISM)
    await asyncio.gather(*(_fetch_one(r, sem) for r in rows))


async def _fetch_one(row: Any, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            wt_path = Path(row.path)
            if not wt_path.is_dir():
                # Worktree gone from disk — leave any cached pr_state in
                # place; the user will see stale data flagged by status
                # going 'stale' elsewhere.
                return
            # Look up via the module rather than a local import so
            # tests can monkeypatch pr_state.fetch_pr_summary and have
            # the polling loop see it too.
            summary = await pr_state.fetch_pr_summary(wt_path)
            await asyncio.to_thread(
                pr_state.upsert_pr_state_sync, row.repo, row.name, summary
            )
        except GhNotFound:
            # gh missing → log once-per-tick is enough; suppress per-row.
            log.info("gh CLI not on PATH; skipping pr_state poll for %s/%s", row.repo, row.name)
        except Exception as e:
            log.warning(
                "pr_state fetch failed for %s/%s: %s", row.repo, row.name, e
            )
