"""Background enrichment loop: walks every ``pr`` row, fetches the
classifier payload, writes both ``pr_state`` and the unified pr row's
scalar metadata.

Unifies the previous "pr_state poll walks worktrees" + "bookmark poll
refreshes bookmarks" + the lazy worktree.pr_author_login backfill into
one loop. Every card on the hub — bookmark, inbox, authored, worktree
— gets the same rich classifier output regardless of origin.

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
from app.models.pr import PrRow
from app.models.worktree import now_iso
from app.services import pr_db, pr_state
from app.services.gh_cli import GhNotFound

log = logging.getLogger(__name__)

PARALLELISM = 4


async def enrichment_poll_loop(state: Any) -> None:  # noqa: ARG001 — kept for lifespan symmetry
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "enrichment poll tick failed: %s; will retry on next interval",
                e,
            )
        try:
            interval = load_config().polling.pr_enrichment_interval_seconds
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def _tick() -> None:
    """Walk every pr row + enrich. Worktree-attached PRs use the
    worktree's path for ``gh pr view`` (cheaper, branch-scoped); other
    PRs use the no-cwd ``gh pr view <num> --repo <pr_repo>`` form."""
    rows = await asyncio.to_thread(_list_enrichment_targets_sync)
    if not rows:
        return
    sem = asyncio.Semaphore(PARALLELISM)
    await asyncio.gather(*(_fetch_one(r, sem) for r in rows))


def _list_enrichment_targets_sync() -> list[tuple[str, int, str | None]]:
    """Every ``pr`` row + the worktree path attached to it (if any).

    Returns ``[(pr_repo, pr_number, worktree_path_or_None), ...]``.
    Single JOIN query so the enrichment tick doesn't N+1 against the
    worktree table.
    """
    from app.db import get_db_path, open_db

    db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr.pr_repo, pr.pr_number, w.path "
            "FROM pr "
            "LEFT JOIN worktree w "
            "  ON w.pr_repo = pr.pr_repo AND w.pr_number = pr.pr_number"
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        conn.close()


async def _fetch_one(
    target: tuple[str, int, str | None], sem: asyncio.Semaphore
) -> None:
    pr_repo, pr_number, worktree_path = target
    async with sem:
        try:
            if worktree_path is not None and Path(worktree_path).is_dir():
                summary = await pr_state.fetch_pr_summary(Path(worktree_path))
            else:
                summary = await pr_state.fetch_pr_summary_by_pr(
                    pr_repo, pr_number
                )
            # Write classifier output to both pr_state (the rich
            # payload, FK'd to pr) and pr's scalar columns. The latter
            # feeds the bookmark / inbox / authored surfaces that read
            # straight from pr; the former feeds the worktree row's
            # pr_state badge.
            #
            # last_refreshed_at = now() retires the temporary semantic
            # plan-59 added (non-worktree bookmarks' last_refreshed_at
            # was frozen until this loop shipped).
            await asyncio.to_thread(
                pr_db.upsert_pr_sync,
                PrRow(
                    pr_repo=pr_repo,
                    pr_number=pr_number,
                    title=summary.title,
                    url=summary.url,
                    author_login=summary.author_login,
                    state=_coerce_state(summary),  # type: ignore[arg-type]
                    is_draft=summary.is_draft,
                    ci_status=_coerce_ci_status(summary),  # type: ignore[arg-type]
                    pr_updated_at=summary.updated_at,
                    last_refreshed_at=now_iso(),
                ),
            )
            await asyncio.to_thread(
                pr_state.upsert_pr_state_sync,
                pr_repo,
                pr_number,
                summary,
            )
        except GhNotFound:
            # gh missing → log once-per-tick is enough; suppress per-row.
            log.info(
                "gh CLI not on PATH; skipping enrichment for %s#%s",
                pr_repo, pr_number,
                extra={"pr_repo": pr_repo, "pr_number": pr_number},
            )
        except Exception as e:
            log.info(
                "enrichment fetch failed for %s#%s: %s",
                pr_repo, pr_number, e,
                extra={"pr_repo": pr_repo, "pr_number": pr_number},
            )


def _coerce_state(summary: pr_state.PrSummary) -> str | None:
    """Map a PrSummary's terminal-state labels to the ``pr.state`` enum
    (open/closed/merged). Returns None for ``no_pr`` so COALESCE-on-
    upsert preserves whatever was previously stored — gh momentarily
    not finding the PR shouldn't wipe a known-good state."""
    if "merged" in summary.labels:
        return "merged"
    if "closed" in summary.labels:
        return "closed"
    if "no_pr" in summary.labels:
        return None
    return "open"


def _coerce_ci_status(summary: pr_state.PrSummary) -> str | None:
    """Reduce the classifier's check tallies to the four-way
    ``pr.ci_status`` enum the discovery rows use."""
    if summary.checks.fail > 0:
        return "fail"
    if summary.checks.pending > 0:
        return "pending"
    if summary.checks.passed > 0:
        return "pass"
    return "none"
