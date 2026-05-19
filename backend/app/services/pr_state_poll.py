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

from app.config.loader import load_config
from app.services import pr_state
from app.services.gh_cli import GhNotFound
from app.services.worktree import (
    list_worktrees_sync,
    update_worktree_pr_author_sync,
)

log = logging.getLogger(__name__)

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
            # Re-read config every tick so YAML edits take effect on
            # the next cycle without a backend restart.
            interval = load_config().polling.pr_state_interval_seconds
            await asyncio.sleep(interval)
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
            # Lazy backfill: worktrees created before the
            # pr_author_login column existed have NULL there. Once we
            # have a fresh gh payload with an author, write it to the
            # worktree row so the hub can route this row to REVIEWING
            # vs. an owner tier without needing the pull-down path to
            # have populated it. The helper itself no-ops when the
            # column already has a value, so this is safe to call
            # every tick.
            if summary.author_login and row.pr_author_login is None:
                await asyncio.to_thread(
                    update_worktree_pr_author_sync,
                    row.repo,
                    row.name,
                    summary.author_login,
                )
        except GhNotFound:
            # gh missing → log once-per-tick is enough; suppress per-row.
            log.info("gh CLI not on PATH; skipping pr_state poll for %s/%s", row.repo, row.name)
        except Exception as e:
            log.warning(
                "pr_state fetch failed for %s/%s: %s", row.repo, row.name, e
            )
